"""Авторизация и работа с Google Drive API v3.

googleapiclient — синхронный, поэтому каждый вызов выполняется через
asyncio.to_thread: event loop бота не блокируется. Все запросы повторяются
с экспоненциальной задержкой при 429/5xx.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import socket
import ssl
from pathlib import Path
from typing import Any, Callable

import httplib2
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

log = logging.getLogger(__name__)

SCOPES: list[str] = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME = "application/vnd.google-apps.folder"

# Нативные форматы Google Workspace нельзя скопировать напрямую — только экспорт.
# Схема: mime источника -> (целевой mime для export, расширение файла)
NATIVE_EXPORT: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Ошибки сети/TLS-рукопожатия: их тоже повторяем, а при исчерпании попыток
# отдаём понятную ошибку. Типичные причины WRONG_VERSION_NUMBER: выключен
# VPN/прокси, блокировка googleapis.com провайдером, схема прокси указана
# как https:// вместо http://.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ssl.SSLError,
    socket.timeout,
    ConnectionError,
    TimeoutError,
    httplib2.HttpLib2Error,
    httplib2.ServerNotFoundError,
)

LIST_FIELDS = "nextPageToken, files(id,name,mimeType,modifiedTime,size,md5Checksum)"

_FOLDER_LINK_RE = re.compile(r"drive\.google\.com/drive(?:/u/\d+)?/folders/([A-Za-z0-9_-]{10,})")
_FILE_LINK_RE = re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]{10,})")
_ID_PARAM_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})")
_PLAIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class DriveError(Exception):
    """Базовая ошибка сервиса Drive."""


class DriveAuthError(DriveError):
    """Проблемы авторизации: нет token.json, токен невалиден и т.п."""


class DriveHTTPError(DriveError):
    """HTTP-ошибка API после всех попыток повтора."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def extract_folder_id(text: str) -> str | None:
    """Извлекает Folder ID из ссылок любого формата или принимает «сырой» ID.

    Поддерживаемые варианты:
      * https://drive.google.com/drive/folders/ID[?usp=sharing]
      * https://drive.google.com/drive/u/0/folders/ID
      * https://drive.google.com/open?id=ID
      * любой текст, содержащий ?id=ID
      * сам ID (20+ символов)
    """
    text = text.strip()
    if match := _FOLDER_LINK_RE.search(text):
        return match.group(1)
    if match := _ID_PARAM_RE.search(text):
        return match.group(1)
    if _PLAIN_ID_RE.match(text):
        return text
    return None


def extract_drive_id(text: str) -> str | None:
    """Как extract_folder_id, но дополнительно понимает ссылки на файлы
    (drive.google.com/file/d/ID/...). Используется командой /clone, которая
    принимает и папки, и одиночные файлы.
    """
    text = text.strip()
    if match := _FOLDER_LINK_RE.search(text):
        return match.group(1)
    if match := _FILE_LINK_RE.search(text):
        return match.group(1)
    if match := _ID_PARAM_RE.search(text):
        return match.group(1)
    if _PLAIN_ID_RE.match(text):
        return text
    return None


class DriveClient:
    """Асинхронная обёртка над googleapiclient с backoff и локами."""

    def __init__(
        self,
        credentials_file: str,
        token_file: str,
        max_retries: int = 5,
        strip_fragments: tuple[str, ...] = (),
    ) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._max_retries = max(1, max_retries)
        self._strip_fragments = tuple(f for f in strip_fragments if f)
        self._service: Any | None = None
        self._build_lock = asyncio.Lock()

    # --------------------------------------------------------- авторизация

    async def ensure_authorized(self) -> None:
        """Проверяет, что сервис можно построить. Бросает DriveAuthError при проблемах."""
        await self._get_service()

    async def _get_service(self) -> Any:
        if self._service is None:
            async with self._build_lock:
                if self._service is None:  # double-checked locking
                    self._service = await asyncio.to_thread(self._build_service_sync)
        return self._service

    def _build_service_sync(self) -> Any:
        creds = self._load_credentials_sync()
        # Явный timeout предотвращает бесконечное зависание запроса. Для
        # googleapiclient используем AuthorizedHttp, чтобы credentials точно
        # применялись и к httplib2-соединению.
        http = httplib2.Http(timeout=120)
        authorized_http = AuthorizedHttp(creds, http=http)
        return build(
            "drive", "v3", http=authorized_http, cache_discovery=False
        )

    def _load_credentials_sync(self) -> Credentials:
        """Читает token.json, при необходимости обновляет refresh-токеном."""
        token_path = Path(self._token_file)
        if not token_path.is_file():
            raise DriveAuthError(
                f"Файл {self._token_file} не найден. Выполните одноразовую авторизацию: "
                "python scripts/authorize.py (подробности в README)."
            )
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
                log.info("Access-токен обновлён и сохранён в %s", self._token_file)
            else:
                raise DriveAuthError(
                    f"Токен в {self._token_file} невалиден — пройдите авторизацию заново"
                )
        return creds

    # -------------------------------------------------- ядро retry/backoff

    async def _with_backoff(self, execute: Callable[[], Any]) -> Any:
        """Выполняет переданную операцию в потоке с экспоненциальным backoff.

        `execute` вызывается заново на каждой попытке, поэтому request-объекты
        (и медиа-потоки) пересоздаются с чистого листа.
        """
        attempt = 0
        while True:
            try:
                return await asyncio.to_thread(execute)
            except HttpError as exc:
                status = int(getattr(exc.resp, "status", -1) or -1)
                if status in RETRYABLE_STATUSES and attempt < self._max_retries:
                    delay = min(2 ** attempt + random.uniform(0.0, 1.0), 60.0)
                    log.warning(
                        "Drive API: HTTP %s — повтор через %.1f c (попытка %d/%d)",
                        status, delay, attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise DriveHTTPError(status, self._http_error_message(exc)) from exc
            except NETWORK_ERRORS as exc:
                if attempt < self._max_retries:
                    delay = min(2 ** attempt + random.uniform(0.0, 1.0), 60.0)
                    log.warning(
                        "Drive API: сетевая ошибка (%s) — повтор через %.1f c (попытка %d/%d)",
                        exc, delay, attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise DriveError(
                    f"Нет связи с Google Drive API: {exc}. Проверьте интернет и VPN/прокси: "
                    "чаще всего прокси-клиент выключен или блокируется доступ к googleapis.com. "
                    "Если прокси нужен — задайте HTTPS_PROXY=http://127.0.0.1:ПОРТ (схема http://)."
                ) from exc

    async def _call(self, build_request: Callable[[Any], Any]) -> Any:
        """Универсальный вызов JSON-метода API (не медиа)."""
        service = await self._get_service()
        return await self._with_backoff(lambda: build_request(service).execute())

    async def _download(self, build_request: Callable[[Any], Any]) -> bytes:
        """Скачивание медиа-контента (files.get / files.export) чанками."""
        service = await self._get_service()
        return await self._with_backoff(
            lambda: self._download_sync(build_request(service))
        )

    @staticmethod
    def _download_sync(request: Any) -> bytes:
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    @staticmethod
    def _http_error_message(exc: HttpError) -> str:
        """Достаёт человекочитаемое сообщение из тела ошибки."""
        try:
            data = json.loads(exc.content.decode("utf-8"))
            return str(data.get("error", {}).get("message", str(exc)))
        except Exception:  # noqa: BLE001
            return str(exc)

    # -------------------------------------------------------- операции API

    async def get_meta(
        self, file_id: str, fields: str = "id,name,mimeType"
    ) -> dict[str, Any] | None:
        """Метаданные элемента; None, если нет доступа или не существует."""
        try:
            return await self._call(
                lambda s: s.files().get(fileId=file_id, fields=fields, supportsAllDrives=True)
            )
        except DriveHTTPError as exc:
            if exc.status in (403, 404):
                return None
            raise

    async def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        """Все неудалённые дети папки (с пагинацией). Папки идут первыми.

        `includeItemsFromAllDrives`/`supportsAllDrives` обязательны, иначе
        содержимое папок на Shared Drive просто не возвращается API.
        """
        query = f"'{folder_id}' in parents and trashed = false"
        return await self._list_query(query)

    async def search(self, text: str, parent_id: str | None = None) -> list[dict[str, Any]]:
        """Ищет файлы и папки по подстроке имени. parent_id ограничивает поиск одной папкой."""
        escaped = text.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name contains '{escaped}' and trashed = false"
        if parent_id and parent_id != "root":
            query += f" and '{parent_id}' in parents"
        return await self._list_query(query, page_size=50, max_items=30)

    async def _list_query(
        self, query: str, page_size: int = 500, max_items: int | None = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            token = page_token

            def build(s: Any, token: str | None = token) -> Any:  # noqa: B008
                return s.files().list(
                    q=query, fields=LIST_FIELDS, pageSize=page_size,
                    pageToken=token, orderBy="folder,name",
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                )

            result = await self._call(build)
            items.extend(result.get("files", []))
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            page_token = result.get("nextPageToken")
            if not page_token:
                return items

    async def create_folder(self, name: str, parent_id: str) -> str:
        """Создаёт папку без предварительного поиска.

        Используется синхронизацией только для нового source_id: журнал SQLite уже
        гарантирует, что в рамках нормального повторного запуска дублей не будет.
        """
        created = await self._call(
            lambda s: s.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id", supportsAllDrives=True,
            )
        )
        return str(created["id"])

    async def ensure_folder(self, name: str, parent_id: str) -> str:
        """Находит подпапку по имени внутри родителя или создаёт новую.

        Возвращает ID существующей/созданной папки — синхронизация идемпотентна.
        """
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped}' and '{parent_id}' in parents and "
            f"mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        result = await self._call(
            lambda s: s.files().list(
                q=query, fields="files(id)", pageSize=10,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            )
        )
        found = result.get("files", [])
        if found:
            return str(found[0]["id"])
        created = await self._call(
            lambda s: s.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id", supportsAllDrives=True,
            )
        )
        return str(created["id"])

    async def copy_file(self, source_id: str, name: str, parent_id: str) -> dict[str, Any]:
        """Серверное копирование files.copy — трафик через наш сервер не идёт."""
        name = self._clean_name(name)
        return await self._call(
            lambda s: s.files().copy(
                fileId=source_id, supportsAllDrives=True,
                body={"name": name, "parents": [parent_id]}, fields="id,name",
            )
        )

    async def download_bytes(self, file_id: str) -> bytes:
        """Скачивает файл в память (fallback-путь).

        `get_media` — настоящий download (`alt=media`), поэтому
        `acknowledgeAbuse` передаётся его официальным параметром. В обычный
        `files().get()` этот параметр передавать нельзя.
        """

        def build(s: Any) -> Any:
            # acknowledgeAbuse допустим только для download-запроса.
            # get_media формирует именно такой запрос (alt=media).
            return s.files().get_media(
                fileId=file_id,
                acknowledgeAbuse=True,
                supportsAllDrives=True,
            )

        return await self._download(build)

    async def export_bytes(self, file_id: str, target_mime: str) -> bytes:
        """Экспорт нативного Google-документа в выбранный формат."""
        return await self._download(
            lambda s: s.files().export(fileId=file_id, mimeType=target_mime)
        )

    async def upload_bytes(
        self, name: str, mime: str, parent_id: str, content: bytes
    ) -> dict[str, Any]:
        """Загружает файл из памяти (resumable — переживёт большие файлы)."""
        name = self._clean_name(name)

        def build(s: Any) -> Any:
            # медиа создаётся заново на каждой попытке — свежий BytesIO
            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime, resumable=True)
            return s.files().create(
                body={"name": name, "parents": [parent_id]},
                media_body=media, fields="id,name", supportsAllDrives=True,
            )

        return await self._call(build)

    async def update_file(
        self, file_id: str, name: str, mime: str, content: bytes
    ) -> dict[str, Any]:
        """Обновляет содержимое уже существующего файла (in-place)."""
        name = self._clean_name(name)

        def build(s: Any) -> Any:
            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime, resumable=True)
            return s.files().update(
                fileId=file_id, body={"name": name}, media_body=media,
                fields="id,name", supportsAllDrives=True,
            )

        return await self._call(build)

    async def rename_file(self, file_id: str, new_name: str) -> None:
        new_name = self._clean_name(new_name)
        await self._call(
            lambda s: s.files().update(
                fileId=file_id, body={"name": new_name}, fields="id", supportsAllDrives=True
            )
        )

    async def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str) -> None:
        """Перемещает файл или папку, меняя родителя (содержимое не копируется)."""
        await self._call(
            lambda s: s.files().update(
                fileId=file_id,
                addParents=new_parent_id,
                removeParents=old_parent_id,
                fields="id,parents",
                supportsAllDrives=True,
            )
        )

    async def delete_file(self, file_id: str) -> None:
        await self._call(lambda s: s.files().delete(fileId=file_id, supportsAllDrives=True))

    async def share_with_anyone(self, file_id: str, role: str = "reader") -> None:
        """Открывает доступ «по ссылке» (type=anyone) — нужно для команды /clone,
        чтобы пользователь сразу получил рабочую ссылку на свежую копию.
        """
        await self._call(
            lambda s: s.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": role},
                fields="id",
                supportsAllDrives=True,
            )
        )

    async def delete_file_quietly(self, file_id: str) -> None:
        """Удаление старой копии: ошибка не должна ломать синхронизацию."""
        try:
            await self.delete_file(file_id)
        except DriveError as exc:
            log.warning("Не удалось удалить старую копию %s: %s", file_id, exc)

    # ------------------------------------------- очистка имён файлов

    # Автоматически удаляемые паттерны (regex) — применяются до текстовых фрагментов.
    # Добавляй сюда новые регулярки для мусора в именах файлов.
    AUTO_STRIP_PATTERNS: tuple[re.Pattern[str], ...] = (
        # Telegram-бот хвосты: " tg:NCEI4SLIVOGE", " tg:ABC123XYZ" и т.д.
        # Вырезает пробел + "tg:" + любые латинские буквы/цифры/подчёркивания/дефисы
        re.compile(r"\s+tg:[A-Za-z0-9_-]+", re.IGNORECASE),
    )

    @classmethod
    def clean_filename(cls, name: str, fragments: list[str]) -> str | None:
        """Удаляет из имени файла мусорные паттерны и заданные фрагменты.

        Сначала применяются AUTO_STRIP_PATTERNS (регулярки), затем точечные
        фрагменты из списка (регистронезависимо). Возвращает новое имя, если
        оно изменилось, или None, если чистить нечего.
        """
        original = name

        # 1. Автоматические regex-паттерны (tg:хвосты и прочий мусор)
        for pat in cls.AUTO_STRIP_PATTERNS:
            name = pat.sub("", name)

        # 2. Пользовательские фрагменты из конфига
        for frag in fragments:
            if not frag:
                continue
            pattern = re.compile(re.escape(frag), re.IGNORECASE)
            name = pattern.sub("", name)

        # схлопываем множественные пробелы
        name = re.sub(r" {2,}", " ", name).strip()

        # убираем пробел перед расширением: "file .pdf" -> "file.pdf"
        name = re.sub(r"\s+\.([^.]+)$", r".\1", name)

        # если после очистки имя пустое — оставляем оригинал
        if not name or name == ".":
            return None

        return name if name != original else None

    def _clean_name(self, name: str) -> str:
        """Применяет очистку имени (regex-паттерны + фрагменты из конфига).

        Используется при копировании/загрузке/переименовании. Если чистить
        нечего — возвращается исходное имя без изменений.
        """
        cleaned = self.clean_filename(name, list(self._strip_fragments))
        return cleaned if cleaned is not None else name

    async def batch_clean_names(
        self,
        folder_id: str,
        fragments: list[str],
        *,
        recursive: bool = False,
        progress_cb: Callable[[int, int, str], Any] | None = None,
    ) -> dict[str, Any]:
        """Проходит по папке и переименовывает файлы, удаляя фрагменты из имён.

        Работает через серверный rename (без скачивания) — моментально.

        Args:
            folder_id: ID папки для обработки.
            fragments: список подстрок, которые нужно удалить из имён.
            recursive: если True, обрабатывает вложенные папки.
            progress_cb: опциональный колбэк (processed, total, current_name).

        Returns:
            Словарь с результатами: renamed, skipped, errors.
        """
        items = await self.list_folder(folder_id)
        stats = {"renamed": 0, "skipped": 0, "errors": 0, "details": []}

        files_to_process = [f for f in items if f.get("mimeType") != FOLDER_MIME]
        folders_to_process = [f for f in items if f.get("mimeType") == FOLDER_MIME]
        total = len(files_to_process)

        for idx, item in enumerate(files_to_process, 1):
            file_id = item["id"]
            old_name = item.get("name", "")
            new_name = self.clean_filename(old_name, fragments)

            if progress_cb:
                try:
                    await asyncio.to_thread(progress_cb, idx, total, old_name)
                except Exception:
                    pass

            if new_name is None:
                stats["skipped"] += 1
                continue

            try:
                await self.rename_file(file_id, new_name)
                stats["renamed"] += 1
                stats["details"].append({"id": file_id, "old": old_name, "new": new_name})
                log.info("Переименован: %r -> %r", old_name, new_name)
            except DriveError as exc:
                stats["errors"] += 1
                stats["details"].append({"id": file_id, "old": old_name, "error": str(exc)})
                log.warning("Ошибка переименования %r: %s", old_name, exc)

        # Рекурсивно обрабатываем подпапки (их имена тоже чистим)
        if recursive:
            for folder in folders_to_process:
                f_id = folder["id"]
                f_old_name = folder.get("name", "")
                f_new_name = self.clean_filename(f_old_name, fragments)
                if f_new_name:
                    try:
                        await self.rename_file(f_id, f_new_name)
                        stats["renamed"] += 1
                        stats["details"].append({"id": f_id, "old": f_old_name, "new": f_new_name})
                    except DriveError as exc:
                        stats["errors"] += 1
                        log.warning("Ошибка переименования папки %r: %s", f_old_name, exc)

                sub_stats = await self.batch_clean_names(
                    f_id, fragments, recursive=True, progress_cb=progress_cb
                )
                stats["renamed"] += sub_stats["renamed"]
                stats["skipped"] += sub_stats["skipped"]
                stats["errors"] += sub_stats["errors"]
                stats["details"].extend(sub_stats["details"])

        return stats
