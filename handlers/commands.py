"""Базовые команды: /start, /help, /tasks."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from deps import get_deps
from handlers.tasks import send_tasks_menu

router = Router(name="commands")
log = logging.getLogger(__name__)

START_TEXT = (
    "👋 <b>Привет!</b>\n\n"
    "Я слежу за обновлениями папок в Google Drive и автоматически копирую новые и "
    "изменённые файлы в вашу папку-назначение. Копирование выполняется на серверах "
    "Google (files.copy), поэтому трафик не идёт через мой хостинг.\n\n"
    "<b>Что я умею:</b>\n"
    "• /clone — прислать ссылку на чужой файл/папку — я сам скопирую её к себе на диск и пришлю готовую ссылку (для папки ещё и автосинхронизацию включу; по ходу можно указать файлы-исключения, которые копировать не нужно)\n"
    "• /new_task — создать связку «источник → назначение» вручную (мастер из 5 шагов)\n"
    "• /new_folder Название — создать папку сразу, а источник подключить позже\n"
    "• /tasks — удобный список задач по категориям\n"
    "• /folders — отдельный проводник: просмотр, открытие, переименование и удаление папок/файлов\n"
    "• /cancel — отменить текущее действие. ⏸ Пауза: остановить автосинхронизацию по интервалу и возобновить в любой момент — настройки, журнал и файлы сохраняются; ручной запуск работает и на паузе\n\n"
    "💡 Ссылку на чужую публичную папку можно вставить целиком — я сам извлеку Folder ID."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    await send_tasks_menu(message, user_id)
