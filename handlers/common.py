"""Построители текстов и клавиатур карточек задач (без логики)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import Task
from utils import esc, format_dt_short, format_interval


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def task_card_text(task: Task, synced_count: int) -> str:
    status = "▶️ Активна" if task.is_active else "⏸ На паузе"
    notify = "🔔 уведомления включены" if task.notify_on_update else "🔇 тихий режим"
    category = task.category or "Без категории"
    source = "🔗 подключён" if task.source_folder_id else "➕ не подключён"
    template = "✅ включён" if task.template_enabled and task.template_data else "❌ выключен / не задан"
    return (
        f"<b>{esc(task.title)}</b>\n"
        f"Статус: {status}\n"
        f"Интервал проверки: {format_interval(task.interval_sec)}\n"
        f"Режим: {notify}\n"
        f"Категория: {esc(category)}\n"
        f"Источник: {source}\n"
        f"Файл в новые папки: {template}\n"
        f"Синхронизировано элементов: {synced_count}\n"
        f"Последняя синхронизация: {format_dt_short(task.last_run_at)}"
    )


def task_keyboard(task: Task) -> InlineKeyboardMarkup:
    toggle_text = "⏸ Остановить автосинхронизацию" if task.is_active else "▶️ Возобновить автосинхронизацию"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=toggle_text, callback_data=f"task:{task.id}:toggle"
            ),
            InlineKeyboardButton(
                text="🔄 Синхронизировать сейчас", callback_data=f"task:{task.id}:sync"
            ),
        ],
        [InlineKeyboardButton(
            text="⚙️ Настройки", callback_data=f"task:{task.id}:settings"
        )],
        [InlineKeyboardButton(
            text="🗑 Удалить", callback_data=f"task:{task.id}:delete"
        )],
        ([
            InlineKeyboardButton(text="📂 Источник", url=folder_url(task.source_folder_id)),
            InlineKeyboardButton(text="📁 Назначение", url=folder_url(task.target_folder_id)),
        ] if task.source_folder_id else [
            InlineKeyboardButton(text="➕ Подключить источник", callback_data=f"set:{task.id}:source"),
            InlineKeyboardButton(text="📁 Назначение", url=folder_url(task.target_folder_id)),
        ]),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(task: Task) -> str:
    notify_state = "🔔 включены" if task.notify_on_update else "🔇 выключены"
    mirror_state = "🗑 включено" if task.mirror_deletes else "📁 выключено (только отчёт)"
    if task.exclude_patterns:
        exclude_display = (
            task.exclude_patterns[:120] + "…" if len(task.exclude_patterns) > 120
            else task.exclude_patterns
        )
    else:
        exclude_display = "нет"
    return (
        f"⚙️ <b>Настройки «{esc(task.title)}»</b>\n\n"
        f"⏱ Интервал проверки: {format_interval(task.interval_sec)}\n"
        f"Автосинхронизация: {'▶️ включена' if task.is_active else '⏸ на паузе (запуск только вручную)'}\n"
        f"Уведомления: {notify_state}\n"
        f"Зеркалирование удалений: {mirror_state}\n"
        f"Исключения: {esc(exclude_display)}\n"
        f"Категория: {esc(task.category or 'Без категории')}\n"
        f"Источник: {'подключён' if task.source_folder_id else 'не подключён'}\n"
        f"Шаблон: {esc(task.template_name) if task.template_data else 'не задан'} "
        f"({'вкл.' if task.template_enabled else 'выкл.'}), задержка: {format_interval(task.template_delay_sec) if task.template_delay_sec else 'сразу'}\n\n"
        "<i>Исключения — имена или маски файлов (например, <code>*.mp4</code>), которые бот не копирует в назначение.</i>\n"
        "<i>Шаблон — файл, который бот добавит в каждую новую подпапку назначения после указанной задержки.</i>"
    )


def settings_keyboard(task: Task) -> InlineKeyboardMarkup:
    mirror_text = (
        "🗑 Зеркалирование удалений: выкл." if task.mirror_deletes
        else "🗑 Зеркалирование удалений: вкл."
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="⏱ Изменить интервал", callback_data=f"set:{task.id}:interval"
        )],
        [InlineKeyboardButton(
            text="🔔/🔇 Уведомления вкл/выкл", callback_data=f"set:{task.id}:notify"
        )],
        [InlineKeyboardButton(
            text=mirror_text, callback_data=f"set:{task.id}:mirror"
        )],
        [InlineKeyboardButton(
            text="🚫 Исключения файлов", callback_data=f"set:{task.id}:exclude"
        )],
        [InlineKeyboardButton(
            text="📁 Источник" if task.source_folder_id else "➕ Подключить источник", callback_data=f"set:{task.id}:source"
        )],
        [InlineKeyboardButton(
            text="🏷 Категория", callback_data=f"set:{task.id}:category"
        )],
        [InlineKeyboardButton(
            text="📄 Шаблон: изменить" if task.template_data else "📄 Добавить шаблон", callback_data=f"set:{task.id}:template"
        )],
        [InlineKeyboardButton(
            text="✏️ Переименовать", callback_data=f"set:{task.id}:name"
        )],
        [InlineKeyboardButton(
            text="◀️ Назад", callback_data=f"set:{task.id}:back"
        )],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_keyboard(task: Task) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"taskdel:{task.id}:yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"taskdel:{task.id}:no"),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)
