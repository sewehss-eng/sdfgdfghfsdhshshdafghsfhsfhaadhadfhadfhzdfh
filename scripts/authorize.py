"""Одноразовая OAuth-авторизация в Google Drive -> token.json.

Запускайте на машине с браузером:
    python scripts/authorize.py

В браузере откроется окно согласия Google — подтвердите доступ.
Файл token.json появится в корне проекта.

Если бот живёт на удалённом сервере без браузера:
запустите этот скрипт локально и скопируйте получившийся token.json на сервер.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Скрипт лежит в scripts/, а импортирует модули из корня проекта
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config  # noqa: E402
from services.drive import SCOPES  # noqa: E402


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Сначала установите зависимости: pip install -r requirements.txt") from exc

    flow = InstalledAppFlow.from_client_secrets_file(config.credentials_file, SCOPES)
    # prompt="consent" гарантирует выдачу refresh-токена
    credentials = flow.run_local_server(
        port=0,
        prompt="consent",
        success_message="Готово! Вернитесь в терминал.",
    )

    Path(config.token_file).write_text(credentials.to_json(), encoding="utf-8")
    print(f"✅ Токен сохранён: {config.token_file}")


if __name__ == "__main__":
    main()
