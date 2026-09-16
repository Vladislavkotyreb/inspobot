"""Минимальный клиент MCP поверх streamable HTTP.

Зачем он, когда есть коннектор Anthropic. Утренняя лента не должна ходить к
Claude: у Mobbin уже есть свой MCP-сервер, токен к нему выписан на сам
ресурс (`resource=https://api.mobbin.com/mcp`, RFC 8707 — см. `mobbin_auth`),
и разговаривать с ним можно напрямую. Один запрос вместо цепочки
«модель → коннектор → сервер», нулевая стоимость и ни одного токена модели.

Разбор кадров вынесен в чистые функции (`SseReader`, `pick_result`,
`content_kind`, `tool_content`) — они и проверяются тестами. В сети остаётся
только то, что без сети проверить нельзя.

Реализована ревизия 2025-06-18. Если сервер договорится на другую, клиент
скажет об этом и остановится, а не сделает вид, что понял: в 2025-11-25
поменялась семантика потока (переподключение через GET с Last-Event-ID), и
молчаливое «поток кончился» там выглядит как пустой ответ, а не как ошибка.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

import httpx

log = logging.getLogger(__name__)

PROTOCOL = "2025-06-18"
# Спецификация требует перечислить оба типа на каждом POST — и на уведомлении
# тоже. Сервер, которому не хватает одного, отвечает 406 ещё до тела запроса.
ACCEPT = "application/json, text/event-stream"
CLIENT_NAME = "inspobot"
CLIENT_VERSION = "0.1"

# Таймаут httpx считает простой между кусками, а не общее время. Отдельный
# срок на весь вызов нужен, иначе сервер, капающий по байту, держит нас вечно.
DEADLINE = 120.0
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class McpError(RuntimeError):
    pass


class McpUnauthorized(McpError):
    """401. Отдельным типом, чтобы вызывающий обновил токен и повторил —
    и, главное, сохранил обновлённый: Supabase гасит прежний refresh-токен
    при каждом обмене, и незаписанный на диск означает ручной вход завтра."""


# --- разбор кадров (без сети) -----------------------------------------------


def content_kind(content_type: str) -> str:
    """«json», «sse» или пусто.

    Сравнивать целиком нельзя: настоящий заголовок выглядит как
    `application/json; charset=utf-8`, и проверка на равенство его не узнаёт.
    """
    head = (content_type or "").split(";", 1)[0].strip().lower()
    if head.startswith("application/json"):
        return "json"
    if head.startswith("text/event-stream"):
        return "sse"
    return ""


@dataclass
class SseReader:
    """Сборка событий SSE построчно.

    Мелочи, на которых ломаются наивные разборы: строка, начинающаяся с
    двоеточия, — это комментарий (обычное написание heartbeat), и до
    `json.loads` доходить не должна; после `data:` снимается ровно один
    пробел, а не все; событие отдаётся по пустой строке; несколько строк
    `data:` склеиваются переводом строки.
    """

    data: list[str] = field(default_factory=list)
    last_id: str = ""

    def push(self, line: str) -> list[str]:
        """Скормить строку; вернуть готовые события (обычно ноль или одно)."""
        line = line.rstrip("\r")
        if not line:
            return self.flush()
        if line.startswith(":"):
            return []
        field_name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            self.data.append(value)
        elif field_name == "id":
            # Пригодится, если сервер оборвёт поток: с этим номером можно
            # продолжить с места обрыва, а не пересчитывать поиск заново.
            self.last_id = value
        return []

    def flush(self) -> list[str]:
        if not self.data:
            return []
        payload = "\n".join(self.data)
        self.data = []
        return [payload] if payload else []


def sse_messages(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Сообщения JSON-RPC из потока SSE. Нечитаемое событие пропускается:
    сервер имеет право слать своё, и падать на этом нельзя."""
    reader = SseReader()
    for line in lines:
        for payload in reader.push(line):
            try:
                yield json.loads(payload)
            except ValueError:
                log.debug("Событие SSE не разобралось: %.120s", payload)
    for payload in reader.flush():
        try:
            yield json.loads(payload)
        except ValueError:
            log.debug("Хвост SSE не разобрался: %.120s", payload)


def is_server_request(message: Mapping[str, Any]) -> bool:
    """Запрос от сервера (есть и `id`, и `method`) требует ответа.

    Уведомление (`method` без `id`) можно не замечать, запрос — нельзя:
    самый частый здесь `ping`, и молчание на него сервер вправе считать
    мёртвым соединением и оборвать поток посреди поиска.
    """
    return "id" in message and "method" in message


def server_reply(message: Mapping[str, Any]) -> dict[str, Any]:
    if message.get("method") == "ping":
        return {"jsonrpc": "2.0", "id": message["id"], "result": {}}
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "error": {"code": -32601, "message": f"Метод {message.get('method')!r} не поддержан"},
    }


def rpc_error(payload: Any) -> str:
    """Человеческий текст из тела ошибки JSON-RPC, если оно там есть."""
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            text = error.get("message") or "без объяснения"
            return f"{text} (код {code})" if code is not None else str(text)
    return ""


def pick_result(messages: Sequence[Mapping[str, Any]], want_id: int) -> dict[str, Any]:
    """Ответ на наш запрос среди всего, что пришло.

    Сравнение по `id` строгое и по типу: мы отправили число, и строка «3» —
    это чужое сообщение, а не наше.
    """
    for message in messages:
        if message.get("id") != want_id or "method" in message:
            continue
        if "error" in message:
            raise McpError(rpc_error(message) or "сервер вернул ошибку без текста")
        if "result" in message:
            return dict(message["result"])
    raise McpError(
        "Поток закончился, а ответа на запрос так и не пришло"
        if messages
        else "Сервер не прислал ни одного сообщения"
    )


def tool_content(result: Mapping[str, Any]) -> Any:
    """Полезная нагрузка вызова инструмента.

    Порядок предпочтений: `structuredContent` (если инструмент объявил
    outputSchema), иначе первый текстовый блок, разобранный как JSON, иначе
    сам список блоков. `isError` — это не ошибка транспорта, а ответ
    инструмента, и текст оттуда куда полезнее «вызов не удался».
    """
    if result.get("isError"):
        blocks = result.get("content") or []
        text = next(
            (b.get("text", "") for b in blocks if isinstance(b, Mapping) and b.get("text")),
            "",
        )
        raise McpError(f"Инструмент ответил ошибкой: {text or 'без текста'}")
    if isinstance(result.get("structuredContent"), Mapping):
        return result["structuredContent"]
    for block in result.get("content") or []:
        if isinstance(block, Mapping) and block.get("type") == "text":
            try:
                return json.loads(block.get("text", ""))
            except ValueError:
                continue
    return result.get("content", [])


# --- транспорт --------------------------------------------------------------


@dataclass
class McpClient:
    url: str
    token: str
    user_agent: str = f"{CLIENT_NAME}/{CLIENT_VERSION}"
    deadline: float = DEADLINE
    timeout: httpx.Timeout = field(default_factory=lambda: TIMEOUT)
    session: str = ""
    protocol: str = ""
    # Подставной транспорт для тестов: рукопожатие, заголовки и разбор SSE
    # иначе проверить нечем — до живого сервера из проверки не достучаться.
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _next_id: int = field(default=0, init=False, repr=False)

    async def __aenter__(self) -> "McpClient":
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
            # Переадресацию не ходим: httpx снимает Authorization при переходе
            # на другой origin, и ответ 401 после этого выглядит как истёкший
            # токен. Обновление по такому «истечению» сжигает refresh-токен
            # каждый раз, а причина — лишний слэш в адресе.
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, handshake: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": ACCEPT,
            "User-Agent": self.user_agent,
        }
        # На самом initialize договариваться ещё не о чем: версия там идёт в
        # теле запроса, а заголовком — только на последующих.
        if not handshake:
            if self.protocol:
                headers["MCP-Protocol-Version"] = self.protocol
            if self.session:
                headers["Mcp-Session-Id"] = self.session
        return headers

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(
        self, payload: Mapping[str, Any], *, want_id: int | None, handshake: bool = False
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("McpClient используется вне `async with`")
        client = self._client
        async with client.stream(
            "POST", self.url, json=dict(payload), headers=self._headers(handshake)
        ) as response:
            got = response.headers.get("mcp-session-id")
            if got:
                self.session = got
            if response.status_code >= 400:
                await response.aread()
                raise self._http_error(response)
            if response.is_redirect:
                # За переадресацией не идём (см. follow_redirects), но и
                # молчать нельзя: «непонятный тип ответа» на пустом теле
                # 307 уводит от причины — лишнего слэша в адресе.
                raise McpError(
                    f"HTTP {response.status_code}: переадресация на "
                    f"{response.headers.get('location', '—')}. За ней не идём: "
                    "httpx снимает Authorization при смене хоста, и ответ "
                    "оттуда выглядит как истёкший токен. Поправьте адрес."
                )
            if want_id is None:
                # Уведомление. Спецификация обещает 202 с пустым телом, но
                # требовать это с чужого сервера не наше дело: важно лишь,
                # что это не ошибка.
                return {}
            kind = content_kind(response.headers.get("content-type", ""))
            if kind == "json":
                # Без явного чтения httpx в потоковом режиме бросает
                # ResponseNotRead на .content — это не теория, это падение.
                await response.aread()
                body = json.loads(response.content or b"{}")
                messages = body if isinstance(body, list) else [body]
                return pick_result(messages, want_id)
            if kind == "sse":
                return await self._read_stream(response, want_id)
            raise McpError(
                f"Непонятный тип ответа: {response.headers.get('content-type', '—')!r}"
            )

    async def _read_stream(self, response: httpx.Response, want_id: int) -> dict[str, Any]:
        reader = SseReader()
        messages: list[dict[str, Any]] = []
        answered: list[asyncio.Task[None]] = []
        async for line in response.aiter_lines():
            for payload in reader.push(line):
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if is_server_request(message):
                    answered.append(asyncio.create_task(self._answer(message)))
                    continue
                messages.append(message)
                if message.get("id") == want_id and "method" not in message:
                    for task in answered:
                        task.cancel()
                    return pick_result(messages, want_id)
        for task in answered:
            task.cancel()
        # Поток кончился, ответа нет. Молча вернуть пустоту нельзя: это
        # выглядит как «поиск ничего не нашёл», а на деле оборвалось.
        return pick_result(messages, want_id)

    async def _answer(self, message: Mapping[str, Any]) -> None:
        try:
            await self._send(server_reply(message), want_id=None)
        except (McpError, httpx.HTTPError) as exc:
            log.debug("Не ответил на %s: %s", message.get("method"), exc)

    def _http_error(self, response: httpx.Response) -> McpError:
        """Сначала тело, потом код: у JSON-RPC там настоящая причина."""
        detail = ""
        if content_kind(response.headers.get("content-type", "")) == "json":
            try:
                detail = rpc_error(json.loads(response.content or b"{}"))
            except ValueError:
                detail = ""
        where = f"HTTP {response.status_code}"
        text = f"{where}: {detail}" if detail else where
        if response.status_code == 401:
            return McpUnauthorized(text)
        if response.status_code == 404 and self.session:
            return McpError(f"{text} — сессия закрыта сервером")
        return McpError(text)

    async def start(self) -> None:
        """Рукопожатие: initialize, затем обязательное уведомление."""
        want_id = self._take_id()
        result = await self._send(
            {
                "jsonrpc": "2.0",
                "id": want_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
            },
            want_id=want_id,
            handshake=True,
        )
        agreed = str(result.get("protocolVersion") or "")
        if agreed != PROTOCOL:
            raise McpError(
                f"Сервер говорит на ревизии {agreed or '—'}, а клиент понимает "
                f"только {PROTOCOL}. Слепо согласиться нельзя: в следующих "
                "ревизиях оборванный поток нужно продолжать через GET, и без "
                "этого пустой ответ не отличить от потерянного."
            )
        self.protocol = agreed
        log.debug("MCP %s, сессия %s", agreed, self.session or "без сессии")
        await self._send(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, want_id=None
        )

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        want_id = self._take_id()
        async with asyncio.timeout(self.deadline):
            result = await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": want_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": dict(arguments)},
                },
                want_id=want_id,
            )
        return tool_content(result)

    async def list_tools(self) -> list[dict[str, Any]]:
        want_id = self._take_id()
        result = await self._send(
            {"jsonrpc": "2.0", "id": want_id, "method": "tools/list", "params": {}},
            want_id=want_id,
        )
        return list(result.get("tools") or [])
