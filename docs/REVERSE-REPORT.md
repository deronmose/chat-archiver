# Реверс-анализ unnohwn/telegram-scraper и обоснование tg-export

Дата: 2026-10-30. Инструмент: REA 6.8.0 (MCP), Python 3.10 / 3.12.

## 1. Объект

| | |
|---|---|
| Проект | unnohwn/telegram-scraper (оригинал на GitHub удалён; форк DarkWebInformer тоже удалён) |
| Источник | зеркало SourceForge, `git clone https://git.code.sf.net/p/telegram-scraper/code` |
| Коммит | `8d4e092b1b5704866afb76a658eadf67ba1ce894` (2025-09-11, «Update telegram-scraper.py») |
| `telegram-scraper.py` | 37 361 байт, SHA-256 `7B678687F61D41063F21A9FA590B911E6A14CFE06F74AF1CFB937CFB4D723A8D` |
| `requirements.txt` | SHA-256 `FE5E5B05753CA76EB581CD24C9269D8E722BF8D00BA529FD7BBE3F3C6AA08EE1` |

## 2. Метод

Исходник скомпилирован в байткод без выполнения (`py_compile`, CPython 3.10) и разобран
`inspect_python_pyc` REA: полный обход marshal-дерева, все объекты кода, их имена (`co_names` —
каждый модуль, атрибут и глобальное имя, к которому обращается код) и константы (`co_consts` —
все строки и числа).

- Evidence: `ev_22f6975fdc7edd6883151e2c7326c631fce0f4ad97a1d7b5d348f2ce3797b8cb`
- Покрытие: `complete` — 45/45 объектов кода, 1 557 объектов marshal, pyc SHA-256 `6fe3e08e…f9116`.
- Ограничение REA: байткод не декомпилируется — анализ имён и констант, не потока данных.
  Поведение дополнительно сверено чтением исходника.

## 3. Наблюдения (REA)

**Модули**: `os, sqlite3, json, csv, asyncio, time, sys, uuid, warnings, dataclasses, typing,
pathlib, io, telethon, qrcode`. (`uuid` импортирован, но не используется.)

**Нет ни в одном объекте кода**: `socket`, `requests`, `urllib`, `http`, `aiohttp`, `subprocess`,
`os.system`, `exec`, `eval`, `compile`, `__import__`, `base64`, `marshal`, `pickle`, `zlib`, `ctypes`.

**Единственный URL среди констант**: `https://my.telegram.org` — текст подсказки (`print`).
Закодированных/обфусцированных строк нет.

**Сеть**: только методы `TelegramClient` — `connect, is_user_authorized, qr_login, send_code_request,
sign_in, iter_dialogs, get_entity, get_messages, iter_messages, get_sender, download_media, disconnect`.

**Вывод**: признаков вредоносного кода, утечки данных или скрытых соединений нет.

## 4. Найденные недостатки оригинала

| # | Проблема | Риск |
|---|---|---|
| 1 | `api_id`/`api_hash` в `state.json` открытым текстом | утечка ключей |
| 2 | `session.session` (полный доступ к аккаунту) открытым текстом, рядом со скриптом | захват аккаунта при копировании папки |
| 3 | Пароль 2FA и код через `input()` — видны на экране | подсмотр |
| 4 | Позиция возобновления сохраняется до записи пачки в БД | пропуск до 99 сообщений после сбоя |
| 5 | `get_sender()` на каждое сообщение | лишние запросы, риск FloodWait |
| 6 | FloodWait обрабатывается только при скачивании медиа | обрыв на больших чатах |
| 7 | Нет ограничения действий клиента | любая ошибка/правка кода может писать в аккаунт |
| 8 | Нельзя указать ссылку-приглашение, @имя, t.me/… — только номер из списка/ID | неудобство |
| 9 | CSV без защиты от формул Excel | CSV-injection из чужих сообщений |
| 10 | Пустые `except:` | молча неполные данные |
| 11 | Зависимости без хешей; лишний пакет `asyncio==3.4.3` | supply chain |

## 5. tg-export: что сделано иначе

| Недостаток | Решение в tg-export | Где |
|---|---|---|
| 1, 2 | ключи и сессия в одном файле, зашифрованы DPAPI | `vault.py` |
| 3 | `getpass` для кода, пароля и `api_hash` | `cli.py` |
| 4 | позиция = `MAX(id)` уже записанных строк — не может обогнать данные | `store.py` |
| 5 | отправитель берётся из уже полученного ответа (`_sender`), без запросов | `scraper.py` |
| 6 | `flood_sleep_threshold=3600` — ожидание FloodWait до часа автоматически | `cli.py` |
| 7 | **фильтр запросов**: переопределён `TelegramClient._call`, через который идут все запросы Telethon; allowlist чтения, запись заблокирована | `guard.py` |
| 8 | разбор `t.me/+…`, `joinchat/`, `tg://join`, `t.me/имя`, `t.me/c/…`, `@имя`, ID | `refs.py` |
| 9 | ячейки на `= + - @` экранируются; HTML экранирован + CSP без скриптов | `exporters.py` |
| 10 | ошибки медиа записываются в `media_error`, повтор `--retry-failed` | `scraper.py`, `store.py` |
| 11 | Telethon 1.45.0, qrcode 8.2, хеши SHA-256, `--require-hashes` | `requirements.txt` |

Дополнительно: имена медиа очищаются (пути, `..`, запрещённые символы, CON/NUL…), путь проверяется
перед записью; запись файлов атомарная (`.part`/`.tmp` → rename); поля `edit_date, fwd_from, views,
forwards, post_author, grouped_id, service` (в оригинале их нет).

## 6. Проверка tg-export

**REA, тем же методом** (CPython 3.12, покрытие `complete` для всех модулей):

| Модуль | Evidence |
|---|---|
| cli | `ev_40ca2dfc546f4d5e23b050073a43e20599b9bbd9edbd464bbe9930da4b8ac0bd` |
| scraper | `ev_7728e25ef6de6eebe53ce476b59483dbbaa306417aa07dc223e5fdbac066d455` |
| guard | `ev_37fa8af62afc41488c765f8afa2cc425528a18366185d140a68afbf9af7c4169` |
| vault | `ev_76ad18817b575c3eb62e1d4346e95acc8359d60547e02ed91ac15870daa57d3a` |
| exporters | `ev_a3b2613a7609ae31a20d0703c50843947fe195e584438aa683e6a4879153bb23` |
| store | `ev_6cf4460ef8997a86655610a77278ff7927874be73fa64b2a74858f2de46071e5` |
| refs | `ev_0ae0be0127349a0bf9bf202dae8fb3de40cbea92266391d7486636a95d50a014` |

Сеть — только методы Telethon. `ctypes` — только `crypt32`/`kernel32` (DPAPI, `vault.py`).
`urllib.parse.quote` — кодирование путей в HTML, не сеть.

**Живая проверка фильтра** (фиктивные ключи, без аккаунта): подключение к Telegram и проверка
авторизации проходят через фильтр; `messages.SendMessageRequest` заблокирован до отправки
в сеть; на диск ничего не записано.

**Офлайн-тесты**: 17/17 (`tests/test_tg_export.py`).

**Не проверено**: полный прогон на реальном аккаунте (нужен вход пользователя). REA
`capture_process_scenario` на Windows недоступен (только Linux/WSL/macOS), поэтому живая
проверка выполнена обычным запуском.

## 7. Остаточные риски

- **Telethon** — сторонняя библиотека (MIT, ~10k★, основной Python-клиент MTProto); её код не
  аудитировался построчно. Версия и хеш закреплены.
- **Сессия** даёт полный доступ к аккаунту тому, кто расшифрует её под вашим пользователем Windows
  (вирус в вашей учётке). После работы — `tg-export logout`.
- Fallback фильтра пропускает прочие `Get*`-запросы (по конвенции API они только читают), кроме
  известных исключений с побочным эффектом (`DENIED_GETTERS`).
