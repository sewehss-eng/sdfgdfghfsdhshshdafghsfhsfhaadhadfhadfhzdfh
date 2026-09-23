"""Движок синхронизации: рекурсивный обход, инкрементальность, отчёты.

Логика инкрементальности: каждый элемент источника сверяется с таблицей
synced_items по (task_id, source_id). Элемент считается неизменённым, если
совпадают modifiedTime и (для бинарных файлов) size. Нативные документы
Google Workspace экспортируются в Office/PDF-форматы.
"""

from __future__ import annotations

undefined
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from database.db import Database, SyncedItem, Task
from services.drive import DriveClient, DriveError, FOLDER_MIME, NATIVE_EXPORT
from utils import is_excluded

log = logging.getLogger(__name__)

MAX_TREE_ITEMS = 5000  # защитный лимит объёма одного прохода
MAX_DEPTH = 15          # защитный лимит вложенности
undefined = Callable[["SyncReport"], Awaitable[None]]

KIND_NEW = "new"
KIND_UPDATED = "updated"
KIND_RENAMED = "renamed"

# Что не копируем в принципе: ярлыки, формы (нет API экспорта), карты.
NOT_SYNCABLE_MIMES = {
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
}


undefined(frozen=True)
class Change:
    kind: str   # new / updated / renamed
    name: str
    path: str   # путь относительно корня источника
    note: str = ""


@dataclass
class SyncReport:
    task_id: int
    task_title: str
    started_at: datetime
    finished_at: datetime | None = None
    checked: int = 0
    skipped: int = 0
    excluded_names: list[str] = field(default_factory=list)  # пропущены по маске исключения
    created_folders: int = 0
    changes: list[Change] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # исчезли из источника, но не удалены (mirror_deletes выключен)
    deleted: list[str] = field(default_factory=list)  # удалены в назначении (mirror_deletes включён)
    listing_ok: bool = True   # False -> отчёт о «stale» не показываем
    truncated: bool = False


class SyncEngine:
    """Выполняет синхронизацию задач; защищён от параллельного запуска одной задачи."""

    def __init__(self, db: Database, drive: DriveClient) -> None:
        self._db = db
        self._drive = drive
        self._running: set[int] = set()
        self._stop_requested: set[int] = set()
        # Один проход Drive за раз: параллельные синхронизации делят один токен
        # и легко ловят 429/таймауты. Остальные ждут в очереди.
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued: set[int] = set()
        self._gate = asyncio.Lock()

    def is_running(self, task_id: int) -> bool:
        return task_id in self._running

    def is_busy(self, task_id: int) -> bool:
        """Задача уже выполняется или стоит в очереди."""
        return task_id in self._running or task_id in self._queued

    def queue_position(self, task_id: int) -> int:
        """0 — выполняется сейчас, >0 — место в очереди, -1 — не в очереди."""
        if task_id in self._running:
            return 0
        if task_id not in self._queued:
            return -1
        ahead = 1
        for queued_id in list(self._queue._queue):  # noqa: SLF001 — позиция для сообщения пользователю
            if queued_id == task_id:
                return ahead
            ahead += 1
        return ahead

    def request_stop(self, task_id: int) -> bool:
        """Просит безопасно остановить текущий обход после активных API-вызовов."""
        if task_id not in self._running:
            return False
        self._stop_requested.add(task_id)
        return True

    def _should_stop(self, task_id: int) -> bool:
        return task_id in self._stop_requested

    async def run(self, task: Task, progress: ProgressCallback | None = None) -> SyncReport:
        """Полный проход по задаче. Никогда не бросает исключений наружу — всё в отчёт."""
        report = SyncReport(
            task_id=task.id,
            task_title=task.title,
            started_at=datetime.now(timezone.utc),
        )
        if task.id in self._running or task.id in self._queued:
            raise TaskAlreadyRunningError(f"Задача {task.id} уже синхронизируется")

        self._queued.add(task.id)
        await self._queue.put(task.id)
        acquired = False
        try:
            # Ждём своей очереди. Замок держим до конца прохода, чтобы следующий
            # не стартовал, пока этот не освободит Drive.
            while True:
                await self._gate.acquire()
                try:
                    if self._queue.qsize() and self._queue._queue[0] == task.id:  # noqa: SLF001
                        await self._queue.get()
                        acquired = True
                        break
                finally:
                    if not acquired:
                        self._gate.release()
                await asyncio.sleep(0.2)
            self._queued.discard(task.id)

            self._running.add(task.id)
            self._stop_requested.discard(task.id)
            seen: set[str] = set()
            item_cache: dict[str, SyncedItem] = {}
            pending_upserts: dict[str, SyncedItem] = {}
            try:
                # Один SELECT вместо запроса SQLite для каждого файла/папки.
                item_cache = {item.source_id: item for item in await self._db.get_synced_items(task.id)}
                if not task.source_folder_id:
                    report.errors.append("Источник ещё не подключён. Добавьте его в настройках задачи.")
                    return report
                await self._walk(
                    task, task.source_folder_id, task.target_folder_id,
                    path="", report=report, seen=seen, depth=0,
                    item_cache=item_cache, pending_upserts=pending_upserts, progress=progress,
                )
                # «Пропавшие» из источника считаем только если обход прошёл без ошибок,
                # иначе есть риск ложных срабатываний из-за частичного листинга.
                if report.listing_ok and not report.truncated:
                    # Ранее синхронизированные, но теперь исключённые маской элементы
                    # игнорируем молча: они не «исчезли из источника» и удалять их не нужно.
                    stale_items = [
                        i for i in item_cache.values()
                        if i.source_id not in seen
                        and not is_excluded(i.file_name, task.exclude_patterns)
                    ]
                    if task.mirror_deletes:
                        await self._mirror_deletions(task, stale_items, report)
                    else:
                        report.stale = [i.file_name for i in stale_items]
            except Exception as exc:  # noqa: BLE001 — фоновая задача не должна ронять воркера
                log.exception("Критическая ошибка синхронизации задачи #%s", task.id)
                report.errors.append(f"Критическая ошибка: {exc!r}")
            finally:
                # Все новые/изменённые записи — одним executemany + одной транзакцией.
                if pending_upserts:
                    try:
                        await self._db.upsert_synced_items(list(pending_upserts.values()))
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Не сохранён пакет журнала задачи #%s", task.id)
                        report.errors.append(f"Не сохранён журнал синхронизации: {exc!r}")
                self._running.discard(task.id)
                self._stop_requested.discard(task.id)
                report.finished_at = datetime.now(timezone.utc)
        finally:
            self._queued.discard(task.id)
            if acquired:
                self._gate.release()
        if progress is not None:
            try:
                await progress(report)
            except Exception:  # noqa: BLE001
                log.debug("Не обновлён финальный прогресс задачи #%s", task.id, exc_info=True)
        return report

    # -------------------------------------------------- зеркалирование удалений

    async def _mirror_deletions(
        self, task: Task, stale_items: list[SyncedItem], report: SyncReport
    ) -> None:
        """Удаляет в назначении всё, что исчезло из источника.

        Сортируем так, чтобы файлы удалялись раньше папок — если папку
        удалить первой вместе с содержимым, попытки удалить уже
        отсутствующие вложенные файлы просто тихо ничего не сделают.
        """
        ordered = sorted(stale_items, key=lambda i: i.mime_type == FOLDER_MIME)
        for item in ordered:
            try:
                await self._drive.delete_file(item.target_id)
            except DriveError as exc:
                log.info("Зеркалирование удаления %s: %s", item.target_id, exc)
            await self._db.delete_synced_item(task.id, item.source_id)
            report.deleted.append(item.file_name)

    # --------------------------------------------------------------- обход

    async def _walk(
        self,
        task: Task,
        src_folder_id: str,
        dst_folder_id: str,
        path: str,
        report: SyncReport,
        seen: set[str],
        depth: int,
        item_cache: dict[str, SyncedItem],
        pending_upserts: dict[str, SyncedItem],
        progress: ProgressCallback | None,
    ) -> None:
        if self._should_stop(task.id):
            report.errors.append("Синхронизация остановлена пользователем.")
            report.listing_ok = False
            return
        if depth > MAX_DEPTH:
            report.errors.append(f"Превышена глубина вложенности ({MAX_DEPTH}) в «{path}»")
            return
        if report.checked >= MAX_TREE_ITEMS:
            if not report.truncated:
                report.truncated = True
                report.errors.append(f"Обход остановлен: более {MAX_TREE_ITEMS} элементов")
            return

        try:
            children = await self._drive.list_folder(src_folder_id)
        except DriveError as exc:
            report.errors.append(f"Не удалось прочитать папку «{path or 'корень'}»: {exc}")
            report.listing_ok = False
            return

        # Папки идут последовательно, чтобы целевая структура всегда была готова.
        # Обычные файлы обрабатываются ограниченными пачками: не более _concurrency.
        folders = [item for item in children if item.get("mimeType") == FOLDER_MIME]
        files = [item for item in children if item.get("mimeType") != FOLDER_MIME]
        for item in folders:
            if self._should_stop(task.id):
                report.errors.append("Синхронизация остановлена пользователем.")
                report.listing_ok = False
                return
            if report.checked >= MAX_TREE_ITEMS:
                report.truncated = True
                report.errors.append(f"Обход остановлен: более {MAX_TREE_ITEMS} элементов")
                return

            source_id: str = item["id"]
            name: str = item.get("name", source_id)
            mime: str = item.get("mimeType", "")
            child_path = f"{path}/{name}" if path else name
            if is_excluded(name, task.exclude_patterns):
                # Папка исключена маской — не создаём её в назначении и не заходим внутрь.
                report.excluded_names.append(child_path)
                continue
            seen.add(source_id)
            report.checked += 1

            try:
                if mime == FOLDER_MIME:
                    target_sub = await self._ensure_target_folder(
                        task, item, dst_folder_id, report, item_cache, pending_upserts
                    )
                    if target_sub:
                        await self._apply_template(
                            task, target_sub, report, folder_name=name
                        )
                        await self._walk(
                            task, source_id, target_sub,
                            path=child_path, report=report, seen=seen, depth=depth + 1,
                            item_cache=item_cache, pending_upserts=pending_upserts, progress=progress,
                        )
                elif mime in NOT_SYNCABLE_MIMES:
                    report.skipped += 1
            except DriveError as exc:
                report.errors.append(f"«{child_path}»: {exc}")
            await self._notify_progress(report, progress)

        # Файлы обрабатываются строго последовательно: один запрос загрузки/
        # копирования завершается до начала следующего. Это снижает нагрузку
        # на сеть и прокси и не замедляет общий проход из-за конкурирующих TLS-соединений.
        for item in files:
            if self._should_stop(task.id):
                report.errors.append("Синхронизация остановлена пользователем.")
                report.listing_ok = False
                return
            await self._sync_file_item(
                task, item, dst_folder_id, path, report, seen,
                item_cache, pending_upserts,
            )
            await self._notify_progress(report, progress)

    async def _sync_file_item(
        self, task: Task, item: dict[str, Any], parent_target_id: str, base_path: str,
        report: SyncReport, seen: set[str], item_cache: dict[str, SyncedItem],
        pending_upserts: dict[str, SyncedItem],
    ) -> None:
        """Обрабатывает один файл; запускается только внутри ограниченной пачки."""
        source_id = str(item["id"])
        name = str(item.get("name", source_id))
        mime = str(item.get("mimeType", ""))
        path = f"{base_path}/{name}" if base_path else name
        if self._should_stop(task.id):
            return
        if is_excluded(name, task.exclude_patterns):
            report.excluded_names.append(path)
            return
        seen.add(source_id)
        report.checked += 1
        try:
            if mime in NOT_SYNCABLE_MIMES:
                report.skipped += 1
            elif mime in NATIVE_EXPORT:
                await self._sync_native_doc(task, item, parent_target_id, path, report, item_cache, pending_upserts)
            else:
                await self._sync_regular_file(task, item, parent_target_id, path, report, item_cache, pending_upserts)
        except DriveError as exc:
            report.errors.append(f"«{path}»: {exc}")

    async def _notify_progress(self, report: SyncReport, progress: ProgressCallback | None) -> None:
        if progress is not None:
            try:
                await progress(report)
            except Exception:  # noqa: BLE001
                log.debug("Не обновлён прогресс задачи #%s", report.task_id, exc_info=True)

    # --------------------------------------------------- папки назначения

    async def _ensure_target_folder(
        self,
        task: Task,
        folder_item: dict[str, Any],
        parent_target_id: str,
        report: SyncReport,
        item_cache: dict[str, SyncedItem],
        pending_upserts: dict[str, SyncedItem],
    ) -> str | None:
        """Возвращает ID подпапки в назначении, создавая её при первом проходе."""
        record = item_cache.get(folder_item["id"])
        if record is not None:
            return record.target_id

        try:
            # Для нового source_id поиск по имени не нужен: минус один API list-запрос
            # на каждую новую папку. Повторный запуск использует запись из SQLite.
            target_id = await self._drive.create_folder(
                folder_item.get("name", ""), parent_target_id
            )
        except DriveError as exc:
            report.errors.append(
                f"Не удалось создать папку «{folder_item.get('name', '')}»: {exc}"
            )
            report.listing_ok = False
            return None

        record = SyncedItem(
            task_id=task.id, source_id=folder_item["id"], target_id=target_id,
            file_name=folder_item.get("name", ""), mime_type=FOLDER_MIME,
            modified_time=folder_item.get("modifiedTime"), size="", last_synced_at="",
        )
        # Папку фиксируем немедленно: если процесс упадёт, следующий запуск
        # возьмёт её ID из SQLite и не создаст дубль на Drive.
        await self._db.upsert_synced_items([record])
        item_cache[record.source_id] = record
        report.created_folders += 1
        if task.template_enabled and task.template_data and task.template_name:
            if task.template_delay_sec == 0:
                try:
                    await self._drive.upload_bytes(
                        task.template_name, task.template_mime or "application/octet-stream",
                        target_id, task.template_data,
                    )
                except DriveError as exc:
                    report.errors.append(f"Не добавлен шаблон в папку «{folder_item.get('name', '')}»: {exc}")
            else:
                due_at = datetime.now(timezone.utc) + timedelta(seconds=task.template_delay_sec)
                await self._db.enqueue_template(task.id, target_id, due_at.isoformat())
        return target_id

    async def _apply_template(
        self,
        task: Task,
        target_folder_id: str,
        report: SyncReport,
        folder_name: str,
    ) -> None:
        """Добавляет шаблон в существующую или новую целевую подпапку.

        Раньше шаблон обрабатывался только внутри ветки создания новой папки.
        Поэтому шаблон, заданный после клонирования, не попадал в уже созданное
        дерево. Теперь каждая целевая подпапка проверяется при обходе.
        """
        if not (task.template_enabled and task.template_data and task.template_name):
            return

        try:
            # Сначала проверяем папку назначения. Это защищает от дублей и для
            # немедленного, и для отложенного добавления.
            target_items = await self._drive.list_folder(target_folder_id)
            if any(str(item.get("name", "")) == task.template_name for item in target_items):
                return

            if task.template_delay_sec > 0:
                # INSERT OR IGNORE сохраняет первоначальный due_at и не сдвигает
                # срок при каждом последующем проходе синхронизации.
                due_at = datetime.now(timezone.utc) + timedelta(seconds=task.template_delay_sec)
                await self._db.enqueue_template(task.id, target_folder_id, due_at.isoformat())
                return

            await self._drive.upload_bytes(
                task.template_name,
                task.template_mime or "application/octet-stream",
                target_folder_id,
                task.template_data,
            )
        except DriveError as exc:
            report.errors.append(
                f"Не добавлен шаблон в папку «{folder_name}»: {exc}"
            )
            report.listing_ok = False


    @staticmethod
    def _is_unchanged(record: SyncedItem | None, item: dict[str, Any]) -> bool:
        """Сверка по modifiedTime + size (ID уже является ключом в БД)."""
        if record is None:
            return False
        if record.modified_time != (item.get("modifiedTime") or ""):
            return False
        new_size = item.get("size")
        if new_size and record.size != new_size:  # у нативных файлов size нет
            return False
        return True

    async def _sync_regular_file(
        self,
        task: Task,
        item: dict[str, Any],
        parent_target_id: str,
        path: str,
        report: SyncReport,
        item_cache: dict[str, SyncedItem],
        pending_upserts: dict[str, SyncedItem],
    ) -> None:
        source_id: str = item["id"]
        name: str = item["name"]
        mime: str = item["mimeType"]
        modified: str = item.get("modifiedTime") or ""
        size: str = item.get("size") or ""

        record = item_cache.get(source_id)

        if self._is_unchanged(record, item):
            # контент не менялся, но имя могли переименовать в источнике
            if record is not None and record.file_name != name:
                try:
                    await self._drive.rename_file(record.target_id, name)
                    updated_record = SyncedItem(
                        task.id, source_id, record.target_id, name, mime, modified, size, ""
                    )
                    item_cache[source_id] = updated_record
                    pending_upserts[source_id] = updated_record
                    report.changes.append(Change(KIND_RENAMED, name, path))
                except DriveError as exc:
                    report.errors.append(f"«{path}»: не удалось переименовать: {exc}")
            else:
                report.skipped += 1
            return

        used_fallback = False
        docx_note = ""
        kind = KIND_NEW if record is None else KIND_UPDATED
        try:
            if mime == DOCX_MIME or name.lower().endswith(".docx"):
                # DOCX сначала обрабатываем локально: files.copy не позволяет
                # удалить только нужную картинку внутри архива.
                content = await self._drive.download_bytes(source_id)
                content, removed = _prepare_docx_first_image(content)
                used_fallback = True
                docx_note = "удалена первая картинка до текста" if removed else "картинка до текста не найдена"
                if record is not None:
                    updated = await self._drive.update_file(record.target_id, name, mime, content)
                    target_id = str(updated["id"])
                else:
                    uploaded = await self._drive.upload_bytes(name, mime, parent_target_id, content)
                    target_id = str(uploaded["id"])
            else:
                try:
                    # Основной путь: серверное копирование без скачивания
                    copied = await self._drive.copy_file(source_id, name, parent_target_id)
                    target_id = str(copied["id"])
                    if record is not None:
                        # старую версию удаляем, чтобы не плодить дубликаты
                        await self._drive.delete_file_quietly(record.target_id)
                except DriveError as copy_exc:
                    # Fallback: не хватает прав на files.copy -> качаем и заливаем сами
                    log.info("files.copy не сработал (%s) — download+upload: %s", copy_exc, name)
                    content = await self._drive.download_bytes(source_id)
                    used_fallback = True
                    if record is not None:
                        updated = await self._drive.update_file(record.target_id, name, mime, content)
                        target_id = str(updated["id"])
                    else:
                        uploaded = await self._drive.upload_bytes(name, mime, parent_target_id, content)
                        target_id = str(uploaded["id"])
        except DriveError as exc:
            report.listing_ok = False
            report.errors.append(f"«{path}»: {exc}")
            return

        saved_record = SyncedItem(
            task.id, source_id, target_id, name, mime, modified, size, ""
        )
        item_cache[source_id] = saved_record
        pending_upserts[source_id] = saved_record
        note = docx_note if (mime == DOCX_MIME or name.lower().endswith(".docx")) else ("скачан и загружен вручную" if used_fallback else "")
        report.changes.append(Change(kind, name, path, note))

    # ------------------------------------------------- Google Workspace

    async def _sync_native_doc(
        self,
        task: Task,
        item: dict[str, Any],
        parent_target_id: str,
        path: str,
        report: SyncReport,
        item_cache: dict[str, SyncedItem],
        pending_upserts: dict[str, SyncedItem],
    ) -> None:
        source_id: str = item["id"]
        name: str = item["name"]
        mime: str = item["mimeType"]
        export_mime, ext = NATIVE_EXPORT[mime]
        target_name = f"{name}{ext}"
        modified: str = item.get("modifiedTime") or ""

        record = item_cache.get(source_id)

        if self._is_unchanged(record, item):
            if record is not None and record.file_name != target_name:
                try:
                    await self._drive.rename_file(record.target_id, target_name)
                    updated_record = SyncedItem(
                        task.id, source_id, record.target_id, target_name, export_mime,
                        modified, record.size, ""
                    )
                    item_cache[source_id] = updated_record
                    pending_upserts[source_id] = updated_record
                    report.changes.append(Change(KIND_RENAMED, target_name, path, "экспорт"))
                except DriveError as exc:
                    report.errors.append(f"«{path}»: не удалось переименовать: {exc}")
            else:
                report.skipped += 1
            return

        kind = KIND_NEW if record is None else KIND_UPDATED
        try:
            content = await self._drive.export_bytes(source_id, export_mime)
            docx_note = ""
            if export_mime == DOCX_MIME:
                content, removed = _prepare_docx_first_image(content)
                docx_note = "удалена первая картинка до текста" if removed else "картинка до текста не найдена"
            if record is not None:
                updated = await self._drive.update_file(
                    record.target_id, target_name, export_mime, content
                )
                target_id = str(updated["id"])
            else:
                uploaded = await self._drive.upload_bytes(
                    target_name, export_mime, parent_target_id, content
                )
                target_id = str(uploaded["id"])
        except DriveError as exc:
            report.errors.append(f"«{path}» (Google Workspace): {exc}")
            return

        saved_record = SyncedItem(
            task.id, source_id, target_id, target_name, export_mime,
            modified, str(len(content)), ""
        )
        item_cache[source_id] = saved_record
        pending_upserts[source_id] = saved_record
        note = "экспорт из Google Workspace"
        if docx_note:
            note += "; " + docx_note
        report.changes.append(Change(kind, target_name, path, note))


def render_report(report: SyncReport) -> str:
    """Формирует HTML-отчёт о синхронизации для Telegram."""
    finished = report.finished_at or datetime.now(timezone.utc)
    duration = (finished - report.started_at).total_seconds()

    def esc(value: object) -> str:
        return escape(str(value), quote=False)

    lines: list[str] = [
        f"🔄 <b>Синхронизация «{esc(report.task_title)}»</b>",
        f"⏱ {duration:.0f} сек • проверено: {report.checked} • без изменений: "
        f"{report.skipped} • исключено по маске: {len(report.excluded_names)} • "
        f"папок создано: {report.created_folders}",
    ]

    groups: list[tuple[str, str, list[Change]]] = [
        ("🆕", "Новые файлы", [c for c in report.changes if c.kind == KIND_NEW]),
        ("♻️", "Обновлённые файлы", [c for c in report.changes if c.kind == KIND_UPDATED]),
        ("✏️", "Переименованные", [c for c in report.changes if c.kind == KIND_RENAMED]),
    ]
    for emoji, title, group in groups:
        if not group:
            continue
        lines.append(f"{emoji} <b>{title} ({len(group)})</b>")
        for change in group[:MAX_LISTED]:
            note = f" <i>({esc(change.note)})</i>" if change.note else ""
            lines.append(f"• {esc(change.path)}{note}")
        if len(group) > MAX_LISTED:
            lines.append(f"• …и ещё {len(group) - MAX_LISTED}")

    if report.deleted:
        lines.append(f"🗑 <b>Удалены в назначении ({len(report.deleted)})</b>")
        for name in report.deleted[:MAX_LISTED]:
            lines.append(f"• {esc(name)}")
        if len(report.deleted) > MAX_LISTED:
            lines.append(f"• …и ещё {len(report.deleted) - MAX_LISTED}")

    if report.excluded_names:
        lines.append(f"🚫 <b>Пропущены по маске исключения ({len(report.excluded_names)})</b>")
        for name in report.excluded_names[:MAX_LISTED]:
            lines.append(f"• {esc(name)}")
        if len(report.excluded_names) > MAX_LISTED:
            lines.append(f"• …и ещё {len(report.excluded_names) - MAX_LISTED}")

    if report.stale:
        lines.append(f"❓ <b>Исчезли из источника ({len(report.stale)})</b>")
        for name in report.stale[:MAX_LISTED]:
            lines.append(f"• {esc(name)}")
        if len(report.stale) > MAX_LISTED:
            lines.append(f"• …и ещё {len(report.stale) - MAX_LISTED}")

    if report.errors:
        lines.append(f"⚠️ <b>Ошибки ({len(report.errors)})</b>")
        for error in report.errors[:MAX_LISTED]:
            lines.append(f"• {esc(error)}")
        if len(report.errors) > MAX_LISTED:
            lines.append(f"• …и ещё {len(report.errors) - MAX_LISTED}")

    if not report.changes and not report.errors and not report.stale and not report.deleted \
            and not report.excluded_names:
        lines.append("✅ Изменений нет — всё актуально.")

    return "\n".join(lines)
