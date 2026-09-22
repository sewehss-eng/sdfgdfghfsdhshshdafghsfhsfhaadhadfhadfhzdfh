"""Утилиты: HTML-экранирование, парсинг/форматирование интервалов, длинные сообщения."""

from __future__ import annotations

import asyncio
import fnmatch
import html
import random
import re
from datetime import datetime, timezone

from aiogram import Bot

MAX_MESSAGE_LEN = 4000  # с запасом до лимита Telegram в 4096 символов

MIN_INTERVAL_SEC = 60        # минимум по ТЗ
MAX_INTERVAL_SEC = 30 * 86400  # максимум — 30 дней


def esc(value: object) -> str:
    """Экранирует текст для HTML-parse mode Telegram."""
    return html.escape(str(value), quote=False)


# Невидимые символы Unicode: в Google Drive имя выглядит как обычное,
# но каждая комбинация — отдельное имя папки.
_INVISIBLE_MARKS = ("\u200b", "\u200c", "\u200d", "\ufeff")


def unique_clone_name(name: str, count: int = 3) -> str:
    """Добавляет к имени случайные невидимые символы, чтобы копия не сливалась с уже существующей папкой."""
    suffix = "".join(random.choice(_INVISIBLE_MARKS) for _ in range(count))
    return f"{name}{suffix}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def format_dt_short(iso: str | None) -> str:
    """ISO-строка -> '2026-09-12 21:56 UTC'."""
    if not iso:
        return "—"
    return iso[:16].replace("T", " ") + " UTC"


_INTERVAL_UNITS: dict[str, int] = {
    "": 1, "с": 1, "сек": 1, "секунд": 1, "sec": 1, "secs": 1, "s": 1,
    "м": 60, "мин": 60, "минут": 60, "m": 60, "min": 60, "mins": 60,
    "ч": 3600, "час": 3600, "часов": 3600, "h": 3600, "hr": 3600, "hrs": 3600,
    "д": 86400, "дн": 86400, "день": 86400, "дней": 86400, "d": 86400,
}


def parse_interval(raw: str) -> int | None:
    """Парсит интервал: '90', '90с', '30 мин', '2ч', '1 день'.

    Возвращает количество секунд либо None, если формат неверен
    или значение вне диапазона [MIN_INTERVAL_SEC, MAX_INTERVAL_SEC].
    """
    match = re.fullmatch(r"(\d+)\s*([a-zA-Zа-яА-ЯёЁ]*)", raw.strip().lower())
    if not match:
        return None
    value = int(match.group(1))
    multiplier = _INTERVAL_UNITS.get(match.group(2))
    if multiplier is None:
        return None
    seconds = value * multiplier
    if not (MIN_INTERVAL_SEC <= seconds <= MAX_INTERVAL_SEC):
        return None
    return seconds


def format_interval(seconds: int) -> str:
    """Красиво форматирует секунды: '1 час', '30 мин', '90 сек'."""
    if seconds % 86400 == 0:
        n = seconds // 86400
        return f"{n} дн" if n > 1 else "1 день"
    if seconds % 3600 == 0:
        n = seconds // 3600
        return f"{n} ч" if n > 1 else "1 час"
    if seconds % 60 == 0:
        n = seconds // 60
        return f"{n} мин" if n > 1 else "1 мин"
    return f"{seconds} сек"


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    """Отправляет текст любого размера, разрезая его по строкам под лимит Telegram."""
    if len(text) <= MAX_MESSAGE_LEN:
        await bot.send_message(chat_id, text)
        return

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        # отдельно режем сверхдлинные строки без переносов
        while len(line) > MAX_MESSAGE_LEN:
            chunks.append(line[:MAX_MESSAGE_LEN])
            line = line[MAX_MESSAGE_LEN:]
        if len(current) + len(line) > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)

    for chunk in chunks:
        await bot.send_message(chat_id, chunk)
        await asyncio.sleep(0.05)  # мягкая защита от флуд-лимита Telegram


# ------------------------------------------------------------------ исключения

def split_exclude_patterns(raw: str) -> list[str]:
    """Разбивает строку исключений на маски (разделители: запятая или новая строка)."""
    patterns: list[str] = []
    for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            patterns.append(chunk)
    return patterns


def is_excluded(name: str, patterns_raw: str) -> bool:
    """True, если имя файла/папки подпадает под список исключений связки.

    Маски задаются через запятую: точное имя («отчёт.docx») или шаблон
    с подстановочными знаками * и ? («*.mp4», «черновик*»). Регистр не важен.
    """
    if not patterns_raw:
        return False
    lowered = name.lower()
    for pattern in split_exclude_patterns(patterns_raw):
        p = pattern.lower()
        if "*" in p or "?" in p:
            if fnmatch.fnmatchcase(lowered, p):
                return True
        elif lowered == p:
            return True
    return False
