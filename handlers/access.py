"""Фильтры доступа к боту."""

from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from config import config


class AdminOnly(Filter):
    """Пропускает апдейты только от владельца, заданного в .env."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and event.from_user.id == config.admin_user_id)
