"""DI-контейнер: даёт хэндлерам доступ к боту, БД, Drive и движку синхронизации."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from database.db import Database
from services.drive import DriveClient
from services.sync import SyncEngine


@dataclass
class Deps:
    bot: Bot
    db: Database
    drive: DriveClient
    engine: SyncEngine


_deps: Deps | None = None


def set_deps(deps: Deps) -> None:
    """Инициализирует зависимости. Вызывается один раз в bot.py до старта polling."""
    global _deps
    _deps = deps


def get_deps() -> Deps:
    if _deps is None:
        raise RuntimeError("Зависимости не инициализированы: сначала set_deps() в bot.py")
    return _deps
