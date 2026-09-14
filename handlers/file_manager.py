"""Отдельный Telegram-проводник для управления файлами Google Drive."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from deps import get_deps
from handlers.common import folder_url
from services.drive import DriveError, FOLDER_MIME
from utils import esc

router = Router(name="file_manager")


class FileManagerStates(StatesGroup):
    rename = State()


def _short_name(name: str, limit: int = 42) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _item_url(item_id: str, mime: str) -> str:
    if mime == FOLDER_MIME:
        return folder_url(item_id)
    return f"https://drive.google.com/open?id={item_id}"


def _folder_keyboard(items: list[dict], folder_id: str, parent_id: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        item_id = str(item.get("id", ""))
        name = str(item.get("name", item_id))
        mime = str(item.get("mimeType", ""))
        if mime == FOLDER_MIME:
            rows.append([InlineKeyboardButton(
                text=f"📁 {_short_name(name)}",
                callback_data=f"fm:open:{item_id}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                text=f"📄 {_short_name(name)}",
                callback_data=f"fm:item:{item_id}",
            )])

    if not items:
        rows.append([InlineKeyboardButton(text="(папка пуста)", callback_data="fm:noop")])

    if folder_id == "root":
        rows.append([InlineKeyboardButton(text="🔗 Открыть Google Drive", url="https://drive.google.com/drive/my-drive")])
    else:
        rows.append([InlineKeyboardButton(text="🔗 Открыть в Google Drive", url=folder_url(folder_id))])
        rows.append([InlineKeyboardButton(text="🗑 Удалить эту папку", callback_data=f"fm:delete:{folder_id}")])
    rows.append([InlineKeyboardButton(
        text="◀️ Назад" if parent_id else "🏠 Обновить",
        callback_data=f"fm:open:{parent_id}" if parent_id else "fm:root",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _parent_id(item: dict) -> str:
    parents = item.get("parents") or []
    return str(parents[0]) if parents else "root"


async def _show_folder(message: Message, folder_id: str) -> None:
    deps = get_deps()
    try:
        if folder_id == "root":
            items = await deps.drive.list_folder("root")
            title = "🏠 <b>Мой Google Drive</b>"
            parent_id = None
        else:
            meta = await deps.drive.get_meta(folder_id, fields="id,name,mimeType,parents")
            if not meta or meta.get("mimeType") != FOLDER_MIME:
                await message.edit_text("Папка недоступна или уже удалена.")
                return
            items = await deps.drive.list_folder(folder_id)
            title = f"📁 <b>{esc(str(meta.get('name', folder_id)))}</b>"
            parent_id = await _parent_id(meta)

        await message.edit_text(
            title + f"\nЭлементов: {len(items)}\n\nВыберите папку или файл:",
            reply_markup=_folder_keyboard(items, folder_id, parent_id),
        )
    except DriveError as exc:
        await message.edit_text(f"⚠️ Не удалось открыть папку: {esc(exc)}")


@router.message(Command("folders"))
async def cmd_folders(message: Message) -> None:
    deps = get_deps()
    try:
        items = await deps.drive.list_folder("root")
        await message.answer(
            "📂 <b>Проводник Google Drive</b>\n"
            f"Элементов в корне: {len(items)}\n\nВыберите папку или файл:",
            reply_markup=_folder_keyboard(items, "root", None),
        )
    except DriveError as exc:
        await message.answer(f"⚠️ Не удалось открыть Google Drive: {esc(exc)}")


@router.callback_query(F.data == "fm:noop")
async def fm_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "fm:root")
async def fm_root(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, "root")
    await cb.answer()


@router.callback_query(F.data.startswith("fm:open:"))
async def fm_open(cb: CallbackQuery) -> None:
    folder_id = (cb.data or "").split(":", 2)[2]
    if not folder_id:
        await cb.answer("Папка не найдена", show_alert=True)
        return
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, folder_id)
    await cb.answer()


@router.callback_query(F.data.startswith("fm:item:"))
async def fm_item(cb: CallbackQuery) -> None:
    item_id = (cb.data or "").split(":", 2)[2]
    deps = get_deps()
    try:
        item = await deps.drive.get_meta(item_id, fields="id,name,mimeType,parents")
    except DriveError as exc:
        await cb.answer(f"Ошибка Drive: {esc(exc)}", show_alert=True)
        return
    if not item:
        await cb.answer("Файл недоступен или уже удалён", show_alert=True)
        return

    name = str(item.get("name", item_id))
    mime = str(item.get("mimeType", ""))
    parent_id = await _parent_id(item)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть", url=_item_url(item_id, mime))],
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"fm:rename:{item_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"fm:delete:{item_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"fm:back:{parent_id}")],
    ])
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            f"{'📁' if mime == FOLDER_MIME else '📄'} <b>{esc(name)}</b>\n"
            f"Тип: {esc(mime)}",
            reply_markup=keyboard,
        )
    await cb.answer()


@router.callback_query(F.data.startswith("fm:back:"))
async def fm_back(cb: CallbackQuery) -> None:
    folder_id = (cb.data or "").split(":", 2)[2] or "root"
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, folder_id)
    await cb.answer()


@router.callback_query(F.data.startswith("fm:delete:"))
async def fm_delete(cb: CallbackQuery) -> None:
    item_id = (cb.data or "").split(":", 2)[2]
    deps = get_deps()
    try:
        item = await deps.drive.get_meta(item_id, fields="id,name,mimeType,parents")
    except DriveError as exc:
        await cb.answer(f"Ошибка Drive: {esc(exc)}", show_alert=True)
        return
    if not item:
        await cb.answer("Элемент уже удалён", show_alert=True)
        return

    parent_id = await _parent_id(item)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"fm:yes:{item_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"fm:item:{item_id}"),
    ]])
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            f"🗑 Удалить «{esc(str(item.get('name', item_id)))}»?\n"
            "Если это папка, Google Drive удалит её содержимое тоже.",
            reply_markup=keyboard,
        )
    await cb.answer()


@router.callback_query(F.data.startswith("fm:yes:"))
async def fm_delete_yes(cb: CallbackQuery) -> None:
    item_id = (cb.data or "").split(":", 2)[2]
    deps = get_deps()
    try:
        item = await deps.drive.get_meta(item_id, fields="id,name,mimeType,parents")
        if not item:
            await cb.answer("Элемент уже удалён", show_alert=True)
            return
        parent_id = await _parent_id(item)
        await deps.drive.delete_file(item_id)
    except DriveError as exc:
        await cb.answer(f"Не удалось удалить: {esc(exc)}", show_alert=True)
        return

    if isinstance(cb.message, Message):
        await _show_folder(cb.message, parent_id)
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("fm:rename:"))
async def fm_rename_start(cb: CallbackQuery, state: FSMContext) -> None:
    item_id = (cb.data or "").split(":", 2)[2]
    await state.update_data(fm_item_id=item_id)
    await state.set_state(FileManagerStates.rename)
    if isinstance(cb.message, Message):
        await cb.message.edit_text("✏️ Пришлите новое имя (до 255 символов). Для отмены: /cancel")
    await cb.answer()


@router.message(FileManagerStates.rename)
async def fm_rename_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    item_id = str(data.get("fm_item_id", ""))
    name = (message.text or "").strip()
    if not item_id:
        await state.clear()
        await message.answer("Операция устарела. Откройте /folders заново.")
        return
    if not name or len(name) > 255:
        await message.answer("Имя должно быть от 1 до 255 символов.")
        return

    deps = get_deps()
    try:
        item = await deps.drive.get_meta(item_id, fields="id,name,mimeType,parents")
        if not item:
            await message.answer("Элемент уже удалён или недоступен.")
            await state.clear()
            return
        parent_id = await _parent_id(item)
        await deps.drive.rename_file(item_id, name)
    except DriveError as exc:
        await message.answer(f"⚠️ Не удалось переименовать: {esc(exc)}")
        return

    await state.clear()
    await message.answer(f"✅ Переименовано: «{esc(name)}»")
    # После текстового ответа отправляем обновлённый список той же родительской папки.
    try:
        items = await deps.drive.list_folder(parent_id)
        await message.answer(
            f"📁 Элементов в папке: {len(items)}",
            reply_markup=_folder_keyboard(items, parent_id, None if parent_id == "root" else "root"),
        )
    except DriveError as exc:
        await message.answer(f"⚠️ Не удалось обновить список: {esc(exc)}")
