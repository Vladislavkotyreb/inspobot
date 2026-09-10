"""Конфигурация: читается из окружения и (если есть) из файла .env рядом с проектом."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MCP_URL = "https://api.mobbin.com/mcp"


def load_dotenv(path: Path | None = None) -> None:
    """Минимальный загрузчик .env: KEY=VALUE, без экспорта и подстановок.

    Уже выставленные переменные окружения имеют приоритет — на хостинге удобнее
    задавать секреты в панели, а .env держать для локального запуска.
    """
    path = path or BASE_DIR / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    anthropic_api_key: str
    anthropic_workspace_id: str
    model: str
    effort: str
    max_tokens: int
    server_fallbacks: bool
    mobbin_mcp_url: str
    mobbin_token_file: Path
    mobbin_access_token: str
    db_path: Path
    profile_path: Path
    timezone: str
    hour: int
    minute: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        var_dir = Path(os.environ.get("INSPOBOT_VAR_DIR", str(BASE_DIR / "var")))
        return cls(
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            anthropic_workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip(),
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5").strip(),
            effort=os.environ.get("INSPOBOT_EFFORT", "high").strip(),
            max_tokens=_int("INSPOBOT_MAX_TOKENS", 32000),
            server_fallbacks=_bool("ANTHROPIC_SERVER_FALLBACKS", True),
            mobbin_mcp_url=os.environ.get("MOBBIN_MCP_URL", DEFAULT_MCP_URL).strip(),
            mobbin_token_file=Path(
                os.environ.get("MOBBIN_TOKEN_FILE", str(var_dir / "mobbin_token.json"))
            ),
            mobbin_access_token=os.environ.get("MOBBIN_ACCESS_TOKEN", "").strip(),
            db_path=Path(os.environ.get("INSPOBOT_DB", str(var_dir / "inspobot.sqlite3"))),
            profile_path=Path(
                os.environ.get("INSPOBOT_PROFILE", str(var_dir / "profile.json"))
            ),
            timezone=os.environ.get("INSPOBOT_TZ", "Europe/Moscow").strip(),
            hour=_int("INSPOBOT_HOUR", 11),
            minute=_int("INSPOBOT_MINUTE", 0),
        )

    def require(self, *names: str) -> None:
        """Падать на старте с внятным списком, а не на первом же запросе."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(
                "Не заданы обязательные переменные окружения: "
                + ", ".join(sorted({_ENV_NAMES.get(n, n) for n in missing}))
                + ". См. .env.example"
            )


_ENV_NAMES = {
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
}
