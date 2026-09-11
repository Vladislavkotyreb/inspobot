/**
 * Ретранслятор для кнопок «Топ-10» — Cloudflare Worker, бесплатный тариф.
 *
 * Зачем он нужен. Дайджест живёт в GitHub Actions и между запусками не
 * существует, а нажатие на кнопку в Telegram должно кто-то принять. Этот
 * скрипт и есть тот «кто-то»: получил нажатие → ответил «собираю» →
 * дёрнул GitHub, чтобы Actions прислал топ. Сам ничего не считает и не
 * хранит — примерно тридцать строк логики.
 *
 * Переменные окружения воркера (Settings → Variables and Secrets):
 *   TELEGRAM_BOT_TOKEN       — чтобы ответить на нажатие
 *   TELEGRAM_WEBHOOK_SECRET  — любая длинная строка; та же указывается
 *                              при setWebhook, чтобы чужие POST-запросы
 *                              не считались нажатиями
 *   GITHUB_TOKEN             — fine-grained token с правом Contents: write
 *                              на репозиторий inspobot (repository_dispatch
 *                              требует именно его)
 *   GITHUB_REPO              — "Vladislavkotyreb/inspobot"
 *   ALLOWED_CHATS            — необязательно: id чатов через запятую;
 *                              нажатия из других чатов игнорируются
 *
 * Установка и подключение вебхука — docs/TOP.md.
 */

const PERIODS = { "top:week": "top-week", "top:month": "top-month" };

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("inspobot relay", { status: 200 });
    }

    // Telegram присылает секрет в заголовке — так отсекается всё, что не от него.
    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.TELEGRAM_WEBHOOK_SECRET || secret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    // Интересны только нажатия на кнопки; всё остальное подтверждаем и забываем.
    const query = update.callback_query;
    if (!query) {
      return new Response("ok");
    }

    const chatId = String(query.message?.chat?.id ?? "");
    const eventType = PERIODS[query.data ?? ""];

    if (!eventType) {
      await answer(env, query.id, "Кнопка устарела — дождитесь свежей подборки");
      return new Response("ok");
    }
    if (!allowed(env, chatId)) {
      await answer(env, query.id, "Этот бот отвечает другому чату");
      return new Response("ok");
    }

    const dispatched = await dispatch(env, eventType, chatId);
    await answer(
      env,
      query.id,
      dispatched
        ? "Собираю топ — придёт примерно через минуту"
        : "Не получилось запустить сборку, попробуйте позже"
    );
    return new Response("ok");
  },
};

function allowed(env, chatId) {
  if (!env.ALLOWED_CHATS) return true;
  return env.ALLOWED_CHATS.split(",").map((s) => s.trim()).includes(chatId);
}

async function answer(env, callbackId, text) {
  // Без ответа Telegram крутит на кнопке спиннер до таймаута.
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ callback_query_id: callbackId, text }),
  });
}

async function dispatch(env, eventType, chatId) {
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "inspobot-relay",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: eventType, client_payload: { chat_id: chatId } }),
  });
  // GitHub отвечает 204 без тела; всё остальное — отказ.
  return response.status === 204;
}
