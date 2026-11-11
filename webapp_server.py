"""HTTP-сервер Telegram Mini App: статика, расписание, профиль и пользовательские настройки."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import ClientSession, ClientTimeout, web

from bells import Bells
from schedule import ScheduleError
from tgauth import validate_init_data

log = logging.getLogger("webapp")
INDEX = Path(__file__).resolve().parent / "webapp" / "index.html"
MAX_SHIFT_DAYS = 90
DEFAULT_PREFS = {"lesson_reminders": True, "schedule_changes": True, "daily_summary": True}
FULL_NAME_RE = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*(?:\s+[^\W\d_]+(?:[-'][^\W\d_]+)*){1,3}$", re.UNICODE)
ACCESS_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
ACCESS_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
ACCESS_CODE_ROUNDS = 310_000


@dataclass
class Deps:
    token: str
    group: str
    tz: tzinfo
    tz_name: str
    get_lessons: Callable[..., Awaitable[list[dict]]]
    bells: Bells
    is_member: Callable[[int], Awaitable[bool]] | None = None
    dev: bool = False
    data_dir: Path = Path("data")
    auth_max_age: int = 3600
    admin_ids: set[int] = field(default_factory=lambda: {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()})
    prefs: dict[int, dict] = field(default_factory=dict)
    prefs_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rate: dict[int, list[float]] = field(default_factory=dict)
    users_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    access_attempts: dict[int, list[float]] = field(default_factory=dict)


@web.middleware
async def guard(request: web.Request, handler):
    d: Deps = request.app["deps"]
    request["user"] = None
    if request.path.startswith("/api/") and not d.dev:
        user = validate_init_data(request.headers.get("X-Init-Data", ""), d.token, d.auth_max_age)
        if user is None:
            return web.json_response({"error": "unauthorized", "message": "Откройте приложение через Telegram"}, status=401)
        uid = int(user["id"])
        # Небольшой per-user лимит защищает API от случайных циклов и спама.
        now_mono = time.monotonic()
        recent = [t for t in d.rate.get(uid, []) if now_mono - t < 60]
        if len(recent) >= 90:
            return web.json_response({"error": "rate_limited", "message": "Слишком много запросов. Подождите минуту."}, status=429)
        recent.append(now_mono)
        d.rate[uid] = recent
        request["user"] = user
        access_exempt = {"/api/me", "/api/access", "/api/admin/access-code"}
        if request.path.startswith("/api/") and request.path not in access_exempt:
            if not await _has_access(d, uid):
                return web.json_response({"error": "access_code_required", "message": "Введите код доступа."}, status=403)
        if request.path.startswith("/api/") and request.path not in {"/api/me", "/api/access", "/api/register", "/api/admin/access-code", "/api/admin/users"}:
            if await _get_registered_user(d, uid) is None:
                return web.json_response({"error": "registration_required", "message": "Сначала заполните профиль."}, status=428)
        if d.is_member is not None and not await d.is_member(uid):
            return web.json_response({"error": "forbidden", "message": "Доступ только участникам учебной группы"}, status=403)
    elif request.path.startswith("/api/") and d.dev:
        # DEV-режим не должен случайно открывать API наружу.
        peer = request.remote or ""
        if peer not in {"127.0.0.1", "::1", "localhost"}:
            return web.json_response({"error": "dev_local_only"}, status=403)
        request["user"] = {"id": 1, "first_name": "Локальный разработчик"}
    resp = await handler(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(INDEX, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _day_json(d: Deps, day: date, lessons: list[dict], error: bool, cached: bool = False) -> dict:
    times = d.bells.day_times(day)
    out = []
    for l in lessons:
        subject = str(l.get("subject") or "").strip()
        # Защитная проверка API: запись без названия предмета
        # не должна попадать ни в расписание дня, ни в неделю.
        if subject.casefold() in {"", "-", "—", "без названия", "занятие без названия",
                                  "первая", "вторая", "третья", "четвертая", "четвёртая",
                                  "пятая", "шестая", "седьмая", "восьмая", "девятая"}:
            continue
        t = times.get(l.get("number"))
        out.append({
            "number": l.get("number"), "subject": subject,
            "room": l.get("room", ""), "teacher": l.get("teacher", ""),
            "start": f"{t[0]:%H:%M}" if t else None,
            "end": f"{t[1]:%H:%M}" if t else None,
        })
    return {"date": day.isoformat(), "lessons": out, "error": error, "cached": cached}


async def api_day(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    today = datetime.now(d.tz).date()
    try:
        day = date.fromisoformat(request.query.get("date", today.isoformat()))
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректная дата")
    if abs((day - today).days) > MAX_SHIFT_DAYS:
        raise web.HTTPBadRequest(text="Дата вне допустимого диапазона")
    try:
        result = _day_json(d, day, await d.get_lessons(day), False)
    except ScheduleError as e:
        log.warning("API %s: %s", day, e)
        result = _day_json(d, day, [], True)
    except Exception:
        log.exception("Неожиданная ошибка получения расписания на %s", day)
        result = _day_json(d, day, [], True)
    return web.json_response({
        "tz": d.tz_name, "group": d.group,
        "now": datetime.now(d.tz).isoformat(timespec="seconds"),
        "day": result,
    })


async def api_week(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    today = datetime.now(d.tz).date()
    try:
        anchor = date.fromisoformat(request.query.get("date", today.isoformat()))
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректная дата")
    if abs((anchor - today).days) > MAX_SHIFT_DAYS:
        raise web.HTTPBadRequest(text="Дата вне допустимого диапазона")
    monday = anchor - timedelta(days=anchor.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]

    semaphore = asyncio.Semaphore(2)

    async def one(day: date) -> dict:
        async with semaphore:
            try:
                return _day_json(d, day, await d.get_lessons(day), False)
            except ScheduleError as e:
                log.warning("API %s: %s", day, e)
                return _day_json(d, day, [], True)
            except Exception:
                log.exception("Неожиданная ошибка получения расписания на %s", day)
                return _day_json(d, day, [], True)

    results = await asyncio.gather(*(one(day) for day in days))
    return web.json_response({
        "tz": d.tz_name, "group": d.group,
        "now": datetime.now(d.tz).isoformat(timespec="seconds"),
        "week_start": monday.isoformat(), "days": results,
    })


def _prefs_path(d: Deps) -> Path:
    return d.data_dir / "miniapp_preferences.json"


async def _load_prefs(d: Deps) -> None:
    if d.prefs:
        return
    path = _prefs_path(d)
    try:
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if str(key).isdigit() and isinstance(value, dict):
                    d.prefs[int(key)] = {k: bool(value.get(k, default)) for k, default in DEFAULT_PREFS.items()}
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        log.exception("Не удалось прочитать настройки Mini App")


async def _save_prefs(d: Deps) -> None:
    path = _prefs_path(d)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    payload = json.dumps({str(k): v for k, v in d.prefs.items()}, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    await asyncio.to_thread(tmp.write_text, payload, encoding="utf-8")
    await asyncio.to_thread(tmp.replace, path)


def require_admin(request: web.Request, d: Deps) -> bool:
    user = request.get("user") or {}
    try:
        return int(user.get("id", 0)) in d.admin_ids
    except (TypeError, ValueError):
        return False


def _user_db(d: Deps) -> Path:
    return d.data_dir / "miniapp_users.sqlite3"


def _db_connect(d: Deps) -> sqlite3.Connection:
    d.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(_user_db(d), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS registered_users (
        telegram_id INTEGER PRIMARY KEY,
        full_name TEXT NOT NULL,
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        language_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(registered_users)").fetchall()}
    if "bio" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
    if "role" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'")
    if "roles_json" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN roles_json TEXT NOT NULL DEFAULT '[\"student\"]'")
        db.execute("UPDATE registered_users SET roles_json = json_array(COALESCE(role, 'student'))")
    db.execute("CREATE TABLE IF NOT EXISTS access_settings (id INTEGER PRIMARY KEY CHECK (id = 1), salt TEXT NOT NULL, code_hash TEXT NOT NULL, version INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS access_grants (telegram_id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS access_bans (telegram_id INTEGER PRIMARY KEY, banned_at TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS journal_absences (
        lesson_date TEXT NOT NULL,
        lesson_number TEXT NOT NULL,
        student_id INTEGER NOT NULL,
        marked_by INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (lesson_date, lesson_number, student_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS journal_sessions (
        lesson_date TEXT NOT NULL,
        lesson_number TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        marked_by INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (lesson_date, lesson_number)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS journal_sync_days (
        lesson_date TEXT PRIMARY KEY,
        synced_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS journal_sync_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_sync_day TEXT NOT NULL
    )""")
    # Restore session headers for absence records saved before the history view.
    db.execute("""INSERT OR IGNORE INTO journal_sessions
        (lesson_date, lesson_number, subject, marked_by, updated_at)
        SELECT lesson_date, lesson_number, '', MIN(marked_by), MAX(updated_at)
        FROM journal_absences GROUP BY lesson_date, lesson_number""")
    db.commit()
    return db


async def _get_registered_user(d: Deps, uid: int) -> dict | None:
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                row = db.execute("SELECT * FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone()
                return dict(row) if row else None
            finally:
                db.close()
        return await asyncio.to_thread(read)


async def _access_snapshot(d: Deps, uid: int) -> dict:
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                settings = db.execute("SELECT version FROM access_settings WHERE id = 1").fetchone()
                banned = db.execute("SELECT 1 FROM access_bans WHERE telegram_id = ?", (uid,)).fetchone() is not None
                if not settings:
                    return {"configured": False, "authorized": False, "banned": banned}
                grant = db.execute("SELECT version FROM access_grants WHERE telegram_id = ?", (uid,)).fetchone()
                return {"configured": True, "authorized": bool(not banned and grant and grant["version"] == settings["version"]), "banned": banned}
            finally:
                db.close()
        return await asyncio.to_thread(read)


async def _has_access(d: Deps, uid: int) -> bool:
    return (await _access_snapshot(d, uid))["authorized"]


async def api_access(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    code = body.get("code") if isinstance(body, dict) and set(body) == {"code"} else None
    if not isinstance(code, str) or not ACCESS_CODE_RE.fullmatch(code):
        raise web.HTTPBadRequest(text="Код должен содержать 8–64 символа: латинские буквы, цифры, точку, дефис или подчёркивание")
    attempts = [t for t in d.access_attempts.get(uid, []) if time.monotonic() - t < 900]
    if len(attempts) >= 5:
        return web.json_response({"error": "rate_limited", "message": "Слишком много попыток. Подождите 15 минут."}, status=429)
    attempts.append(time.monotonic())
    d.access_attempts[uid] = attempts
    async with d.users_lock:
        def verify():
            db = _db_connect(d)
            try:
                if db.execute("SELECT 1 FROM access_bans WHERE telegram_id = ?", (uid,)).fetchone():
                    return "banned"
                row = db.execute("SELECT salt, code_hash, version FROM access_settings WHERE id = 1").fetchone()
                if not row:
                    return False
                digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), bytes.fromhex(row["salt"]), ACCESS_CODE_ROUNDS).hex()
                if not hmac.compare_digest(digest, row["code_hash"]):
                    return False
                db.execute("INSERT INTO access_grants (telegram_id, version) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET version=excluded.version", (uid, row["version"]))
                db.commit()
                return True
            finally:
                db.close()
        ok = await asyncio.to_thread(verify)
    if ok == "banned":
        return web.json_response({"error": "access_revoked", "message": "Ваш доступ закрыт администратором."}, status=403)
    if not ok:
        return web.json_response({"error": "invalid_code", "message": "Код доступа неверный."}, status=401)
    d.access_attempts.pop(uid, None)
    return web.json_response({"granted": True})


async def api_admin_access_code(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    generate = body.get("generate") if isinstance(body, dict) and set(body) == {"generate", "preserve_existing_access"} else None
    preserve = body.get("preserve_existing_access") if isinstance(body, dict) else None
    if generate is not True:
        raise web.HTTPBadRequest(text="Код доступа должен быть сгенерирован сервером")
    if type(preserve) is not bool:
        raise web.HTTPBadRequest(text="Укажите, сохранять ли доступ уже вошедшим пользователям")
    code = "-".join("".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(4)) for _ in range(5))
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, ACCESS_CODE_ROUNDS).hex()
    uid = int((request.get("user") or {}).get("id", 0))
    async with d.users_lock:
        def save():
            db = _db_connect(d)
            try:
                row = db.execute("SELECT version FROM access_settings WHERE id = 1").fetchone()
                version = (row["version"] if row else 0) + 1
                db.execute("INSERT INTO access_settings (id, salt, code_hash, version) VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET salt=excluded.salt, code_hash=excluded.code_hash, version=excluded.version", (salt.hex(), digest, version))
                if preserve:
                    db.execute("UPDATE access_grants SET version = ?", (version,))
                else:
                    db.execute("DELETE FROM access_grants")
                db.execute("INSERT INTO access_grants (telegram_id, version) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET version=excluded.version", (uid, version))
                db.commit()
            finally:
                db.close()
        await asyncio.to_thread(save)
    return web.json_response({"saved": True, "code": code, "preserved_existing_access": preserve})


async def api_admin_overview(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    async with d.prefs_lock:
        await _load_prefs(d)
        total = len(d.prefs)
        counts = {key: sum(1 for prefs in d.prefs.values() if prefs.get(key, default)) for key, default in DEFAULT_PREFS.items()}
    return web.json_response({"admin": True, "group": d.group, "timezone": d.tz_name,
        "preferences": {"users_with_saved_settings": total, "enabled": counts},
        "bells_configured": d.bells.configured,
        "bells": {day: {str(n): [a.strftime("%H:%M"), b.strftime("%H:%M")] for n, (a, b) in slots.items()} for day, slots in d.bells.table.items()},
        "schedule_source": "guspoliteh.ru",
        "notice": "Расписание загружается с сайта техникума. Фоновые персональные push-уведомления пока не реализованы."})


async def api_admin_users(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                rows = db.execute("""SELECT u.telegram_id, u.full_name, u.first_name, u.last_name,
                    u.username, u.language_code, u.created_at, u.updated_at,
                    CASE WHEN b.telegram_id IS NULL THEN 0 ELSE 1 END AS banned
                    FROM registered_users u LEFT JOIN access_bans b ON b.telegram_id = u.telegram_id
                    ORDER BY u.created_at DESC""").fetchall()
                return [dict(row) for row in rows]
            finally:
                db.close()
        users = await asyncio.to_thread(read)
    return web.json_response({"users": users, "total": len(users)})


async def api_students(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    viewer = int((request.get("user") or {}).get("id", 0))
    if not await _has_access(d, viewer):
        return web.json_response({"error": "forbidden", "message": "Нет доступа"}, status=403)
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                rows = db.execute("SELECT telegram_id, full_name, username, role, roles_json FROM registered_users ORDER BY full_name COLLATE NOCASE").fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    uid = int(item["telegram_id"])
                    try:
                        roles = json.loads(item.get("roles_json") or "[]")
                    except (TypeError, json.JSONDecodeError):
                        roles = []
                    if not isinstance(roles, list):
                        roles = []
                    roles = [r for r in roles if r in {"student", "deputy", "starosta", "moderator"}]
                    if not roles:
                        roles = ["student"]
                    if uid in d.admin_ids:
                        continue
                    item["roles"] = roles
                    item.pop("roles_json", None)
                    item["is_me"] = uid == viewer
                    result.append(item)
                priority = {"support": 0, "starosta": 1, "deputy": 2, "moderator": 3, "student": 4}
                result.sort(key=lambda item: (min((priority.get(r, 4) for r in item["roles"]), default=4), item["full_name"].casefold()))
                return result
            finally:
                db.close()
        users = await asyncio.to_thread(read)
    return web.json_response({"students": users, "total": len(users)})

async def api_admin_update_role(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только администратор может менять роли"}, status=403)
    try:
        uid = int(request.match_info["user_id"])
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректный запрос")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Некорректный запрос")
    if "roles" in body and set(body) == {"roles"} and isinstance(body["roles"], list):
        roles = body["roles"]
    elif "role" in body and set(body) == {"role"}:
        roles = [] if body["role"] == "student" else [body["role"]]
    else:
        raise web.HTTPBadRequest(text="Передайте список ролей")
    allowed = {"deputy", "starosta", "moderator"}
    if len(roles) != len(set(roles)) or any(role not in allowed for role in roles):
        raise web.HTTPBadRequest(text="Некорректный список ролей")
    if uid in d.admin_ids:
        raise web.HTTPBadRequest(text="Роль разработчика закреплена за владельцем приложения")
    normalized = roles or ["student"]
    primary_role = next((r for r in ("starosta", "deputy", "moderator") if r in normalized), "student")
    async with d.users_lock:
        def update():
            db = _db_connect(d)
            try:
                cur = db.execute(
                    "UPDATE registered_users SET role = ?, roles_json = ?, updated_at = ? WHERE telegram_id = ?",
                    (primary_role, json.dumps(normalized, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds"), uid)
                )
                db.commit()
                return cur.rowcount > 0
            finally:
                db.close()
        exists = await asyncio.to_thread(update)
    if not exists:
        raise web.HTTPNotFound(text="Студент не найден")
    return web.json_response({"saved": True, "telegram_id": uid, "roles": normalized})

async def api_admin_update_user(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    try:
        uid = int(request.match_info["user_id"])
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректный запрос")
    full_name = body.get("full_name") if isinstance(body, dict) and set(body) == {"full_name"} else None
    if not isinstance(full_name, str):
        raise web.HTTPBadRequest(text="Укажите ФИО")
    full_name = " ".join(full_name.split())
    if len(full_name) > 120 or not FULL_NAME_RE.fullmatch(full_name):
        raise web.HTTPBadRequest(text="Введите ФИО: от двух до четырёх слов, только буквы и дефисы")
    async with d.users_lock:
        def update():
            db = _db_connect(d)
            try:
                cur = db.execute("UPDATE registered_users SET full_name = ?, updated_at = ? WHERE telegram_id = ?", (full_name, datetime.now(timezone.utc).isoformat(timespec="seconds"), uid))
                db.commit()
                return cur.rowcount > 0
            finally:
                db.close()
        exists = await asyncio.to_thread(update)
    if not exists:
        raise web.HTTPNotFound(text="Профиль не найден")
    return web.json_response({"saved": True, "telegram_id": uid, "full_name": full_name})


async def api_admin_kick_user(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    try:
        uid = int(request.match_info["user_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректный Telegram ID")
    if uid in d.admin_ids:
        raise web.HTTPBadRequest(text="Нельзя закрыть доступ администратору")
    async with d.users_lock:
        def kick():
            db = _db_connect(d)
            try:
                if not db.execute("SELECT 1 FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone():
                    return False
                db.execute("INSERT INTO access_bans (telegram_id, banned_at) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET banned_at=excluded.banned_at", (uid, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                db.execute("DELETE FROM access_grants WHERE telegram_id = ?", (uid,))
                db.commit()
                return True
            finally:
                db.close()
        exists = await asyncio.to_thread(kick)
    if not exists:
        raise web.HTTPNotFound(text="Профиль не найден")
    return web.json_response({"saved": True, "banned": True})


async def api_admin_delete_user(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    try:
        uid = int(request.match_info["user_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректный Telegram ID")
    if uid in d.admin_ids:
        raise web.HTTPBadRequest(text="Нельзя удалить аккаунт администратора")
    async with d.users_lock:
        def delete_user():
            db = _db_connect(d)
            try:
                if not db.execute("SELECT 1 FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone():
                    return False
                db.execute("DELETE FROM registered_users WHERE telegram_id = ?", (uid,))
                db.execute("DELETE FROM access_grants WHERE telegram_id = ?", (uid,))
                db.execute("DELETE FROM access_bans WHERE telegram_id = ?", (uid,))
                db.commit()
                return True
            finally:
                db.close()
        deleted = await asyncio.to_thread(delete_user)
    if not deleted:
        raise web.HTTPNotFound(text="Пользователь не найден")
    async with d.prefs_lock:
        await _load_prefs(d)
        d.prefs.pop(uid, None)
        await _save_prefs(d)
    d.access_attempts.pop(uid, None)
    d.rate.pop(uid, None)
    return web.json_response({"deleted": True, "telegram_id": uid})


async def api_admin_restore_user(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    try:
        uid = int(request.match_info["user_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректный Telegram ID")
    async with d.users_lock:
        def restore():
            db = _db_connect(d)
            try:
                cur = db.execute("DELETE FROM access_bans WHERE telegram_id = ?", (uid,))
                db.commit()
                return cur.rowcount > 0
            finally:
                db.close()
        restored = await asyncio.to_thread(restore)
    return web.json_response({"saved": True, "restored": restored, "must_reenter_code": True})


async def api_me(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    user = request.get("user") or {}
    uid = int(user.get("id", 1))
    registered = await _get_registered_user(d, uid)
    access = await _access_snapshot(d, uid)
    async with d.prefs_lock:
        await _load_prefs(d)
        prefs = {**DEFAULT_PREFS, **d.prefs.get(uid, {})}
    return web.json_response({
        "user": {"id": uid, "first_name": str(user.get("first_name", "Пользователь"))[:64],
                 "last_name": str(user.get("last_name", ""))[:64],
                 "username": str(user.get("username", ""))[:64],
                 "language_code": str(user.get("language_code", "ru"))[:12]},
        "group": d.group, "timezone": d.tz_name, "preferences": prefs, "is_admin": require_admin(request, d),
        "registration": {"registered": registered is not None, "full_name": registered["full_name"] if registered else ""},
        "role": "support" if uid in d.admin_ids else (registered["role"] if registered else "student"),
        "roles": ["support"] if uid in d.admin_ids else (json.loads(registered["roles_json"]) if registered and "roles_json" in registered.keys() else [registered["role"] if registered else "student"]),
        "bio": registered["bio"] if registered and "bio" in registered.keys() else "",
        "access": access,
        "notifications_note": "Настройки сохраняются в профиле. Для фоновых push-уведомлений нужна отдельная серверная рассылка.",
    })


async def api_register(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    user = request.get("user") or {}
    uid = int(user.get("id", 1))
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    if not isinstance(body, dict) or set(body) != {"full_name", "consent"} or body.get("consent") is not True:
        raise web.HTTPBadRequest(text="Подтвердите согласие на сохранение ФИО")
    full_name = body.get("full_name")
    if not isinstance(full_name, str):
        raise web.HTTPBadRequest(text="Введите ФИО")
    full_name = " ".join(full_name.split())
    if len(full_name) > 120 or not FULL_NAME_RE.fullmatch(full_name):
        raise web.HTTPBadRequest(text="Введите ФИО: от двух до четырёх слов, только буквы и дефисы")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with d.users_lock:
        def save():
            db = _db_connect(d)
            try:
                db.execute("""INSERT INTO registered_users
                    (telegram_id, full_name, first_name, last_name, username, language_code, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name,
                    first_name=excluded.first_name, last_name=excluded.last_name,
                    username=excluded.username, language_code=excluded.language_code,
                    updated_at=excluded.updated_at""",
                    (uid, full_name, str(user.get("first_name", ""))[:64], str(user.get("last_name", ""))[:64],
                     str(user.get("username", ""))[:64], str(user.get("language_code", ""))[:12], now, now))
                db.commit()
                return dict(db.execute("SELECT * FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone())
            finally:
                db.close()
        saved = await asyncio.to_thread(save)
    return web.json_response({"saved": True, "registration": saved})


async def api_profile(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 1))
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    if not isinstance(body, dict) or set(body) != {"bio"} or not isinstance(body["bio"], str):
        raise web.HTTPBadRequest(text="Некорректное описание профиля")
    bio = body["bio"].strip()
    if len(bio) > 70:
        raise web.HTTPBadRequest(text="Описание должно быть не длиннее 70 символов")
    async with d.users_lock:
        def save():
            db = _db_connect(d)
            try:
                if not db.execute("SELECT 1 FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone():
                    return False
                db.execute("UPDATE registered_users SET bio = ?, updated_at = ? WHERE telegram_id = ?",
                           (bio, datetime.now(timezone.utc).isoformat(timespec="seconds"), uid))
                db.commit()
                return True
            finally:
                db.close()
        saved = await asyncio.to_thread(save)
    if not saved:
        return web.json_response({"error": "registration_required", "message": "Сначала заполните профиль."}, status=428)
    return web.json_response({"bio": bio, "saved": True})


def _journal_roles(d: Deps, uid: int) -> list[str]:
    if uid in d.admin_ids:
        return ["support"]
    db = _db_connect(d)
    try:
        row = db.execute("SELECT role, roles_json FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone()
        registered = dict(row) if row else None
    finally:
        db.close()
    if not registered:
        return []
    try:
        roles = json.loads(registered.get("roles_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        roles = []
    if not isinstance(roles, list):
        roles = []
    # Keep the primary role as well as additional roles. Older accounts may
    # have a stale roles_json value that otherwise hides starosta/deputy/support.
    allowed = {"student", "deputy", "starosta", "moderator", "support"}
    combined = [r for r in roles if r in allowed]
    primary = registered.get("role")
    if primary in allowed and primary not in combined:
        combined.append(primary)
    return combined or ["student"]


def _journal_can_edit(d: Deps, uid: int) -> bool:
    return uid in d.admin_ids or bool(set(_journal_roles(d, uid)) & {"starosta", "deputy", "support"})


async def api_journal_get(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    today = datetime.now(d.tz).date()
    try:
        day = date.fromisoformat(request.query.get("date", today.isoformat()))
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректная дата")
    if abs((day - today).days) > MAX_SHIFT_DAYS:
        raise web.HTTPBadRequest(text="Дата вне допустимого диапазона")
    try:
        day_json = _day_json(d, day, await d.get_lessons(day), False)
    except ScheduleError as e:
        log.warning("Журнал %s: %s", day, e)
        day_json = _day_json(d, day, [], True)
    except Exception:
        log.exception("Не удалось получить расписание для журнала на %s", day)
        day_json = _day_json(d, day, [], True)
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                students = [dict(row) for row in db.execute(
                    "SELECT telegram_id, full_name FROM registered_users ORDER BY full_name COLLATE NOCASE"
                ).fetchall() if int(row["telegram_id"]) not in d.admin_ids]
                rows = db.execute(
                    "SELECT lesson_number, student_id FROM journal_absences WHERE lesson_date = ?",
                    (day.isoformat(),)
                ).fetchall()
                absences: dict[str, list[int]] = {}
                for row in rows:
                    absences.setdefault(str(row["lesson_number"]), []).append(int(row["student_id"]))
                return students, absences
            finally:
                db.close()
        students, absences = await asyncio.to_thread(read)
    return web.json_response({
        "date": day.isoformat(), "lessons": day_json["lessons"],
        "schedule_error": day_json["error"], "students": students,
        "absences": absences, "can_edit": _journal_can_edit(d, uid),
    })


async def _sync_journal_schedule(d: Deps) -> None:
    """Синхронизирует расписание с БД не чаще раза в день; пропущенные даты остаются на повтор."""
    today = datetime.now(d.tz).date()
    academic_start = date(today.year if today.month >= 9 else today.year - 1, 9, 1)
    horizon = today + timedelta(days=30)
    async with d.users_lock:
        def pending_days():
            db = _db_connect(d)
            try:
                row = db.execute("SELECT last_sync_day FROM journal_sync_state WHERE id=1").fetchone()
                if row and row["last_sync_day"] == today.isoformat():
                    return None
                scanned = {r["lesson_date"] for r in db.execute(
                    "SELECT lesson_date FROM journal_sync_days WHERE lesson_date BETWEEN ? AND ?",
                    (academic_start.isoformat(), horizon.isoformat())
                ).fetchall()}
                refresh = {(today - timedelta(days=1)).isoformat(), today.isoformat()}
                return [academic_start + timedelta(days=i)
                        for i in range((horizon - academic_start).days + 1)
                        if (academic_start + timedelta(days=i)).isoformat() not in scanned
                        or (academic_start + timedelta(days=i)).isoformat() in refresh]
            finally:
                db.close()
        days = await asyncio.to_thread(pending_days)
        if days is None:
            return
        semaphore = asyncio.Semaphore(6)
        async def fetch(day: date):
            async with semaphore:
                try:
                    lessons = _day_json(d, day, await d.get_lessons(day), False)["lessons"]
                    return day, lessons, True
                except Exception as exc:
                    log.warning("Синхронизация журнала %s: %s", day, exc)
                    return day, [], False
        results = await asyncio.gather(*(fetch(day) for day in days))
        def persist():
            db = _db_connect(d)
            try:
                now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for day, lessons, ok in results:
                    if not ok:
                        continue
                    for lesson in lessons:
                        db.execute(
                            "INSERT INTO journal_sessions (lesson_date, lesson_number, subject, marked_by, updated_at) VALUES (?, ?, ?, 0, ?) "
                            "ON CONFLICT(lesson_date, lesson_number) DO UPDATE SET subject=excluded.subject",
                            (day.isoformat(), str(lesson["number"]), str(lesson["subject"]), now_text)
                        )
                    db.execute("INSERT OR IGNORE INTO journal_sync_days (lesson_date, synced_at) VALUES (?, ?)",
                               (day.isoformat(), now_text))
                db.execute("INSERT INTO journal_sync_state (id, last_sync_day) VALUES (1, ?) "
                           "ON CONFLICT(id) DO UPDATE SET last_sync_day=excluded.last_sync_day",
                           (today.isoformat(),))
                db.commit()
            finally:
                db.close()
        await asyncio.to_thread(persist)
        log.info("Журнал синхронизирован: %d дат обработано, %d успешно", len(results), sum(1 for x in results if x[2]))


async def api_journal_history(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    await _sync_journal_schedule(d)
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                students = [dict(row) for row in db.execute(
                    "SELECT telegram_id, full_name FROM registered_users ORDER BY full_name COLLATE NOCASE"
                ).fetchall() if int(row["telegram_id"]) not in d.admin_ids]
                sessions = [dict(row) for row in db.execute(
                    "SELECT lesson_date, lesson_number, subject FROM journal_sessions WHERE subject<>'' ORDER BY subject COLLATE NOCASE, lesson_date, CAST(lesson_number AS INTEGER)"
                ).fetchall()]
                for session in sessions:
                    compact_subject = "".join(ch for ch in str(session["subject"]).casefold() if ch.isalnum())
                    if compact_subject.startswith(("технологияфизическогоуровня", "технологиифизическогоуровня")):
                        session["subject"] = "Технологии физического уровня"
                absences = [dict(row) for row in db.execute(
                    "SELECT lesson_date, lesson_number, student_id FROM journal_absences"
                ).fetchall()]
                return students, sessions, absences
            finally:
                db.close()
        students, sessions, absences = await asyncio.to_thread(read)
    return web.json_response({"students": students, "sessions": sessions, "absences": absences,
        "can_edit": _journal_can_edit(d, uid)})


async def api_journal_send_today_report(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    # Отчёт отправляется только тому пользователю, который вызвал этот endpoint.
    uid = int((request.get("user") or {}).get("id", 0))
    if not _journal_can_edit(d, uid):
        return web.json_response({"error": "forbidden", "message": "Запрашивать отчёт могут только староста и зам. старосты."}, status=403)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    raw_day = body.get("date") if isinstance(body, dict) else None
    try:
        report_day = date.fromisoformat(raw_day) if raw_day else datetime.now(d.tz).date()
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_date", "message": "Некорректная дата отчёта."}, status=400)

    async with d.users_lock:
        def read_report():
            db = _db_connect(d)
            try:
                students = db.execute("""
                    SELECT telegram_id, full_name
                    FROM registered_users
                    WHERE full_name IS NOT NULL AND TRIM(full_name) <> ''
                    ORDER BY full_name COLLATE NOCASE
                """).fetchall()
                absent = db.execute("""
                    SELECT DISTINCT student_id
                    FROM journal_absences
                    WHERE lesson_date = ?
                """, (report_day.isoformat(),)).fetchall()
                return students, {int(row["student_id"]) for row in absent}
            finally:
                db.close()
        students, absent_ids = await asyncio.to_thread(read_report)

    absent_names = [
        str(row["full_name"]).strip()
        for row in students
        if int(row["telegram_id"]) not in d.admin_ids
        and int(row["telegram_id"]) in absent_ids
    ]

    lines = [
        f"📋 Отсутствующие за {report_day:%d.%m.%Y}",
        f"Группа: {d.group}:"
    ]
    if absent_names:
        lines.extend(f"• {name}" for name in absent_names)
    else:
        lines.append("Отсутствующих нет.")
    message = "\\n".join(lines).strip()

    try:
        async with ClientSession(timeout=ClientTimeout(total=12)) as session:
            async with session.post(
                f"https://api.telegram.org/bot{d.token}/sendMessage",
                json={
                    "chat_id": uid,
                    "text": message,
                    "reply_markup": {
                        "inline_keyboard": [[{
                            "text": "📋 Скопировать",
                            "copy_text": {"text": message[:256]}
                        }]]
                    } if len(message) <= 256 else None
                }
            ) as response:
                result = await response.json(content_type=None)
        if not result.get("ok"):
            description = str(result.get("description") or "")
            if "chat not found" in description.lower() or "bot can't initiate" in description.lower():
                return web.json_response({
                    "error": "bot_not_started",
                    "message": "Сначала откройте @vasya_sa_bot и нажмите /start. После этого повторите отправку отчёта."
                }, status=400)
            log.warning("Telegram не отправил отчёт посещаемости пользователю %s: %s", uid, description)
            return web.json_response({"error": "telegram_send_failed", "message": "Telegram не смог отправить сообщение. Попробуйте позже."}, status=502)
    except Exception:
        log.exception("Не удалось связаться с Telegram при отправке отчёта пользователю %s", uid)
        return web.json_response({"error": "telegram_unavailable", "message": "Не удалось связаться с Telegram. Попробуйте позже."}, status=502)

    # Отчёт автоматически удаляется через 30 секунд после отправки.
    sent_message_id = (result.get("result") or {}).get("message_id")
    if sent_message_id:
        async def delete_report_later() -> None:
            await asyncio.sleep(30)
            try:
                async with ClientSession(timeout=ClientTimeout(total=12)) as delete_session:
                    async with delete_session.post(
                        f"https://api.telegram.org/bot{d.token}/deleteMessage",
                        json={"chat_id": uid, "message_id": int(sent_message_id)}
                    ) as delete_response:
                        delete_result = await delete_response.json(content_type=None)
                if not delete_result.get("ok"):
                    log.warning("Не удалось удалить отчёт посещаемости %s для %s: %s",
                                sent_message_id, uid, delete_result.get("description"))
            except Exception:
                log.exception("Ошибка автоматического удаления отчёта %s для %s", sent_message_id, uid)

        asyncio.create_task(delete_report_later())

    return web.json_response({"sent": True, "date": report_day.isoformat(), "sessions_count": 0})


async def api_journal_save(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    if not _journal_can_edit(d, uid):
        return web.json_response({"error": "forbidden", "message": "Отмечать посещаемость могут только староста и зам. старосты."}, status=403)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    if not isinstance(body, dict) or set(body) != {"date", "lesson_number", "absent_ids"}:
        raise web.HTTPBadRequest(text="Передайте дату, номер пары и список отсутствующих")
    try:
        day = date.fromisoformat(body["date"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Некорректная дата")
    lesson_number = str(body["lesson_number"]).strip()
    if not lesson_number or len(lesson_number) > 12:
        raise web.HTTPBadRequest(text="Некорректный номер пары")
    absent_ids = body["absent_ids"]
    if not isinstance(absent_ids, list) or len(absent_ids) > 200:
        raise web.HTTPBadRequest(text="Некорректный список отсутствующих")
    if any(type(value) is not int or value <= 0 for value in absent_ids) or len(absent_ids) != len(set(absent_ids)):
        raise web.HTTPBadRequest(text="Некорректный список студентов")
    async with d.users_lock:
        def save():
            db = _db_connect(d)
            try:
                session_exists = db.execute(
                    "SELECT 1 FROM journal_sessions WHERE lesson_date=? AND lesson_number=? AND subject<>''",
                    (day.isoformat(), lesson_number)
                ).fetchone()
                if not session_exists:
                    raise ValueError("Занятие не найдено в сохранённой истории расписания")
                if absent_ids:
                    placeholders = ",".join("?" for _ in absent_ids)
                    known = {int(row["telegram_id"]) for row in db.execute(
                        f"SELECT telegram_id FROM registered_users WHERE telegram_id IN ({placeholders})",
                        tuple(absent_ids)
                    ).fetchall() if int(row["telegram_id"]) not in d.admin_ids}
                    if known != set(absent_ids):
                        raise ValueError("В списке есть неизвестные студенты")
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                db.execute(
                    "UPDATE journal_sessions SET marked_by=?, updated_at=? WHERE lesson_date=? AND lesson_number=?",
                    (uid, now, day.isoformat(), lesson_number)
                )
                db.execute(
                    "DELETE FROM journal_absences WHERE lesson_date = ? AND lesson_number = ?",
                    (day.isoformat(), lesson_number)
                )
                db.executemany(
                    "INSERT INTO journal_absences (lesson_date, lesson_number, student_id, marked_by, updated_at) VALUES (?, ?, ?, ?, ?)",
                    [(day.isoformat(), lesson_number, student_id, uid, now) for student_id in absent_ids]
                )
                db.commit()
            finally:
                db.close()
        try:
            await asyncio.to_thread(save)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"saved": True, "date": day.isoformat(), "lesson_number": lesson_number, "absent_count": len(absent_ids)})


async def api_journal_bulk_save(request: web.Request) -> web.Response:
    """Сохраняет посещаемость всех пар выбранной даты одной транзакцией."""
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    if not _journal_can_edit(d, uid):
        return web.json_response({"error": "forbidden", "message": "Отмечать посещаемость могут только староста и зам. старосты."}, status=403)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    if not isinstance(body, dict) or set(body) != {"date", "sessions"} or not isinstance(body["sessions"], list):
        raise web.HTTPBadRequest(text="Передайте дату и список пар с посещаемостью")
    try:
        day = date.fromisoformat(body["date"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Некорректная дата")
    sessions = body["sessions"]
    if not sessions or len(sessions) > 20:
        raise web.HTTPBadRequest(text="Нет пар для сохранения или слишком много пар")
    normalized = []
    all_absent_ids = set()
    seen_numbers = set()
    for item in sessions:
        if not isinstance(item, dict) or set(item) != {"lesson_number", "absent_ids"}:
            raise web.HTTPBadRequest(text="Некорректные данные пары")
        lesson_number = str(item["lesson_number"]).strip()
        absent_ids = item["absent_ids"]
        if not lesson_number or len(lesson_number) > 12 or lesson_number in seen_numbers:
            raise web.HTTPBadRequest(text="Некорректный или повторяющийся номер пары")
        if not isinstance(absent_ids, list) or len(absent_ids) > 200:
            raise web.HTTPBadRequest(text="Некорректный список отсутствующих")
        if any(type(value) is not int or value <= 0 for value in absent_ids) or len(absent_ids) != len(set(absent_ids)):
            raise web.HTTPBadRequest(text="Некорректный список студентов")
        seen_numbers.add(lesson_number)
        all_absent_ids.update(absent_ids)
        normalized.append((lesson_number, absent_ids))
    async with d.users_lock:
        def save_all():
            db = _db_connect(d)
            try:
                placeholders = ",".join("?" for _ in seen_numbers)
                found = {str(row["lesson_number"]) for row in db.execute(
                    f"SELECT lesson_number FROM journal_sessions WHERE lesson_date=? AND subject<>'' AND lesson_number IN ({placeholders})",
                    (day.isoformat(), *seen_numbers)
                ).fetchall()}
                if found != seen_numbers:
                    raise ValueError("Одна или несколько пар не найдены в истории расписания")
                if all_absent_ids:
                    ids_placeholders = ",".join("?" for _ in all_absent_ids)
                    known = {int(row["telegram_id"]) for row in db.execute(
                        f"SELECT telegram_id FROM registered_users WHERE telegram_id IN ({ids_placeholders})",
                        tuple(all_absent_ids)
                    ).fetchall() if int(row["telegram_id"]) not in d.admin_ids}
                    if known != all_absent_ids:
                        raise ValueError("В списке есть неизвестные студенты")
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for lesson_number, absent_ids in normalized:
                    db.execute(
                        "UPDATE journal_sessions SET marked_by=?, updated_at=? WHERE lesson_date=? AND lesson_number=?",
                        (uid, now, day.isoformat(), lesson_number)
                    )
                    db.execute(
                        "DELETE FROM journal_absences WHERE lesson_date=? AND lesson_number=?",
                        (day.isoformat(), lesson_number)
                    )
                    db.executemany(
                        "INSERT INTO journal_absences (lesson_date, lesson_number, student_id, marked_by, updated_at) VALUES (?, ?, ?, ?, ?)",
                        [(day.isoformat(), lesson_number, student_id, uid, now) for student_id in absent_ids]
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        try:
            await asyncio.to_thread(save_all)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"saved": True, "date": day.isoformat(), "sessions_saved": len(normalized)})


async def api_preferences(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    user = request.get("user") or {}
    uid = int(user.get("id", 1))
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    if not isinstance(body, dict) or set(body) - set(DEFAULT_PREFS):
        raise web.HTTPBadRequest(text="Неизвестные настройки")
    if any(type(value) is not bool for value in body.values()):
        raise web.HTTPBadRequest(text="Значения настроек должны быть true или false")
    async with d.prefs_lock:
        await _load_prefs(d)
        prefs = {**DEFAULT_PREFS, **d.prefs.get(uid, {}), **body}
        d.prefs[uid] = prefs
        await _save_prefs(d)
    return web.json_response({"preferences": prefs, "saved": True})


def create_app(deps: Deps) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=16 * 1024)
    app["deps"] = deps
    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/day", api_day)
    app.router.add_get("/api/week", api_week)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/students", api_students)
    app.router.add_get("/api/journal", api_journal_get)
    app.router.add_get("/api/journal/history", api_journal_history)
    app.router.add_post("/api/journal/send-today-report", api_journal_send_today_report)
    app.router.add_post("/api/journal", api_journal_save)
    app.router.add_post("/api/journal/bulk", api_journal_bulk_save)
    app.router.add_post("/api/access", api_access)
    app.router.add_post("/api/admin/access-code", api_admin_access_code)
    app.router.add_get("/api/admin/overview", api_admin_overview)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_put("/api/admin/users/{user_id:\\d+}", api_admin_update_user)
    app.router.add_put("/api/admin/users/{user_id:\\d+}/role", api_admin_update_role)
    app.router.add_post("/api/admin/users/{user_id:\\d+}/kick", api_admin_kick_user)
    app.router.add_delete("/api/admin/users/{user_id:\\d+}", api_admin_delete_user)
    app.router.add_post("/api/admin/users/{user_id:\\d+}/restore", api_admin_restore_user)
    app.router.add_post("/api/register", api_register)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/preferences", api_preferences)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Mini App слушает %s:%d", host, port)
    return runner


