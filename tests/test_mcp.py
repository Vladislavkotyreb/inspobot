"""Клиент MCP: разбор кадров и рукопожатие на подставном транспорте."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from inspobot.mcp_client import (
    ACCEPT,
    PROTOCOL,
    McpClient,
    McpError,
    McpUnauthorized,
    SseReader,
    content_kind,
    is_server_request,
    pick_result,
    rpc_error,
    server_reply,
    sse_messages,
    tool_content,
)

URL = "https://api.mobbin.com/mcp"


def run(coro):
    return asyncio.run(coro)


class ContentKindTests(unittest.TestCase):
    def test_parameters_do_not_confuse_it(self):
        """Настоящий заголовок приходит с charset — сравнение на равенство
        его не узнаёт, и ответ уходит в «непонятный тип»."""
        self.assertEqual(content_kind("application/json; charset=utf-8"), "json")
        self.assertEqual(content_kind("TEXT/EVENT-STREAM"), "sse")
        self.assertEqual(content_kind("text/html"), "")
        self.assertEqual(content_kind(""), "")


class SseReaderTests(unittest.TestCase):
    def test_comment_lines_never_reach_the_parser(self):
        reader = SseReader()
        self.assertEqual(reader.push(": heartbeat"), [])
        self.assertEqual(reader.push(""), [])

    def test_strips_exactly_one_space(self):
        reader = SseReader()
        reader.push("data:  два пробела")
        self.assertEqual(reader.push(""), [" два пробела"])

    def test_multiline_data_is_joined(self):
        reader = SseReader()
        reader.push("data: {")
        reader.push('data: "a": 1}')
        self.assertEqual(reader.push(""), ['{\n"a": 1}'])

    def test_id_is_remembered_for_a_possible_resume(self):
        reader = SseReader()
        reader.push("id: 42")
        reader.push("data: x")
        reader.push("")
        self.assertEqual(reader.last_id, "42")

    def test_event_boundary_resets_the_buffer(self):
        reader = SseReader()
        reader.push("data: один")
        reader.push("")
        reader.push("data: два")
        self.assertEqual(reader.push(""), ["два"])

    def test_messages_skip_unparsable_events(self):
        lines = [": ping", "", "data: не json", "", 'data: {"id": 1}', ""]
        self.assertEqual(list(sse_messages(lines)), [{"id": 1}])

    def test_tail_without_a_blank_line_still_counts(self):
        """Сервер закрыл поток сразу после data: — сообщение потерять нельзя."""
        self.assertEqual(list(sse_messages(['data: {"id": 7}'])), [{"id": 7}])


class PickResultTests(unittest.TestCase):
    def test_finds_our_answer_among_others(self):
        messages = [
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}},
        ]
        self.assertEqual(pick_result(messages, 3), {"ok": True})

    def test_id_comparison_is_type_strict(self):
        """Мы отправили число. Строка «3» — чужое сообщение."""
        with self.assertRaises(McpError):
            pick_result([{"id": "3", "result": {}}], 3)

    def test_error_body_becomes_the_message(self):
        with self.assertRaisesRegex(McpError, "нет такого инструмента.*-32601"):
            pick_result([{"id": 1, "error": {"code": -32601, "message": "нет такого инструмента"}}], 1)

    def test_stream_without_an_answer_is_an_error_not_emptiness(self):
        """Оборванный поток обязан отличаться от «ничего не нашлось»."""
        with self.assertRaisesRegex(McpError, "ответа на запрос так и не пришло"):
            pick_result([{"method": "notifications/progress"}], 1)

    def test_nothing_at_all(self):
        with self.assertRaisesRegex(McpError, "ни одного сообщения"):
            pick_result([], 1)

    def test_a_server_request_with_our_id_is_not_our_answer(self):
        with self.assertRaises(McpError):
            pick_result([{"id": 1, "method": "ping"}], 1)


class ServerMessageTests(unittest.TestCase):
    def test_request_versus_notification(self):
        self.assertTrue(is_server_request({"id": 1, "method": "ping"}))
        self.assertFalse(is_server_request({"method": "notifications/progress"}))
        self.assertFalse(is_server_request({"id": 1, "result": {}}))

    def test_ping_gets_an_empty_result(self):
        self.assertEqual(
            server_reply({"id": 9, "method": "ping"}),
            {"jsonrpc": "2.0", "id": 9, "result": {}},
        )

    def test_unknown_method_gets_a_refusal(self):
        reply = server_reply({"id": 9, "method": "sampling/createMessage"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_rpc_error_on_junk(self):
        self.assertEqual(rpc_error("не объект"), "")
        self.assertEqual(rpc_error({"result": {}}), "")


class ToolContentTests(unittest.TestCase):
    def test_structured_content_wins(self):
        result = {"structuredContent": {"screens": [1]}, "content": [{"type": "text", "text": "{}"}]}
        self.assertEqual(tool_content(result), {"screens": [1]})

    def test_falls_back_to_a_json_text_block(self):
        result = {"content": [
            {"type": "image", "data": "..."},
            {"type": "text", "text": '{"screens": [{"id": "a"}]}'},
        ]}
        self.assertEqual(tool_content(result), {"screens": [{"id": "a"}]})

    def test_non_json_text_is_left_alone(self):
        result = {"content": [{"type": "text", "text": "просто текст"}]}
        self.assertEqual(tool_content(result), result["content"])

    def test_tool_error_carries_its_text(self):
        result = {"isError": True, "content": [{"type": "text", "text": "лимит исчерпан"}]}
        with self.assertRaisesRegex(McpError, "лимит исчерпан"):
            tool_content(result)


def sse(*messages: dict) -> bytes:
    return "".join(f"data: {json.dumps(m)}\n\n" for m in messages).encode()


class HandshakeTests(unittest.TestCase):
    """Рукопожатие целиком на подставном транспорте: порядок, заголовки,
    сессия, версия. До живого сервера отсюда не достучаться — egress-политика
    песочницы рубит api.mobbin.com, — поэтому проверяется так."""

    def transport(self, *, protocol: str = PROTOCOL, framing: str = "json",
                  session: str | None = "sess-1", status: int = 200):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = json.loads(request.content)
            if status != 200:
                return httpx.Response(status, json={"error": {"code": -1, "message": "низзя"}})
            if body.get("method") == "initialize":
                payload = {"jsonrpc": "2.0", "id": body["id"],
                           "result": {"protocolVersion": protocol, "capabilities": {}}}
                headers = {"mcp-session-id": session} if session else {}
                return httpx.Response(200, json=payload, headers=headers)
            if "id" not in body:
                return httpx.Response(202)
            payload = {"jsonrpc": "2.0", "id": body["id"],
                       "result": {"content": [{"type": "text", "text": '{"screens": []}'}]}}
            if framing == "sse":
                return httpx.Response(
                    200, content=sse({"method": "notifications/progress"}, payload),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json=payload)

        return httpx.MockTransport(handler), seen

    def test_order_headers_and_session(self):
        transport, seen = self.transport()

        async def go():
            async with McpClient(url=URL, token="tok-1", transport=transport) as client:
                await client.start()
                return await client.call_tool("search_screens", {"query": "x"})

        self.assertEqual(run(go()), {"screens": []})
        self.assertEqual(
            [json.loads(r.content).get("method") for r in seen],
            ["initialize", "notifications/initialized", "tools/call"],
        )
        # Accept обязан перечислять оба типа на каждом POST, включая уведомление.
        for request in seen:
            self.assertEqual(request.headers["accept"], ACCEPT)
            self.assertEqual(request.headers["authorization"], "Bearer tok-1")
        # На initialize договариваться ещё не о чем — заголовка версии там нет.
        self.assertNotIn("mcp-protocol-version", seen[0].headers)
        self.assertNotIn("mcp-session-id", seen[0].headers)
        for request in seen[1:]:
            self.assertEqual(request.headers["mcp-protocol-version"], PROTOCOL)
            self.assertEqual(request.headers["mcp-session-id"], "sess-1")

    def test_server_without_a_session_id_is_fine(self):
        transport, seen = self.transport(session=None)

        async def go():
            async with McpClient(url=URL, token="t", transport=transport) as client:
                await client.start()
                await client.call_tool("search_screens", {"query": "x"})

        run(go())
        self.assertNotIn("mcp-session-id", seen[-1].headers)

    def test_sse_framing_end_to_end(self):
        transport, _ = self.transport(framing="sse")

        async def go():
            async with McpClient(url=URL, token="t", transport=transport) as client:
                await client.start()
                return await client.call_tool("search_screens", {"query": "x"})

        self.assertEqual(run(go()), {"screens": []})

    def test_another_revision_stops_the_client(self):
        """Слепо согласиться нельзя: в 2025-11-25 оборванный поток
        продолжают через GET, и без этого пустой ответ не отличить от
        потерянного."""
        transport, _ = self.transport(protocol="2025-11-25")

        async def go():
            async with McpClient(url=URL, token="t", transport=transport) as client:
                await client.start()

        with self.assertRaisesRegex(McpError, "2025-11-25"):
            run(go())

    def test_401_is_its_own_error_so_the_caller_can_refresh(self):
        transport, _ = self.transport(status=401)

        async def go():
            async with McpClient(url=URL, token="stale", transport=transport) as client:
                await client.start()

        with self.assertRaises(McpUnauthorized):
            run(go())

    def test_error_body_beats_the_status_code(self):
        transport, _ = self.transport(status=400)

        async def go():
            async with McpClient(url=URL, token="t", transport=transport) as client:
                await client.start()

        with self.assertRaisesRegex(McpError, "низзя"):
            run(go())

    def test_redirects_are_not_followed(self):
        """httpx снимает Authorization при переходе на другой хост, и ответ
        401 после этого читается как истёкший токен. Обновление по такому
        «истечению» сжигает refresh-токен на каждом запуске."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(307, headers={"location": "https://other.example/mcp"})

        async def go():
            async with McpClient(
                url=URL, token="t", transport=httpx.MockTransport(handler)
            ) as client:
                await client.start()

        with self.assertRaisesRegex(McpError, "HTTP 307"):
            run(go())


if __name__ == "__main__":
    unittest.main()
