"""FSM-мастер /new_task и все inline-обработчики карточек задач."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import Task
from deps import get_deps
from handlers.common import (
    confirm_delete_keyboard,
    folder_url,
    settings_keyboard,
    settings_text,
    task_card_text,
    task_keyboard,
)
from services.drive import DriveError, FOLDER_MIME, extract_drive_id, extract_folder_id
from services.sync import TaskAlreadyRunningError, render_report
from utils import esc, format_interval, parse_interval, send_long, split_exclude_patterns, utcnow_iso

router = Router(name="tasks")
log = logging.getLogger(__name__)

CANCEL_HINT = "\n\n<i>Отменить: /cancel</i>"

# Держим ссылки на фоновые задачи, чтобы их не съел GC до завершения
_background: set["asyncio.Task[None]"] = set()


class NewTaskStates(StatesGroup):
    """Шаги мастера создания связки (5 шагов)."""
    title = State()
    source = State()
    target = State()
    interval = State()
    notify = State()


class SettingsStates(StatesGroup):
    """Шаги изменения настроек существующей связки."""
    interval = State()
    name = State()
    source = State()
    category = State()
    template = State()
    template_delay = State()
    exclude = State()


class CloneStates(StatesGroup):
    """Шаги команды /clone: ссылка -> список исключений."""
    link = State()
    exclude = State()


# Интервал автосинхронизации, который получает связка, созданная через /clone —
# пользователь может изменить его позже через ⚙️ Настройки.
CLONE_DEFAULT_INTERVAL_SEC = 30 * 60


def spawn(coro) -> None:  # type: ignore[no-untyped-def]
    """Безопасно запускает фоновую корутину."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _parse_callback(data: str | None) -> tuple[int, str] | None:
    """'task:12:sync' -> (12, 'sync'); None при некорректном формате."""
    parts = (data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2]


async def _load_task(cb: CallbackQuery, task_id: int) -> Task | None:
    """Загружает задачу и проверяет, что она принадлежит нажавшему."""
    deps = get_deps()
    task = await deps.db.get_task(task_id)
    if task is None or task.user_id != cb.from_user.id:
        await cb.answer("Связка не найдена или принадлежит другому пользователю.",
                        show_alert=True)
        return None
    return task


def progress_text(task: Task, checked: int, changes: int, folders: int, errors: int) -> str:
    return (
        f"⏳ <b>Синхронизация «{esc(task.title)}»</b>\n"
        f"Проверено: {checked} • изменений: {changes} • новых папок: {folders}"
        + (f" • ошибок: {errors}" if errors else "")
        + "\n<i>Прогресс обновляется автоматически…</i>"
    )


async def run_manual_sync(task_id: int, chat_id: int) -> None:
    """Фоновая синхронизация с одним редактируемым сообщением прогресса."""
    deps = get_deps()
    task = await deps.db.get_task(task_id)
    if task is None:
        return
    status = await deps.bot.send_message(chat_id, progress_text(task, 0, 0, 0, 0))
    last_edit = 0.0
    last_snapshot = (-1, -1, -1, -1)

    async def show_progress(report) -> None:  # type: ignore[no-untyped-def]
        nonlocal last_edit, last_snapshot
        snapshot = (report.checked, len(report.changes), report.created_folders, len(report.errors))
        now = asyncio.get_running_loop().time()
        # Не спамим Telegram: обновление максимум раз в 2 секунды.
        if snapshot == last_snapshot or now - last_edit < 2.0:
            return
        try:
            await status.edit_text(progress_text(task, *snapshot))
            last_edit = now
            last_snapshot = snapshot
        except Exception:  # сообщение могло быть удалено или Telegram мог ответить flood limit
            log.debug("Не удалось обновить прогресс задачи #%s", task_id, exc_info=True)

    try:
        report = await deps.engine.run(task, progress=show_progress)
    except TaskAlreadyRunningError:
        await status.edit_text("⏳ Синхронизация этой связки уже выполняется.")
        return
    if report.listing_ok and not report.truncated:
        await deps.db.set_task_last_run(task.id, utcnow_iso())
    else:
        log.warning("Ручной проход задачи #%s неполный; повторим позже", task.id)
    try:
        await status.edit_text(render_report(report))
    except Exception:
        await send_long(deps.bot, chat_id, render_report(report))


# ======================================================================
#  Отмена и старт мастера
# ======================================================================

@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Действие отменено.")


# ======================================================================
#  /clone — скопировать чужую ссылку целиком к себе на Диск
# ======================================================================

@router.message(Command("clone"))
async def cmd_clone(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CloneStates.link)
    await message.answer(
        "📥 <b>Клонирование по ссылке</b>\n\n"
        "Пришлите ссылку на файл или папку Google Drive (доступную по ссылке — не обязательно вашу). "
        "Я скопирую её к себе на диск, открою доступ «по ссылке» и пришлю готовую ссылку на копию.\n\n"
        "📁 Если это папка — заодно создам связку с автосинхронизацией: изменения в источнике будут "
        f"сами подтягиваться в копию каждые {format_interval(CLONE_DEFAULT_INTERVAL_SEC)} "
        "(интервал потом можно изменить в настройках связки).\n\n"
        "После ссылки спрошу, <b>какие файлы не копировать</b> — можно указать имена или маски "
        "(например <code>*.mp4</code>)." + CANCEL_HINT
    )


@router.message(CloneStates.link)
async def step_clone_link(message: Message, state: FSMContext) -> None:
    deps = get_deps()
    file_id = extract_drive_id(message.text or "")
    if not file_id:
        await message.answer(
            "Не удалось распознать ссылку. Пришлите ссылку вида "
            "<code>https://drive.google.com/drive/folders/…</code> или "
            "<code>https://drive.google.com/file/d/…</code>, либо сам ID."
        )
        return
    try:
        meta = await deps.drive.get_meta(file_id, fields="id,name,mimeType")
    except DriveError as exc:
        await message.answer(f"Ошибка Google Drive: {esc(exc)}\nПопробуйте другую ссылку.")
        return
    if meta is None:
        await message.answer(
            "Нет доступа к этому файлу/папке. Убедитесь, что у ссылки открыт доступ «Все, у "
            "кого есть ссылка», и попробуйте снова."
        )
        return

    name = str(meta.get("name", ""))
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id

    if meta.get("mimeType") == FOLDER_MIME:
        await state.update_data(
            clone_id=file_id,
            clone_name=name,
            clone_mime=str(meta.get("mimeType", "")),
            clone_user_id=user_id,
        )
        await state.set_state(CloneStates.exclude)
        await message.answer(
            f"🔗 Нашёл папку «{esc(name)}».\n\n"
            "Пришлите, <b>какие файлы не копировать</b>: названия или маски через запятую.\n"
            "Пример: <code>*.mp4, черновик*, отчёт 2024.docx</code>\n"
            "Регистр не важен; <code>*</code> — любые символы, <code>?</code> — один. "
            "Маска действует и на имена подпапок — такие папки не клонируются целиком.\n\n"
            "Если копировать всё — отправьте <code>-</code>." + CANCEL_HINT
        )
    else:
        await state.clear()
        await message.answer(f"⏳ Клонирую файл «{esc(name)}»…")
        spawn(run_clone_file(file_id, name, str(meta.get("mimeType", "")), chat_id))


@router.message(CloneStates.exclude)
async def step_clone_exclude(message: Message, state: FSMContext) -> None:
    """Принимает список исключений и запускает клонирование папки."""
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(
            "Пришлите маски через запятую (например <code>*.mp4, черновик*</code>) "
            "или <code>-</code>, чтобы копировать всё."
        )
        return
    patterns = "" if raw == "-" else raw
    if len(patterns) > 1000:
        await message.answer("Список слишком длинный (максимум 1000 символов). Сократите его.")
        return

    data = await state.get_data()
    await state.clear()
    file_id = str(data.get("clone_id", ""))
    name = str(data.get("clone_name", ""))
    user_id = int(data.get("clone_user_id", 0)) or message.chat.id
    if not file_id:
        await message.answer("Данные устарели — начните заново: /clone")
        return

    if patterns:
        await message.answer(
            f"🚫 Не копирую: <code>{esc(patterns[:300])}</code>"
            + ("…" if len(patterns) > 300 else "")
        )
    await message.answer(
        f"⏳ Клонирую папку «{esc(name)}»… Это может занять время в зависимости от объёма файлов."
    )
    spawn(run_clone_folder(file_id, name, user_id, message.chat.id, exclude_patterns=patterns))


async def run_clone_file(source_id: str, name: str, mime: str, chat_id: int) -> None:
    """Копирует одиночный файл в корень нашего Диска и делится ссылкой."""
    deps = get_deps()
    try:
        try:
            copied = await deps.drive.copy_file(source_id, name, "root")
            target_id = str(copied["id"])
        except DriveError:
            # Fallback, если серверное копирование недоступно (нет прав files.copy)
            content = await deps.drive.download_bytes(source_id)
            uploaded = await deps.drive.upload_bytes(
                name, mime or "application/octet-stream", "root", content
            )
            target_id = str(uploaded["id"])
        await deps.drive.share_with_anyone(target_id)
        link = f"https://drive.google.com/file/d/{target_id}/view"
        await send_long(
            deps.bot, chat_id,
            f"✅ Файл «{esc(name)}» скопирован на ваш диск.\n🔗 {link}",
        )
    except DriveError as exc:
        await send_long(deps.bot, chat_id, f"⚠️ Не удалось скопировать файл: {esc(exc)}")


async def run_clone_folder(
    source_id: str, name: str, user_id: int, chat_id: int,
    exclude_patterns: str = "",
) -> None:
    """Создаёт копию папки в корне нашего Диска, делится ссылкой и заводит автосинхронизацию.

    exclude_patterns — имена/маски файлов, которые не копировать.
    """
    deps = get_deps()
    try:
        # ensure_folder идемпотентен: повторный /clone той же папки не создаст дубликат.
        target_id = await deps.drive.ensure_folder(name, "root")
        await deps.drive.share_with_anyone(target_id)

        task_id = await deps.db.create_task(
            user_id=user_id, title=name, source_folder_id=source_id,
            target_folder_id=target_id, interval_sec=CLONE_DEFAULT_INTERVAL_SEC,
            notify_on_update=True, exclude_patterns=exclude_patterns,
        )
        task = await deps.db.get_task(task_id)
        assert task is not None

        status = await deps.bot.send_message(chat_id, progress_text(task, 0, 0, 0, 0))
        last_edit = 0.0

        async def show_progress(report) -> None:  # type: ignore[no-untyped-def]
            nonlocal last_edit
            now = asyncio.get_running_loop().time()
            if now - last_edit < 2.0:
                return
            try:
                await status.edit_text(progress_text(
                    task, report.checked, len(report.changes), report.created_folders, len(report.errors)
                ))
                last_edit = now
            except Exception:
                log.debug("Не удалось обновить прогресс клонирования", exc_info=True)

        report = await deps.engine.run(task, progress=show_progress)
        if report.listing_ok and not report.truncated:
            await deps.db.set_task_last_run(task.id, utcnow_iso())
        else:
            log.warning("Клонирование задачи #%s неполное; повторим на следующем тике", task.id)

        link = folder_url(target_id)
        final_text = (
            f"✅ Папка «{esc(name)}» скопирована на ваш диск.\n🔗 {link}\n\n" + render_report(report)
            + f"\n\n🔄 Автосинхронизация включена: проверка каждые "
              f"{format_interval(task.interval_sec)}."
        )
        try:
            await status.edit_text(final_text)
        except Exception:
            await send_long(deps.bot, chat_id, final_text)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📂 Открыть копию", url=link)],
            [InlineKeyboardButton(
                text="⚙️ Настроить связку", callback_data=f"task:{task_id}:settings"
            )],
        ])
        await deps.bot.send_message(chat_id, "Управление связкой:", reply_markup=keyboard)
    except DriveError as exc:
        await send_long(deps.bot, chat_id, f"⚠️ Не удалось клонировать папку: {esc(exc)}")


@router.message(Command("new_folder"))
async def cmd_new_folder(message: Message) -> None:
    """Создаёт папку назначения и задачу без источника — источник можно привязать позже."""
    deps = get_deps()
    name = " ".join((message.text or "").split(maxsplit=1)[1:]).strip()
    if not name or len(name) > 64:
        await message.answer("Использование: <code>/new_folder Название папки</code>")
        return
    try:
        target_id = await deps.drive.ensure_folder(name, "root")
        task_id = await deps.db.create_task(
            message.from_user.id if message.from_user else message.chat.id,
            name, "", target_id, CLONE_DEFAULT_INTERVAL_SEC, True,
        )
    except DriveError as exc:
        await message.answer(f"Не удалось создать папку: {esc(exc)}")
        return
    task = await deps.db.get_task(task_id)
    assert task is not None
    await message.answer(
        "✅ Папка создана. Источник пока не подключён — добавьте его кнопкой «Настройки».\n\n"
        + task_card_text(task, 0), reply_markup=task_keyboard(task)
    )


@router.message(Command("new_task"))
async def cmd_new_task(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewTaskStates.title)
    await message.answer(
        "🆕 <b>Новая связка</b> — шаг 1/5.\n\n"
        "Придумайте короткое имя (до 64 символов), например «Материалы ОГЭ» или "
        "«Курсы Python»." + CANCEL_HINT
    )


# ======================================================================
#  Шаги мастера (FSM)
# ======================================================================

@router.message(NewTaskStates.title)
async def step_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 64:
        await message.answer("Имя должно быть от 1 до 64 символов. Попробуйте ещё раз.")
        return
    await state.update_data(title=title)
    await state.set_state(NewTaskStates.source)
    await message.answer(
        "Шаг 2/5. Отправьте <b>ссылку на папку-источник</b> (чужая публичная папка).\n"
        "Подойдёт любой формат ссылки или просто Folder ID." + CANCEL_HINT
    )


@router.message(NewTaskStates.source)
async def step_source(message: Message, state: FSMContext) -> None:
    deps = get_deps()
    folder_id = extract_folder_id(message.text or "")
    if not folder_id:
        await message.answer(
            "Не удалось распознать ID папки. Пришлите ссылку вида "
            "<code>https://drive.google.com/drive/folders/…</code> или сам ID."
        )
        return
    try:
        meta = await deps.drive.get_meta(folder_id, fields="id,name,mimeType")
    except DriveError as exc:
        await message.answer(f"Ошибка Google Drive: {exc}\nПопробуйте другую ссылку.")
        return
    if meta is None or meta.get("mimeType") != FOLDER_MIME:
        await message.answer(
            "По этой ссылке нет доступной папки (возможно, это файл или доступ закрыт). "
            "Пришлите ссылку именно на папку."
        )
        return
    await state.update_data(source_folder_id=folder_id, source_name=meta.get("name", ""))
    await state.set_state(NewTaskStates.target)
    await message.answer(
        f"✅ Источник найден: «{esc(meta.get('name', ''))}».\n\n"
        "Шаг 3/5. Отправьте <b>ссылку на папку-назначение</b> на вашем диске, "
        "куда копировать файлы." + CANCEL_HINT
    )


@router.message(NewTaskStates.target)
async def step_target(message: Message, state: FSMContext) -> None:
    deps = get_deps()
    data = await state.get_data()
    folder_id = extract_folder_id(message.text or "")
    if not folder_id:
        await message.answer("Не смог распознать ID папки. Попробуйте ещё раз.")
        return
    if folder_id == data.get("source_folder_id"):
        await message.answer("Источник и назначение совпадают — выберите другую папку.")
        return
    try:
        meta = await deps.drive.get_meta(folder_id, fields="id,name,mimeType")
    except DriveError as exc:
        await message.answer(f"Ошибка Google Drive: {exc}\nПопробуйте другую ссылку.")
        return
    if meta is None or meta.get("mimeType") != FOLDER_MIME:
        await message.answer(
            "Это не папка (или нет доступа). Пришлите ссылку на папку на вашем диске."
        )
        return
    await state.update_data(target_folder_id=folder_id, target_name=meta.get("name", ""))
    await state.set_state(NewTaskStates.interval)
    await message.answer(
        "✅ Назначение: «" + esc(meta.get("name", "")) + "».\n\n"
        "Шаг 4/5. Как часто проверять обновления?\n\n"
        "Пришлите число с единицей: <code>90</code>, <code>30 мин</code>, "
        "<code>2ч</code>, <code>1 день</code> (минимум 60 сек)." + CANCEL_HINT
    )


@router.message(NewTaskStates.interval)
async def step_interval(message: Message, state: FSMContext) -> None:
    seconds = parse_interval(message.text or "")
    if seconds is None:
        await message.answer(
            "Не понял интервал. Примеры: <code>90</code>, <code>90с</code>, "
            "<code>30 мин</code>, <code>2 часа</code>, <code>1 день</code>. Минимум 60 сек."
        )
        return
    await state.update_data(interval_sec=seconds)
    await state.set_state(NewTaskStates.notify)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Уведомлять", callback_data="newtask:notify:1"),
        InlineKeyboardButton(text="🔇 Тихий режим", callback_data="newtask:notify:0"),
    ]])
    await message.answer(
        f"Шаг 5/5. Интервал: {format_interval(seconds)}.\n\n"
        "Присылать отчёт в чат при каждом изменении?",
        reply_markup=keyboard,
    )


@router.callback_query(NewTaskStates.notify, F.data.startswith("newtask:notify:"))
async def step_notify(cb: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer("Сообщение устарело — начните заново: /new_task", show_alert=True)
        await state.clear()
        return

    deps = get_deps()
    data = await state.get_data()
    notify = (cb.data or "").endswith(":1")
    task_id = await deps.db.create_task(
        user_id=cb.from_user.id,
        title=str(data.get("title", "Без имени")),
        source_folder_id=str(data["source_folder_id"]),
        target_folder_id=str(data["target_folder_id"]),
        interval_sec=int(data["interval_sec"]),
        notify_on_update=notify,
    )
    await state.clear()

    task = await deps.db.get_task(task_id)
    if task is None:  # pragma: no cover — защита от гонки удаления
        await cb.message.answer("Не удалось загрузить связку.")
        await cb.answer()
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔄 Синхронизировать сейчас", callback_data=f"task:{task_id}:sync"
        )],
        [
            InlineKeyboardButton(text="📋 Мои связки", callback_data="show:tasks"),
            InlineKeyboardButton(text="📂 Открыть диск", url=folder_url(task.target_folder_id)),
        ],
    ])
    await cb.message.answer(
        "✅ <b>Связка создана!</b>\n\n" + task_card_text(task, 0),
        reply_markup=keyboard,
    )
    await cb.answer()


# ======================================================================
#  Действия над карточкой задачи: task:{id}:{action}
# ======================================================================

@router.callback_query(F.data.startswith("task:"))
async def on_task_action(cb: CallbackQuery) -> None:
    parsed = _parse_callback(cb.data)
    if parsed is None:
        await cb.answer()
        return
    task_id, action = parsed
    deps = get_deps()

    task = await _load_task(cb, task_id)
    if task is None:
        return

    if action == "toggle":
        was_active = task.is_active
        await deps.db.set_task_active(task.id, not was_active)
        stopping = was_active and deps.engine.request_stop(task.id)
        task = await deps.db.get_task(task_id)
        assert task is not None
        if isinstance(cb.message, Message):
            count = await deps.db.count_synced_items(task_id)
            await cb.message.edit_text(
                task_card_text(task, count), reply_markup=task_keyboard(task)
            )
        if was_active:
            await cb.answer(
                "⏸ Автосинхронизация выключена; текущий проход останавливается…"
                if stopping else "⏸ Автосинхронизация выключена. Данные и файлы сохранены."
            )
        else:
            await cb.answer("▶️ Автосинхронизация возобновлена. Если интервал уже истёк — проверка пройдёт в течение минуты.")

    elif action == "sync":
        if not task.source_folder_id:
            await cb.answer("Сначала подключите источник в настройках задачи.", show_alert=True)
            return
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        await cb.answer("🔄 Синхронизация запущена…")
        spawn(run_manual_sync(task_id, chat_id))

    elif action == "settings":
        if isinstance(cb.message, Message):
            await cb.message.edit_text(settings_text(task), reply_markup=settings_keyboard(task))
        await cb.answer()

    elif action == "delete":
        if isinstance(cb.message, Message):
            await cb.message.edit_text(
                f"🗑 Удалить связку «{esc(task.title)}»?\n"
                "Будут удалены только настройки — файлы, скопированные в назначение, "
                "останутся на Drive.",
                reply_markup=confirm_delete_keyboard(task),
            )
        await cb.answer()

    else:
        await cb.answer()


# ======================================================================
#  Подтверждение удаления: taskdel:{id}:{yes|no}
# ======================================================================

@router.callback_query(F.data.startswith("taskdel:"))
async def on_delete_confirm(cb: CallbackQuery) -> None:
    parsed = _parse_callback(cb.data)
    if parsed is None:
        await cb.answer()
        return
    task_id, action = parsed
    deps = get_deps()

    task = await _load_task(cb, task_id)
    if task is None:
        return

    if action == "yes":
        title = task.title
        await deps.db.delete_task(task_id)
        if isinstance(cb.message, Message):
            await cb.message.edit_text(f"🗑 Связка «{esc(title)}» удалена.")
        await cb.answer("Удалено")
    else:
        if isinstance(cb.message, Message):
            count = await deps.db.count_synced_items(task_id)
            await cb.message.edit_text(
                task_card_text(task, count), reply_markup=task_keyboard(task)
            )
        await cb.answer("Отменено")


# ======================================================================
#  Меню настроек: set:{id}:{interval|notify|name|back}
# ======================================================================

@router.callback_query(F.data.startswith("set:"))
async def on_settings_action(cb: CallbackQuery, state: FSMContext) -> None:
    parsed = _parse_callback(cb.data)
    if parsed is None:
        await cb.answer()
        return
    task_id, action = parsed
    deps = get_deps()

    task = await _load_task(cb, task_id)
    if task is None:
        return

    if action == "notify":
        await deps.db.update_task_notify(task.id, not task.notify_on_update)
        task = await deps.db.get_task(task_id)
        assert task is not None
        if isinstance(cb.message, Message):
            await cb.message.edit_text(settings_text(task), reply_markup=settings_keyboard(task))
        await cb.answer(
            "🔔 Уведомления включены" if task.notify_on_update else "🔇 Уведомления выключены"
        )

    elif action == "mirror":
        await deps.db.update_task_mirror_deletes(task.id, not task.mirror_deletes)
        task = await deps.db.get_task(task_id)
        assert task is not None
        if isinstance(cb.message, Message):
            await cb.message.edit_text(settings_text(task), reply_markup=settings_keyboard(task))
        await cb.answer(
            "⚠️ Зеркалирование удалений включено: бот будет удалять в назначении файлы, "
            "исчезнувшие из источника" if task.mirror_deletes
            else "📁 Зеркалирование удалений выключено",
            show_alert=task.mirror_deletes,
        )

    elif action == "interval":
        await state.update_data(settings_task_id=task_id)
        await state.set_state(SettingsStates.interval)
        if isinstance(cb.message, Message):
            await cb.message.edit_text(
                "Пришлите новый интервал: <code>10 мин</code>, <code>2ч</code>, "
                "<code>90</code>… (минимум 60 сек)" + CANCEL_HINT
            )
        await cb.answer()

    elif action == "source":
        await state.update_data(settings_task_id=task_id)
        await state.set_state(SettingsStates.source)
        if isinstance(cb.message, Message):
            await cb.message.edit_text("Пришлите ссылку на папку-источник." + CANCEL_HINT)
        await cb.answer()

    elif action == "category":
        await state.update_data(settings_task_id=task_id)
        await state.set_state(SettingsStates.category)
        if isinstance(cb.message, Message):
            await cb.message.edit_text("Пришлите название категории (до 24 символов). Чтобы убрать категорию — отправьте <code>-</code>." + CANCEL_HINT)
        await cb.answer()

    elif action == "template":
        await state.update_data(settings_task_id=task_id)
        await state.set_state(SettingsStates.template)
        if isinstance(cb.message, Message):
            await cb.message.edit_text(
                "Пришлите <b>один файл как документ</b>. Он будет добавляться в каждую новую подпапку. "
                "После файла я спрошу задержку.\n\nЧтобы выключить шаблон, отправьте <code>-</code>." + CANCEL_HINT
            )
        await cb.answer()

    elif action == "exclude":
        await state.update_data(settings_task_id=task_id)
        await state.set_state(SettingsStates.exclude)
        if isinstance(cb.message, Message):
            current = task.exclude_patterns or "нет"
            await cb.message.edit_text(
                f"🚫 <b>Исключения «{esc(task.title)}»</b>\n\n"
                f"Текущий список: <code>{esc(current[:300])}</code>"
                + ("…" if len(current) > 300 else "") + "\n\n"
                "Пришлите названия файлов или маски через запятую: "
                "<code>*.mp4, черновик*, отчёт.docx</code>.\n"
                "Эти файлы и папки с такими именами больше не будут копироваться в назначение.\n\n"
                "Чтобы очистить список — отправьте <code>-</code>." + CANCEL_HINT
            )
        await cb.answer()

    elif action == "name":
        await state.update_data(settings_task_id=task_id)
        await state.set_state(SettingsStates.name)
        if isinstance(cb.message, Message):
            await cb.message.edit_text(
                "Пришлите новое имя связки (до 64 символов)" + CANCEL_HINT
            )
        await cb.answer()

    elif action == "back":
        if isinstance(cb.message, Message):
            count = await deps.db.count_synced_items(task_id)
            await cb.message.edit_text(
                task_card_text(task, count), reply_markup=task_keyboard(task)
            )
        await cb.answer()

    else:
        await cb.answer()


@router.message(SettingsStates.interval)
async def settings_set_interval(message: Message, state: FSMContext) -> None:
    deps = get_deps()
    user_id = message.from_user.id if message.from_user else 0
    data = await state.get_data()
    task_id = int(data.get("settings_task_id", 0))
    task = await deps.db.get_task(task_id)

    if task is None or task.user_id != user_id:
        await state.clear()
        await message.answer("Связка не найдена.")
        return

    seconds = parse_interval(message.text or "")
    if seconds is None:
        await message.answer(
            "Не понял интервал. Примеры: <code>90</code>, <code>30 мин</code>, "
            "<code>2 часа</code>, <code>1 день</code>."
        )
        return

    await deps.db.update_task_interval(task.id, seconds)
    await state.clear()
    task = await deps.db.get_task(task_id)
    assert task is not None
    count = await deps.db.count_synced_items(task.id)
    await message.answer(
        f"✅ Интервал обновлён: {format_interval(task.interval_sec)}.\n\n"
        + task_card_text(task, count),
        reply_markup=task_keyboard(task),
    )


@router.message(SettingsStates.source)
async def settings_set_source(message: Message, state: FSMContext) -> None:
    deps = get_deps()
    data = await state.get_data()
    task_id = int(data.get("settings_task_id", 0))
    task = await deps.db.get_task(task_id)
    folder_id = extract_folder_id(message.text or "")
    if task is None or task.user_id != (message.from_user.id if message.from_user else 0):
        await state.clear(); await message.answer("Задача не найдена."); return
    if not folder_id:
        await message.answer("Пришлите корректную ссылку или Folder ID."); return
    meta = await deps.drive.get_meta(folder_id)
    if meta is None or meta.get("mimeType") != FOLDER_MIME:
        await message.answer("Папка недоступна. Проверьте ссылку и доступ."); return
    await deps.db.update_task_source(task_id, folder_id)
    await state.clear()
    task = await deps.db.get_task(task_id); assert task
    await message.answer("✅ Источник подключён.\n\n" + task_card_text(task, await deps.db.count_synced_items(task_id)), reply_markup=task_keyboard(task))


@router.message(SettingsStates.category)
async def settings_set_category(message: Message, state: FSMContext) -> None:
    deps = get_deps(); data = await state.get_data(); task_id = int(data.get("settings_task_id", 0))
    task = await deps.db.get_task(task_id)
    category = (message.text or "").strip()
    if task is None or task.user_id != (message.from_user.id if message.from_user else 0):
        await state.clear(); await message.answer("Задача не найдена."); return
    if not category or len(category) > 24:
        await message.answer("Название — от 1 до 24 символов."); return
    await deps.db.update_task_category(task_id, "" if category == "-" else category)
    await state.clear(); task = await deps.db.get_task(task_id); assert task
    await message.answer("✅ Категория сохранена.\n\n" + task_card_text(task, await deps.db.count_synced_items(task_id)), reply_markup=task_keyboard(task))


@router.message(SettingsStates.template)
async def settings_set_template(message: Message, state: FSMContext) -> None:
    deps = get_deps(); data = await state.get_data(); task_id = int(data.get("settings_task_id", 0))
    task = await deps.db.get_task(task_id)
    if task is None or task.user_id != (message.from_user.id if message.from_user else 0):
        await state.clear(); await message.answer("Задача не найдена."); return
    if (message.text or "").strip() == "-":
        await deps.db.update_task_template(task_id, False)
        await state.clear(); await message.answer("Шаблон выключен."); return
    if not message.document:
        await message.answer("Нужен файл, отправленный именно как документ, либо <code>-</code>."); return
    doc = message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer("Максимальный размер шаблона — 20 МБ."); return
    buffer = BytesIO()
    await deps.bot.download(doc, destination=buffer)
    await state.update_data(template_name=doc.file_name or "template", template_mime=doc.mime_type or "application/octet-stream", template_data=buffer.getvalue())
    await state.set_state(SettingsStates.template_delay)
    await message.answer("Через сколько добавлять файл после создания подпапки? Например <code>30 мин</code>. Для сразу — <code>0</code>.")


@router.message(SettingsStates.template_delay)
async def settings_set_template_delay(message: Message, state: FSMContext) -> None:
    deps = get_deps(); data = await state.get_data(); task_id = int(data.get("settings_task_id", 0))
    raw = (message.text or "").strip()
    delay = 0 if raw == "0" else parse_interval(raw)
    if delay is None:
        await message.answer("Укажите <code>0</code> или интервал не меньше 1 минуты, например <code>30 мин</code>."); return
    await deps.db.update_task_template(task_id, True, str(data["template_name"]), str(data["template_mime"]), data["template_data"], delay)
    await state.clear(); task = await deps.db.get_task(task_id); assert task
    await message.answer("✅ Шаблон сохранён.\n\n" + task_card_text(task, await deps.db.count_synced_items(task_id)), reply_markup=task_keyboard(task))


@router.message(SettingsStates.exclude)
async def settings_set_exclude(message: Message, state: FSMContext) -> None:
    deps = get_deps(); data = await state.get_data(); task_id = int(data.get("settings_task_id", 0))
    task = await deps.db.get_task(task_id)
    raw = (message.text or "").strip()
    if task is None or task.user_id != (message.from_user.id if message.from_user else 0):
        await state.clear(); await message.answer("Связка не найдена."); return
    if not raw or len(raw) > 1000:
        await message.answer("Нужен текст до 1000 символов: маски через запятую или <code>-</code>."); return
    patterns = "" if raw == "-" else raw
    await deps.db.update_task_exclude_patterns(task_id, patterns)
    await state.clear(); task = await deps.db.get_task(task_id); assert task
    saved = split_exclude_patterns(patterns)
    await message.answer(
        ("✅ Список исключений очищен." if not saved
         else f"✅ Исключений в списке: {len(saved)}. Правило применится со следующей синхронизации.")
        + "\n\n" + task_card_text(task, await deps.db.count_synced_items(task_id)),
        reply_markup=task_keyboard(task),
    )


@router.message(SettingsStates.name)
async def settings_set_name(message: Message, state: FSMContext) -> None:
    deps = get_deps()
    user_id = message.from_user.id if message.from_user else 0
    data = await state.get_data()
    task_id = int(data.get("settings_task_id", 0))
    task = await deps.db.get_task(task_id)

    if task is None or task.user_id != user_id:
        await state.clear()
        await message.answer("Связка не найдена.")
        return

    new_title = (message.text or "").strip()
    if not new_title or len(new_title) > 64:
        await message.answer("Имя должно быть от 1 до 64 символов. Попробуйте ещё раз.")
        return

    await deps.db.update_task_title(task.id, new_title)
    await state.clear()
    task = await deps.db.get_task(task_id)
    assert task is not None
    count = await deps.db.count_synced_items(task.id)
    await message.answer(
        "✅ Имя обновлено.\n\n" + task_card_text(task, count),
        reply_markup=task_keyboard(task),
    )


# ======================================================================
#  Показ списка связок (используется после создания задачи)
# ======================================================================

async def send_tasks_menu(message: Message, user_id: int) -> None:
    deps = get_deps()
    tasks = await deps.db.get_user_tasks(user_id)
    if not tasks:
        await message.answer("Связок пока нет. Создайте: /new_task или папку без источника: <code>/new_folder Название</code>")
        return
    categories = await deps.db.get_categories(user_id)
    rows = [[InlineKeyboardButton(text=f"📋 Все задачи ({len(tasks)})", callback_data="tasks:all")]]
    rows.extend([[InlineKeyboardButton(text=f"🏷 {name}", callback_data=f"cat:{i}")] for i, name in enumerate(categories)])
    uncategorized = sum(1 for task in tasks if not task.category)
    if uncategorized:
        rows.append([InlineKeyboardButton(text=f"▫️ Без категории ({uncategorized})", callback_data="tasks:none")])
    await message.answer("📋 <b>Задачи</b>\nВыберите раздел:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def send_task_cards(message: Message, tasks: list[Task], title: str) -> None:
    await message.answer(f"<b>{esc(title)}</b> — {len(tasks)} шт.")
    for task in tasks:
        await message.answer(task_card_text(task, await get_deps().db.count_synced_items(task.id)), reply_markup=task_keyboard(task))


async def send_category_tasks(message: Message, tasks: list[Task], title: str) -> None:
    """Компактное меню категории: карточка задачи открывается отдельной кнопкой."""
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        status = "▶️" if task.is_active else "⏸"
        rows.append([InlineKeyboardButton(
            text=f"{status} {task.title[:45]}",
            callback_data=f"cat_task:{task.id}",
        )])
    if not rows:
        rows.append([InlineKeyboardButton(text="В категории пока нет задач", callback_data="fm:noop")])
    rows.append([InlineKeyboardButton(text="◀️ К категориям", callback_data="show:tasks")])
    await message.answer(f"<b>{esc(title)}</b>\nВыберите папку/задачу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("cat_task:"))
async def on_category_task(cb: CallbackQuery) -> None:
    try:
        task_id = int((cb.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Задача устарела", show_alert=True)
        return
    task = await _load_task(cb, task_id)
    if task is None:
        return
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            task_card_text(task, await get_deps().db.count_synced_items(task.id)),
            reply_markup=task_keyboard(task),
        )
    await cb.answer()



@router.callback_query(F.data == "show:tasks")
async def on_show_tasks(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        await send_tasks_menu(cb.message, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.startswith("tasks:"))
async def on_tasks_group(cb: CallbackQuery) -> None:
    deps = get_deps()
    mode = (cb.data or "").split(":", 1)[1]
    tasks = await deps.db.get_user_tasks(cb.from_user.id)
    if mode == "none":
        tasks = [task for task in tasks if not task.category]
        title = "▫️ Без категории"
    else:
        title = "📋 Все задачи"
    if isinstance(cb.message, Message):
        await send_category_tasks(cb.message, tasks, title)
    await cb.answer()


@router.callback_query(F.data.startswith("cat:"))
async def on_category_group(cb: CallbackQuery) -> None:
    deps = get_deps()
    try:
        index = int((cb.data or "").split(":", 1)[1])
        category = (await deps.db.get_categories(cb.from_user.id))[index]
    except (ValueError, IndexError):
        await cb.answer("Категория устарела. Откройте /tasks заново.", show_alert=True); return
    if isinstance(cb.message, Message):
        await send_category_tasks(
            cb.message,
            await deps.db.get_tasks_by_category(cb.from_user.id, category),
            f"🏷 {category}",
        )
    await cb.answer()
