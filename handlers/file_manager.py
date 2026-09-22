"""Отдельный Telegram-проводник для управления файлами Google Drive."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
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
    new_folder = State()
    search = State()
    move_pick = State()


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
    rows.append([
        InlineKeyboardButton(text="➕ Папка", callback_data=f"fm:mkdir:{folder_id}"),
        InlineKeyboardButton(text="🔍 Поиск", callback_data=f"fm:find:{folder_id}"),
    ])
    rows.append([InlineKeyboardButton(
        text="◀️ Назад" if parent_id else "🏠 Обновить",
        callback_data=f"fm:open:{parent_id}" if parent_id else "fm:root",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _parent_id(item: dict) -> str:
    parents = item.get("parents") or []
    return str(parents[0]) if parents else "root"


async def _show_folder(message: Message, folder_id: str, state: FSMContext | None = None) -> None:
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

        if state is not None:
            await state.update_data(fm_upload_parent=folder_id)
        await message.edit_text(
            title + f"\nЭлементов: {len(items)}\n\nВыберите папку или файл. "
            "Файл, присланный в чат, загрузится сюда.",
            reply_markup=_folder_keyboard(items, folder_id, parent_id),
        )
    except DriveError as exc:
        await message.edit_text(f"⚠️ Не удалось открыть папку: {esc(exc)}")


@router.message(Command("folders"))
async def cmd_folders(message: Message, state: FSMContext) -> None:
    await state.update_data(fm_upload_parent="root")
    deps = get_deps()
    try:
        items = await deps.drive.list_folder("root")
        await message.answer(
            "📂 <b>Проводник Google Drive</b>\n"
            f"Элементов в корне: {len(items)}\n\n"
            "Выберите папку или файл. Чтобы загрузить файл — пришлите его прямо в этот чат, "
            "он попадёт в корень диска.",
            reply_markup=_folder_keyboard(items, "root", None),
        )
    except DriveError as exc:
        await message.answer(f"⚠️ Не удалось открыть Google Drive: {esc(exc)}")


@router.callback_query(F.data == "fm:noop")
async def fm_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "fm:root")
async def fm_root(cb: CallbackQuery, state: FSMContext) -> None:
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, "root", state)
    await cb.answer()


@router.callback_query(F.data.startswith("fm:open:"))
async def fm_open(cb: CallbackQuery, state: FSMContext) -> None:
    folder_id = (cb.data or "").split(":", 2)[2]
    if not folder_id:
        await cb.answer("Папка не найдена", show_alert=True)
        return
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, folder_id, state)
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
        [InlineKeyboardButton(text="📦 Переместить", callback_data=f"fm:move:{item_id}")],
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
async def fm_back(cb: CallbackQuery, state: FSMContext) -> None:
    folder_id = (cb.data or "").split(":", 2)[2] or "root"
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, folder_id, state)
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
async def fm_delete_yes(cb: CallbackQuery, state: FSMContext) -> None:
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
        await _show_folder(cb.message, parent_id, state)
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
    await _send_folder(message, parent_id)


async def _send_folder(message: Message, folder_id: str) -> None:
    """Отправляет новое сообщение со списком папки (после текстового ответа)."""
    deps = get_deps()
    try:
        if folder_id == "root":
            items = await deps.drive.list_folder("root")
            title = "🏠 <b>Мой Google Drive</b>"
            parent_id = None
        else:
            meta = await deps.drive.get_meta(folder_id, fields="id,name,mimeType,parents")
            if not meta:
                await message.answer("Папка недоступна.")
                return
            items = await deps.drive.list_folder(folder_id)
            title = f"📁 <b>{esc(str(meta.get('name', folder_id)))}</b>"
            parent_id = await _parent_id(meta)
        await message.answer(
            title + f"\nЭлементов: {len(items)}",
            reply_markup=_folder_keyboard(items, folder_id, parent_id),
        )
    except DriveError as exc:
        await message.answer(f"⚠️ Не удалось открыть папку: {esc(exc)}")


# Telegram ограничивает входящий файл через Bot API ~20 МБ.
_MAX_UPLOAD = 20 * 1024 * 1024


def _upload_name(message: Message) -> tuple[str, str, int] | None:
    """Имя и mime файла из сообщения Telegram. None, если это не файл."""
    if message.document:
        return (
            message.document.file_name or "file",
            message.document.mime_type or "application/octet-stream",
            message.document.file_size or 0,
        )
    if message.photo:
        photo = message.photo[-1]
        return "photo.jpg", "image/jpeg", photo.file_size or 0
    if message.video:
        return (
            message.video.file_name or "video.mp4",
            message.video.mime_type or "video/mp4",
            message.video.file_size or 0,
        )
    if message.audio:
        return (
            message.audio.file_name or "audio",
            message.audio.mime_type or "audio/mpeg",
            message.audio.file_size or 0,
        )
    if message.voice:
        return "voice.ogg", "audio/ogg", message.voice.file_size or 0
    return None


def _telegram_file(message: Message):
    if message.document:
        return message.document
    if message.photo:
        return message.photo[-1]
    if message.video:
        return message.video
    if message.audio:
        return message.audio
    if message.voice:
        return message.voice
    return None


@router.message(FileManagerStates.new_folder)
async def fm_new_folder_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    parent_id = str(data.get("fm_parent_id", "root"))
    name = (message.text or "").strip()
    if not name or len(name) > 255:
        await message.answer("Имя папки должно быть от 1 до 255 символов.")
        return
    deps = get_deps()
    try:
        await deps.drive.create_folder(name, parent_id)
    except DriveError as exc:
        await message.answer(f"⚠️ Не удалось создать папку: {esc(exc)}")
        return
    await state.clear()
    await message.answer(f"✅ Папка «{esc(name)}» создана.")
    await _send_folder(message, parent_id)


@router.message(FileManagerStates.search)
async def fm_search_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    parent_id = str(data.get("fm_parent_id", "root"))
    query = (message.text or "").strip()
    if not query or len(query) > 100:
        await message.answer("Введите запрос от 1 до 100 символов.")
        return
    deps = get_deps()
    try:
        items = await deps.drive.search(query, parent_id)
    except DriveError as exc:
        await message.answer(f"⚠️ Поиск не удался: {esc(exc)}")
        return
    await state.clear()
    if not items:
        await message.answer(f"Ничего не найдено по «{esc(query)}».")
        await _send_folder(message, parent_id)
        return
    rows: list[list[InlineKeyboardButton]] = []
    for item in items[:20]:
        item_id = str(item.get("id", ""))
        name = str(item.get("name", item_id))
        mime = str(item.get("mimeType", ""))
        prefix = "📁" if mime == FOLDER_MIME else "📄"
        action = "open" if mime == FOLDER_MIME else "item"
        rows.append([InlineKeyboardButton(
            text=f"{prefix} {_short_name(name)}",
            callback_data=f"fm:{action}:{item_id}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ К папке", callback_data=f"fm:open:{parent_id}")])
    await message.answer(
        f"🔍 «{esc(query)}»: {len(items)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(F.document | F.photo | F.video | F.audio | F.voice, StateFilter(None))
async def fm_upload(message: Message, state: FSMContext) -> None:
    """Файл, присланный без активного мастера, загружается в открытую папку или в корень."""
    named = _upload_name(message)
    tg_file = _telegram_file(message)
    if named is None or tg_file is None:
        return
    name, mime, size = named
    if size > _MAX_UPLOAD:
        await message.answer("Файл больше 20 МБ — Telegram не отдаёт такие файлы боту.")
        return
    data = await state.get_data()
    parent_id = str(data.get("fm_upload_parent", "root")) or "root"
    name, mime = named
    deps = get_deps()
    status = await message.answer(f"⏳ Загружаю «{esc(name)}» на Google Drive…")
    try:
        downloaded = await deps.bot.download(tg_file)
        if downloaded is None:
            await status.edit_text("⚠️ Telegram не отдал файл.")
            return
        content = downloaded.read()
        await deps.drive.upload_bytes(name, mime, parent_id, content)
    except DriveError as exc:
        await status.edit_text(f"⚠️ Не удалось загрузить: {esc(exc)}")
        return
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(f"⚠️ Ошибка загрузки: {esc(exc)}")
        return
    where = "корень диска" if parent_id == "root" else "текущую папку"
    await status.edit_text(f"✅ «{esc(name)}» загружен в {where}.")
    await _send_folder(message, parent_id)


@router.callback_query(F.data.startswith("fm:mkdir:"))
async def fm_mkdir_start(cb: CallbackQuery, state: FSMContext) -> None:
    parent_id = (cb.data or "").split(":", 2)[2] or "root"
    await state.update_data(fm_parent_id=parent_id, fm_upload_parent=parent_id)
    await state.set_state(FileManagerStates.new_folder)
    if isinstance(cb.message, Message):
        await cb.message.edit_text("➕ Пришлите имя новой папки. Отмена: /cancel")
    await cb.answer()


@router.callback_query(F.data.startswith("fm:find:"))
async def fm_find_start(cb: CallbackQuery, state: FSMContext) -> None:
    parent_id = (cb.data or "").split(":", 2)[2] or "root"
    await state.update_data(fm_parent_id=parent_id, fm_upload_parent=parent_id)
    await state.set_state(FileManagerStates.search)
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            "🔍 Пришлите часть имени. Ищу в этой папке"
            + (" (корень диска)." if parent_id == "root" else ".")
            + " Отмена: /cancel"
        )
    await cb.answer()


@router.callback_query(F.data.startswith("fm:move:"))
async def fm_move_start(cb: CallbackQuery, state: FSMContext) -> None:
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
    await state.update_data(fm_move_id=item_id, fm_move_from=await _parent_id(item))
    await state.set_state(FileManagerStates.move_pick)
    if isinstance(cb.message, Message):
        await _show_move_picker(cb.message, "root", state)
    await cb.answer()


async def _show_move_picker(message: Message, folder_id: str, state: FSMContext) -> None:
    deps = get_deps()
    data = await state.get_data()
    moving_id = str(data.get("fm_move_id", ""))
    try:
        items = await deps.drive.list_folder(folder_id)
    except DriveError as exc:
        await message.edit_text(f"⚠️ Не удалось открыть папку: {esc(exc)}")
        return
    if folder_id == "root":
        title = "📦 Куда переместить? Сейчас: корень"
        parent_id = None
    else:
        meta = await deps.drive.get_meta(folder_id, fields="id,name,parents")
        title = f"📦 Куда переместить? Сейчас: {esc(str((meta or {}).get('name', folder_id)))}"
        parent_id = await _parent_id(meta) if meta else "root"
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        if str(item.get("mimeType")) != FOLDER_MIME:
            continue
        child_id = str(item.get("id", ""))
        if child_id == moving_id:
            continue
        rows.append([InlineKeyboardButton(
            text=f"📁 {_short_name(str(item.get('name', child_id)))}",
            callback_data=f"fm:mp:{child_id}",
        )])
    rows.append([InlineKeyboardButton(text="✅ Сюда", callback_data=f"fm:here:{folder_id}")])
    if parent_id:
        rows.append([InlineKeyboardButton(text="◀️ Наверх", callback_data=f"fm:mp:{parent_id}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fm:mcancel")])
    await message.edit_text(title, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(FileManagerStates.move_pick, F.data.startswith("fm:mp:"))
async def fm_move_browse(cb: CallbackQuery, state: FSMContext) -> None:
    folder_id = (cb.data or "").split(":", 2)[2] or "root"
    if isinstance(cb.message, Message):
        await _show_move_picker(cb.message, folder_id, state)
    await cb.answer()


@router.callback_query(FileManagerStates.move_pick, F.data.startswith("fm:here:"))
async def fm_move_here(cb: CallbackQuery, state: FSMContext) -> None:
    dest_id = (cb.data or "").split(":", 2)[2] or "root"
    data = await state.get_data()
    item_id = str(data.get("fm_move_id", ""))
    old_parent = str(data.get("fm_move_from", "root"))
    if not item_id:
        await state.clear()
        await cb.answer("Операция устарела", show_alert=True)
        return
    if dest_id == item_id:
        await cb.answer("Нельзя переместить папку в саму себя", show_alert=True)
        return
    if dest_id == old_parent:
        await state.set_state(None)
        await state.update_data(fm_upload_parent=old_parent)
        if isinstance(cb.message, Message):
            await _show_folder(cb.message, old_parent, state)
        await cb.answer("Уже лежит здесь")
        return
    deps = get_deps()
    try:
        await deps.drive.move_file(item_id, dest_id, old_parent)
    except DriveError as exc:
        await cb.answer(f"Не удалось переместить: {esc(exc)}", show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(fm_upload_parent=dest_id)
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, dest_id, state)
    await cb.answer("Перемещено")


@router.callback_query(F.data == "fm:mcancel")
async def fm_move_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    parent_id = str(data.get("fm_move_from", "root"))
    await state.set_state(None)
    await state.update_data(fm_upload_parent=parent_id)
    if isinstance(cb.message, Message):
        await _show_folder(cb.message, parent_id, state)
    await cb.answer("Отменено")
