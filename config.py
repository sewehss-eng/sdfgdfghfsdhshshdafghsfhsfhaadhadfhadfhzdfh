"""Загрузка конфигурации из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent
# .env ищем рядом с проектом — не зависит от текущей директории запуска
load_dotenv(_BASE_DIR / ".env")


@dataclass(frozen=True)
class Config:
    """Иммутабельная конфигурация приложения."""

    bot_token: str
    admin_user_id: int
    database_path: str
    credentials_file: str
    token_file: str
    scheduler_tick_sec: int
    drive_max_retries: int
    log_level: str
    # Фрагменты, которые вырезаются из имён файлов при копировании/загрузке.
    # Задаются в .env через запятую: NAME_STRIP_FRAGMENTS=@site.ru,[копия],скачано с
    name_strip_fragments: tuple[str, ...] = ()
    # Глобальные маски файлов, которые никогда не заливаются на диск (дополнение к дефолтным).
    # Задаются в .env через запятую: GLOBAL_EXCLUDE_PATTERNS=*.torrent,spam_*
    global_exclude_patterns: tuple[str, ...] = ()


def load_config() -> Config:
    """Читает .env и собирает Config с дефолтами."""
    return Config(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        # Один владелец по умолчанию. При необходимости можно переопределить в .env.
        admin_user_id=int(os.getenv("ADMIN_USER_ID", "8380347640")),
        database_path=os.getenv("DATABASE_PATH", "data/bot.db").strip() or "data/bot.db",
        credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json").strip()
        or "credentials.json",
        token_file=os.getenv("GOOGLE_TOKEN_FILE", "token.json").strip() or "token.json",
        scheduler_tick_sec=max(30, int(os.getenv("SCHEDULER_TICK_SEC", "60"))),
        drive_max_retries=max(1, int(os.getenv("DRIVE_MAX_RETRIES", "5"))),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        name_strip_fragments=_parse_fragments(os.getenv("NAME_STRIP_FRAGMENTS", "")),
        global_exclude_patterns=_parse_fragments(os.getenv("GLOBAL_EXCLUDE_PATTERNS", "")),
    )


def _parse_fragments(raw: str) -> tuple[str, ...]:
    """Разбирает список фрагментов для удаления из имён.

    Разделители: запятая или перевод строки. Пустые элементы отбрасываются.
    Порядок сохраняется, повторы удаляются.
    """
    seen: dict[str, None] = {}
    for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk and chunk not in seen:
            seen[chunk] = None
    return tuple(seen)


config: Config = load_config()
