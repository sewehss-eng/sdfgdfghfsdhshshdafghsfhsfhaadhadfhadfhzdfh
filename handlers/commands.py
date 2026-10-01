"""Базовые команды: /start, /help, /tasks."""

from __future__ import annotations

import logging
import os
import sys

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
    "• /clone — клонировать одну или несколько папок/файлов\n"
    "  Массовый формат, каждая папка с новой строки:\n"
    "  <code>ссылка интервал исключения удаление категория</code>\n"
    "  Пример: <code>https://drive.google.com/drive/folders/ID 30мин *.mp4,*.zip + Курсы 2026</code>\n"
    "  Интервал: <code>30мин</code>, <code>2ч</code>, <code>1д</code>; исключения <code>-</code>, если их нет; "
    "удаление <code>+</code> — зеркалировать удаления, <code>-</code> — не удалять; "
    "категория необязательна и может содержать пробелы\n"
    "• /new_task — создать связку «источник → назначение» вручную (мастер из 5 шагов)\n"
    "• /new_folder — создать одну или несколько папок по источникам; старый формат /new_folder Название тоже поддерживается\n"
    "• /tasks — удобный список задач по категориям\n"
    "• /folders — проводник: просмотр, загрузка файлов, создание папок, поиск, перемещение, переименование и удаление\n"
    "• /cancel — отменить текущее действие. ⏸ Пауза: остановить автосинхронизацию по интервалу и возобновить в любой момент — настройки, журнал и файлы сохраняются; ручной запуск работает и на паузе\n"
    "• /restart — полностью перезапустить бота\n\n"
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


@router.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    """Перезапускает текущий Python-процесс; внешний менеджер поднимет его снова."""
    await message.answer("🔄 Перезапускаю бота…")
    await message.bot.session.close()
    os.execl(sys.executable, sys.executable, *sys.argv)
