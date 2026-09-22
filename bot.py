"""Точка входа: инициализация зависимостей, регистрация роутеров, запуск планировщика."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import config
from database.db import Database
from deps import Deps, set_deps
from handlers import commands, tasks, file_manager  # noqa: F401 — роутеры регистрируются ниже
from handlers.access import AdminOnly
from services.drive import DriveAuthError, DriveClient
from services.sync import SyncEngine, render_report
from utils import send_long, utcnow, utcnow_iso

log = logging.getLogger("bot")

SKEW_SEC = 5  # запас в секундах, чтобы не дёргать задачу на границе интервала


async def scheduler_tick(bot: Bot, db: Database, engine: SyncEngine) -> None:
    """Добавляет отложенные шаблоны и запускает синхронизации по интервалу."""
    try:
        # Шаблон добавляется именно после заданной задержки от создания подпапки.
        for task, target_folder_id in await db.get_due_templates(utcnow_iso()):
            try:
                assert task.template_data is not None
                await drive_upload_template(engine, task, target_folder_id)
                await db.complete_template(task.id, target_folder_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не добавлен шаблон в папку %s: %s", target_folder_id, exc)

        now = utcnow()
        for task in await db.get_active_tasks():
            if not task.source_folder_id or engine.is_busy(task.id):
                continue  # синхронизация идёт или уже стоит в очереди

            if task.last_run_at:
                try:
                    last_run = datetime.fromisoformat(task.last_run_at)
                except ValueError:
                    last_run = None
                if last_run is not None and (
                    (now - last_run).total_seconds() + SKEW_SEC < task.interval_sec
                ):
                    continue  # интервал ещё не истёк

            log.info("Плановая синхронизация задачи #%s «%s»", task.id, task.title)
            report = await engine.run(task)
            # Неполный проход (например, timeout при чтении подпапки) не считаем
            # успешным: следующая проверка сразу продолжит обход по журналу.
            if report.listing_ok and not report.truncated:
                await db.set_task_last_run(task.id, utcnow_iso())
            else:
                log.warning("Проход задачи #%s неполный; повторим на следующем тике", task.id)

            if task.notify_on_update and (report.changes or report.errors):
                try:
                    await send_long(bot, task.user_id, render_report(report))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не доставлен отчёт пользователю %s: %s", task.user_id, exc)
    except Exception:  # noqa: BLE001 — тик не должен ронять планировщик
        log.exception("Ошибка в scheduler_tick")


async def drive_upload_template(engine: SyncEngine, task, target_folder_id: str) -> None:
    """Загружает сохранённый Telegram-файл в целевую подпапку."""
    await engine._drive.upload_bytes(  # инкапсуляция здесь намеренно не нужна: единый Drive-клиент
        task.template_name,
        task.template_mime or "application/octet-stream",
        target_folder_id,
        task.template_data,
    )


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    if not config.bot_token:
        log.critical(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и впишите токен от @BotFather."
        )
        sys.exit(1)

    # --- зависмости -----------------------------------------------------
    db = Database(config.database_path)
    await db.init()

    drive = DriveClient(
        credentials_file=config.credentials_file,
        token_file=config.token_file,
        max_retries=config.drive_max_retries,
    )
    try:
        await drive.ensure_authorized()  # fail-fast: без token.json бот не стартует
    except DriveAuthError as exc:
        log.critical("Google Drive не авторизован: %s", exc)
        sys.exit(1)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    engine = SyncEngine(db, drive)
    set_deps(Deps(bot=bot, db=db, drive=drive, engine=engine))

    # --- роутеры ---------------------------------------------------------
    dp = Dispatcher(storage=MemoryStorage())
    # Глобальный фильтр до всех команд, текстов, файлов и callback-кнопок.
    dp.message.filter(AdminOnly())
    dp.callback_query.filter(AdminOnly())
    dp.include_routers(commands.router, tasks.router, file_manager.router)

    # --- планировщик -----------------------------------------------------
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        scheduler_tick,
        trigger="interval",
        seconds=config.scheduler_tick_sec,
        args=(bot, db, engine),
        max_instances=1,        # новый тик не стартует, пока не завершён предыдущий
        coalesce=True,
        next_run_time=utcnow(), # первый прогон — сразу после старта
    )
    scheduler.start()

    log.info("Бот запущен. Проверка каждые %s сек.; файлы копируются последовательно.",
             config.scheduler_tick_sec)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
