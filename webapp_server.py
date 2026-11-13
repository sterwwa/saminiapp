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
DEFAULT_PREFS = {"lesson_reminders": True, "schedule_changes": True, "daily_summary": True, "telegram_dm": True}
FULL_NAME_RE = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*(?:\s+[^\W\d_]+(?:[-'][^\W\d_]+)*){1,3}$", re.UNICODE)
CLASS_ROSTER = [
    "Алексеев Сергей Вячеславович",
    "Андрощук Кирилл Иванович",
    "Артемов Егор Александрович",
    "Биндяк Андрей Максимович",
    "Браташов Станислав Абдуганиевич",
    "Бровка Александр Игоревич",
    "Гуськов Сергей Александрович",
    "Жежера Валерия Евгеньевна",
    "Жилякова Валерия Сергеевна",
    "Загонкина Валентина Андреевна",
    "Зозуля Илья Александрович",
    "Колесникова Ангелина Олеговна",
    "Кононов Вячеслав Дмитриевич",
    "Кузнецов Павел Антонович",
    "Мамонов Тимур Витальевич",
    "Петраш Иван Игоревич",
    "Рогачев Даниил Александрович",
    "Романенко Кирилл Андреевич",
    "Руденок Александр Александрович",
    "Стригунов Артём Максимович",
    "Сударикова Дарья Тимофеевна",
    "Сысоев Дмитрий Викторович",
    "Хачатрян Тигран Артемович",
    "Чибисов Матвей Сергеевич",
    "Шугаева Дарина Николаевна"
]

def _person_key(full_name: str) -> tuple[str, str] | None:
    parts = [part.casefold().replace("ё", "е") for part in str(full_name or "").split()]
    return tuple(sorted(parts[:2])) if len(parts) >= 2 else None


def _sync_class_roster(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS class_roster (
        roster_id INTEGER PRIMARY KEY,
        full_name TEXT NOT NULL,
        matched_telegram_id INTEGER
    )""")
    db.executemany(
        "INSERT INTO class_roster(roster_id, full_name) VALUES(?, ?) "
        "ON CONFLICT(roster_id) DO UPDATE SET full_name=excluded.full_name",
        [(i, name) for i, name in enumerate(CLASS_ROSTER, 1)],
    )
    registered = db.execute("SELECT telegram_id, full_name FROM registered_users").fetchall()
    by_name: dict[tuple[str, str], list[int]] = {}
    for user in registered:
        key = _person_key(user["full_name"])
        if key:
            by_name.setdefault(key, []).append(int(user["telegram_id"]))
    for student in db.execute("SELECT roster_id, full_name, matched_telegram_id FROM class_roster").fetchall():
        matches = by_name.get(_person_key(student["full_name"]), [])
        matched = matches[0] if len(matches) == 1 else None
        previous = student["matched_telegram_id"]
        if previous == matched:
            continue
        old_id = int(previous) if previous is not None else -int(student["roster_id"])
        new_id = int(matched) if matched is not None else -int(student["roster_id"])
        if old_id != new_id:
            db.execute("""INSERT OR IGNORE INTO journal_absences
                (lesson_date, lesson_number, student_id, marked_by, updated_at)
                SELECT lesson_date, lesson_number, ?, marked_by, updated_at
                FROM journal_absences WHERE student_id=?""", (new_id, old_id))
            db.execute("DELETE FROM journal_absences WHERE student_id=?", (old_id,))
            db.execute("""INSERT OR IGNORE INTO class_duties
                (duty_date, student_id, assigned_by, note, completed_at, created_at)
                SELECT duty_date, ?, assigned_by, note, completed_at, created_at
                FROM class_duties WHERE student_id=?""", (new_id, old_id))
            db.execute("DELETE FROM class_duties WHERE student_id=?", (old_id,))
        db.execute("UPDATE class_roster SET matched_telegram_id=? WHERE roster_id=?",
                   (matched, int(student["roster_id"])))


def _class_students(db: sqlite3.Connection, d: Deps) -> list[dict]:
    roster_rows = db.execute("""SELECT r.roster_id, r.full_name, r.matched_telegram_id, u.*
        FROM class_roster r LEFT JOIN registered_users u
        ON u.telegram_id=r.matched_telegram_id ORDER BY r.roster_id""").fetchall()
    result: list[dict] = []
    matched_ids: set[int] = set()
    for row in roster_rows:
        item = dict(row)
        uid = item.get("matched_telegram_id")
        if uid is not None and int(uid) not in d.admin_ids:
            uid = int(uid)
            matched_ids.add(uid)
            try:
                roles = json.loads(item.get("roles_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                roles = []
            if not isinstance(roles, list):
                roles = []
            item.update({"telegram_id": uid, "student_id": uid, "registered": True,
                         "roles": [r for r in roles if r in {"student", "deputy", "starosta", "moderator"}] or ["student"],
                         "full_name": row["full_name"], "roster_id": int(row["roster_id"])})
            item.pop("roles_json", None)
        else:
            synthetic_id = -int(row["roster_id"])
            item = {"telegram_id": synthetic_id, "student_id": synthetic_id,
                    "roster_id": int(row["roster_id"]), "full_name": row["full_name"],
                    "registered": False, "username": "", "role": "student",
                    "roles": ["student"], "last_seen_at": "", "avatar_data": ""}
        result.append(item)
    for row in db.execute("SELECT * FROM registered_users ORDER BY full_name COLLATE NOCASE").fetchall():
        uid = int(row["telegram_id"])
        if uid in d.admin_ids or uid in matched_ids:
            continue
        item = dict(row)
        try:
            roles = json.loads(item.get("roles_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            roles = []
        item["roles"] = [r for r in roles if r in {"student", "deputy", "starosta", "moderator"}] or ["student"]
        item.update({"student_id": uid, "registered": True, "roster_id": None})
        item.pop("roles_json", None)
        result.append(item)
    return result

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
    chat_id: int | str | None = None
    emoji_cache: dict[int, tuple[float, dict | None]] = field(default_factory=dict)
    emoji_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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


def _telegram_login_user(payload: dict, token: str, max_age: int = 86400) -> dict | None:
    """Проверяет подпись Telegram Login Widget и возвращает только подтверждённые поля пользователя."""
    if not isinstance(payload, dict):
        return None
    supplied_hash = payload.get("hash")
    auth_date = payload.get("auth_date")
    if not isinstance(supplied_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
        return None
    try:
        issued = int(auth_date)
    except (TypeError, ValueError):
        return None
    now = int(time.time())
    if issued > now + 30 or now - issued > max_age:
        return None
    fields = {str(k): str(v) for k, v in payload.items() if k != "hash" and v is not None}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        return None
    try:
        uid = int(fields["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    user = {"id": uid}
    for key in ("first_name", "last_name", "username", "photo_url", "language_code"):
        value = fields.get(key)
        if value:
            user[key] = value[:2048] if key == "photo_url" else value[:256]
    return user


def _make_browser_session(user: dict, token: str) -> str:
    payload = {"user": user, "exp": int(time.time()) + 7 * 24 * 60 * 60}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = __import__("base64").urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    key = hmac.new(b"sa225-browser-session", token.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def _read_browser_session(value: str, token: str) -> dict | None:
    try:
        encoded, signature = value.split(".", 1)
        key = hmac.new(b"sa225-browser-session", token.encode("utf-8"), hashlib.sha256).digest()
        expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = __import__("base64").urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        user = payload.get("user")
        if int(payload.get("exp", 0)) < int(time.time()) or not isinstance(user, dict):
            return None
        if int(user.get("id", 0)) <= 0:
            return None
        return user
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return None


async def auth_telegram(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"message": "Некорректный ответ Telegram"}, status=400)
    user = _telegram_login_user(payload, d.token)
    if user is None:
        return web.json_response({"message": "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз."}, status=401)
    response = web.json_response({"authenticated": True})
    response.set_cookie(
        "sa225_session", _make_browser_session(user, d.token),
        max_age=7 * 24 * 60 * 60, httponly=True, secure=True,
        samesite="Lax", path="/",
    )
    return response


async def auth_logout(request: web.Request) -> web.Response:
    response = web.json_response({"logged_out": True})
    response.del_cookie("sa225_session", path="/", secure=True, httponly=True, samesite="Lax")
    return response


@web.middleware
async def guard(request: web.Request, handler):
    d: Deps = request.app["deps"]
    request["user"] = None
    if request.path.startswith("/api/") and not d.dev:
        user = validate_init_data(request.headers.get("X-Init-Data", ""), d.token, d.auth_max_age)
        if user is None:
            user = _read_browser_session(request.cookies.get("sa225_session", ""), d.token)
        if user is None:
            return web.json_response({"error": "unauthorized", "message": "Войдите через Telegram"}, status=401)
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


async def app_manifest(request: web.Request) -> web.Response:
    path = INDEX.parent / "manifest.webmanifest"
    return web.Response(
        text=path.read_text(encoding="utf-8"),
        content_type="application/manifest+json",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def app_service_worker(request: web.Request) -> web.Response:
    path = INDEX.parent / "sw.js"
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/javascript; charset=utf-8",
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


async def app_icon(request: web.Request) -> web.Response:
    path = INDEX.parent / "icon.svg"
    return web.Response(
        body=path.read_bytes(),
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def apple_touch_icon(request: web.Request) -> web.Response:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(r'<div class="install-app-icon"[^>]*><img src="data:image/png;base64,([^"]+)"', html)
    if not match:
        raise web.HTTPNotFound(text="App icon not found")
    icon = __import__("base64").b64decode(match.group(1))
    return web.Response(
        body=icon,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(INDEX, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _has_fourth_pair(lessons: list[dict]) -> bool:
    """Четвёртая пара существует, если в её строке указан предмет или кабинет."""
    for lesson in lessons:
        number = str(lesson.get("number") or "").strip().casefold()
        subject = str(lesson.get("subject") or "").strip()
        room = str(lesson.get("room") or "").strip()
        if number in {"4", "четвертая", "четвёртая", "четвертую", "четвёртую"}:
            if subject not in {"", "-", "—"} or room not in {"", "-", "—"}:
                return True
    return False


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
        raw_lessons = await d.get_lessons(day)
        result = _day_json(d, day, raw_lessons, False)
        result["fourth_pair_present"] = _has_fourth_pair(raw_lessons)
    except ScheduleError as e:
        log.warning("API %s: %s", day, e)
        result = _day_json(d, day, [], True)
        result["fourth_pair_present"] = None
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


def require_invite_manager(request: web.Request, d: Deps) -> bool:
    user = request.get("user") or {}
    try:
        uid = int(user.get("id", 0))
    except (TypeError, ValueError):
        return False
    if uid in d.admin_ids:
        return True
    db = _db_connect(d)
    try:
        row = db.execute("SELECT roles_json, role FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone()
        if not row:
            return False
        roles = json.loads(row["roles_json"] or "[]") if "roles_json" in row.keys() else []
        if not isinstance(roles, list):
            roles = []
        if row["role"]:
            roles.append(row["role"])
        return "starosta" in roles
    except (sqlite3.Error, ValueError, TypeError):
        return False
    finally:
        db.close()


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
    if "birth_date" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN birth_date TEXT NOT NULL DEFAULT ''")
    if "avatar_data" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN avatar_data TEXT NOT NULL DEFAULT ''")
    if "role" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'")
    if "roles_json" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN roles_json TEXT NOT NULL DEFAULT '[\"student\"]'")
        db.execute("UPDATE registered_users SET roles_json = json_array(COALESCE(role, 'student'))")
    if "last_seen_at" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''")
    if "last_seen_source" not in columns:
        db.execute("ALTER TABLE registered_users ADD COLUMN last_seen_source TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE TABLE IF NOT EXISTS access_settings (id INTEGER PRIMARY KEY CHECK (id = 1), salt TEXT NOT NULL, code_hash TEXT NOT NULL, version INTEGER NOT NULL)")
    access_columns = {row["name"] for row in db.execute("PRAGMA table_info(access_settings)").fetchall()}
    if "code_plain" not in access_columns:
        db.execute("ALTER TABLE access_settings ADD COLUMN code_plain TEXT NOT NULL DEFAULT ''")
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
    db.execute("""CREATE TABLE IF NOT EXISTS class_duties (
        duty_date TEXT NOT NULL,
        student_id INTEGER NOT NULL,
        assigned_by INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        completed_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (duty_date, student_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS duty_schedule_meta (
        meta_key TEXT PRIMARY KEY,
        meta_value TEXT NOT NULL DEFAULT ''
    )""")
    _sync_class_roster(db)
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


async def api_admin_current_access_code(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_invite_manager(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                row = db.execute("SELECT code_plain, version FROM access_settings WHERE id = 1").fetchone()
                return {"configured": bool(row), "code": (row["code_plain"] or "") if row else ""}
            finally:
                db.close()
        result = await asyncio.to_thread(read)
    return web.json_response(result)


async def api_admin_access_code(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    if not require_invite_manager(request, d):
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
                db.execute("INSERT INTO access_settings (id, salt, code_hash, version, code_plain) VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET salt=excluded.salt, code_hash=excluded.code_hash, version=excluded.version, code_plain=excluded.code_plain", (salt.hex(), digest, version, code))
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
        "notice": "Расписание загружается с сайта техникума. Личные уведомления Mini App отправляются в Telegram по настройкам каждого пользователя."})


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
                return _class_students(db, d)
            finally:
                db.close()
        users = await asyncio.to_thread(read)

    now_utc = datetime.now(timezone.utc)
    for item in users:
        raw = item.get("last_seen_at") or ""
        try:
            seen = datetime.fromisoformat(raw)
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            item["online"] = bool(item.get("registered")) and 0 <= (now_utc - seen.astimezone(timezone.utc)).total_seconds() <= 30
        except (TypeError, ValueError):
            item["online"] = False
        item["last_seen_at"] = raw or None
        item["is_me"] = int(item["telegram_id"]) == viewer
        item.pop("last_seen_source", None)
    priority = {"support": 0, "starosta": 1, "deputy": 2, "moderator": 3, "student": 4}
    users.sort(key=lambda item: (min((priority.get(r, 4) for r in item["roles"]), default=4),
                                 item["full_name"].casefold()))
    registered_total = sum(1 for item in users if item.get("registered"))
    return web.json_response({"students": users, "total": len(users),
                              "registered_total": registered_total,
                              "not_registered_total": len(users) - registered_total})

async def api_student_presence(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    viewer = int((request.get("user") or {}).get("id", 0))
    if not await _has_access(d, viewer):
        return web.json_response({"error": "forbidden", "message": "Нет доступа"}, status=403)
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                return [dict(row) for row in db.execute(
                    "SELECT telegram_id, last_seen_at FROM registered_users"
                ).fetchall()]
            finally:
                db.close()
        users = await asyncio.to_thread(read)

    now_utc = datetime.now(timezone.utc)
    students = []
    for item in users:
        uid = int(item["telegram_id"])
        if uid in d.admin_ids:
            continue
        raw = item.get("last_seen_at") or ""
        try:
            seen = datetime.fromisoformat(raw)
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            online = 0 <= (now_utc - seen.astimezone(timezone.utc)).total_seconds() <= 30
        except (TypeError, ValueError):
            online = False
        students.append({
            "telegram_id": uid,
            "online": online,
            "last_seen_at": raw or None,
        })
    return web.json_response({"students": students})


async def api_presence(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    if not uid:
        return web.json_response({"error": "unauthorized"}, status=401)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with d.users_lock:
        def touch():
            db = _db_connect(d)
            try:
                cur = db.execute(
                    "UPDATE registered_users SET last_seen_at = ?, last_seen_source = 'miniapp' WHERE telegram_id = ?",
                    (now, uid),
                )
                db.commit()
                return cur.rowcount > 0
            finally:
                db.close()
        registered = await asyncio.to_thread(touch)
    if not registered:
        return web.json_response({"error": "not_registered"}, status=403)
    return web.json_response({"updated": True, "at": now})


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
                 "language_code": str(user.get("language_code", "ru"))[:12],
                 "photo_url": str(user.get("photo_url", ""))[:2048] if str(user.get("photo_url", "")).startswith("https://") else "",
                 },        "group": d.group, "timezone": d.tz_name, "preferences": prefs, "is_admin": require_admin(request, d),
        "registration": {"registered": registered is not None, "full_name": registered["full_name"] if registered else ""},
        "role": "support" if uid in d.admin_ids else (registered["role"] if registered else "student"),
        "roles": ["support"] if uid in d.admin_ids else (json.loads(registered["roles_json"]) if registered and "roles_json" in registered.keys() else [registered["role"] if registered else "student"]),
        "bio": registered["bio"] if registered and "bio" in registered.keys() else "",
        "birth_date": registered["birth_date"] if registered and "birth_date" in registered.keys() else "",
        "avatar_data": registered["avatar_data"] if registered and "avatar_data" in registered.keys() else "",
        "access": access,
        "notifications_note": "Уведомления приходят в личные сообщения Telegram. Включите чат с ботом, чтобы получать расписание на завтра в 20:00 и сообщения об изменениях.",
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
    allowed = {"bio", "full_name", "birth_date", "avatar_data"}
    if not isinstance(body, dict) or not body or set(body) - allowed:
        raise web.HTTPBadRequest(text="Некорректные данные профиля")
    updates: list[str] = []
    values: list[object] = []
    if "bio" in body:
        bio = body["bio"]
        if not isinstance(bio, str):
            raise web.HTTPBadRequest(text="Некорректное описание профиля")
        bio = bio.strip()
        if len(bio) > 70:
            raise web.HTTPBadRequest(text="Описание должно быть не длиннее 70 символов")
        updates.append("bio = ?")
        values.append(bio)
    if "full_name" in body:
        full_name = body["full_name"]
        if not isinstance(full_name, str):
            raise web.HTTPBadRequest(text="Укажите ФИО")
        full_name = " ".join(full_name.split())
        if len(full_name) > 120 or not FULL_NAME_RE.fullmatch(full_name):
            raise web.HTTPBadRequest(text="Введите ФИО: от двух до четырёх слов, только буквы и дефисы")
        updates.append("full_name = ?")
        values.append(full_name)
    if "birth_date" in body:
        birth_date = body["birth_date"]
        if not isinstance(birth_date, str) or (birth_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth_date)):
            raise web.HTTPBadRequest(text="Укажите корректную дату рождения")
        if birth_date:
            try:
                parsed_birth = datetime.strptime(birth_date, "%Y-%m-%d").date()
                if parsed_birth > datetime.now(timezone.utc).date():
                    raise ValueError
            except ValueError:
                raise web.HTTPBadRequest(text="Дата рождения не может быть в будущем")
        updates.append("birth_date = ?")
        values.append(birth_date)
    if "avatar_data" in body:
        avatar_data = body["avatar_data"]
        if not isinstance(avatar_data, str) or len(avatar_data) > 400000 or (avatar_data and not re.fullmatch(r"data:image/jpeg;base64,[A-Za-z0-9+/]+={0,2}", avatar_data)):
            raise web.HTTPBadRequest(text="Фото должно быть JPG и не больше 350 КБ")
        updates.append("avatar_data = ?")
        values.append(avatar_data)
    if not updates:
        raise web.HTTPBadRequest(text="Нет данных для сохранения")
    updates.append("updated_at = ?")
    values.extend([datetime.now(timezone.utc).isoformat(timespec="seconds"), uid])
    async with d.users_lock:
        def save():
            db = _db_connect(d)
            try:
                cur = db.execute(f"UPDATE registered_users SET {', '.join(updates)} WHERE telegram_id = ?", values)
                if not cur.rowcount:
                    return None
                db.commit()
                row = db.execute("SELECT full_name, bio, birth_date, avatar_data FROM registered_users WHERE telegram_id = ?", (uid,)).fetchone()
                return dict(row)
            finally:
                db.close()
        saved = await asyncio.to_thread(save)
    if not saved:
        return web.json_response({"error": "registration_required", "message": "Сначала заполните профиль."}, status=428)
    return web.json_response({**saved, "saved": True})


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
                students = [{"telegram_id": item["telegram_id"], "full_name": item["full_name"], "registered": item["registered"]} for item in _class_students(db, d)]
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
                students = [{"telegram_id": item["telegram_id"], "full_name": item["full_name"], "registered": item["registered"]} for item in _class_students(db, d)]
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
                students = [
                    {"telegram_id": item["telegram_id"], "full_name": item["full_name"]}
                    for item in _class_students(db, d)
                    if str(item.get("full_name") or "").strip()
                ]
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
    message = "\n".join(lines).strip()

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
    if any(type(value) is not int or value == 0 for value in absent_ids) or len(absent_ids) != len(set(absent_ids)):
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
                    known = {int(item["telegram_id"]) for item in _class_students(db, d)
                             if int(item["telegram_id"]) in set(absent_ids)}
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
        if any(type(value) is not int or value == 0 for value in absent_ids) or len(absent_ids) != len(set(absent_ids)):
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
                    known = {int(item["telegram_id"]) for item in _class_students(db, d)
                             if int(item["telegram_id"]) in all_absent_ids}
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



async def api_push_config(request: web.Request) -> web.Response:
    public_key = os.getenv("WEBPUSH_VAPID_PUBLIC_KEY", "").strip()
    if not public_key:
        return web.json_response({"enabled": False, "public_key": "", "message": "Push-уведомления ещё не настроены на сервере"}, status=503)
    return web.json_response({"enabled": True, "public_key": public_key})


async def api_push_subscribe(request: web.Request) -> web.Response:
    from push_service import save_subscription
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    subscription = body.get("subscription") if isinstance(body, dict) else None
    if not isinstance(subscription, dict):
        raise web.HTTPBadRequest(text="Не найдена push-подписка")
    endpoint, keys = subscription.get("endpoint"), subscription.get("keys")
    if (not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 4096
            or not isinstance(keys, dict) or not isinstance(keys.get("p256dh"), str)
            or not isinstance(keys.get("auth"), str) or len(keys["p256dh"]) > 256 or len(keys["auth"]) > 256):
        raise web.HTTPBadRequest(text="Некорректная push-подписка")
    await asyncio.to_thread(save_subscription, d.data_dir, uid, subscription)
    return web.json_response({"saved": True})



async def api_admin_push_test(request: web.Request) -> web.Response:
    from push_service import active_subscriptions, send_push
    d: Deps = request.app["deps"]
    if not require_admin(request, d):
        return web.json_response({"error": "forbidden", "message": "Только для администратора"}, status=403)
    private_key = os.getenv("WEBPUSH_VAPID_PRIVATE_KEY", "").strip()
    subject = os.getenv("WEBPUSH_VAPID_SUBJECT", "https://sa225-miniapp-production.up.railway.app").strip()
    if not private_key:
        return web.json_response({"sent": 0, "message": "Push не настроен на сервере"}, status=503)

    targets = await asyncio.to_thread(active_subscriptions, d.data_dir)
    if not targets:
        return web.json_response({"sent": 0, "total": 0, "message": "Нет пользователей с активной push-подпиской."})

    payload = {
        "title": "Вася Бот",
        "body": "дарова,газява,тест Push'a разработчиком..",
        "url": "/",
        "tag": "sa225-admin-push-test",
    }
    event_key = f"admin-test-broadcast:{time.time_ns()}"

    def broadcast() -> tuple[int, int]:
        sent = 0
        for target in targets:
            if send_push(d.data_dir, target, f"{event_key}:{target['telegram_id']}:{target['endpoint']}", payload, private_key, subject):
                sent += 1
        return sent, len(targets)

    sent, total = await asyncio.to_thread(broadcast)
    return web.json_response({"sent": sent, "total": total, "message": f"Отправлено push: {sent} из {total} устройств."})


async def api_push_unsubscribe(request: web.Request) -> web.Response:
    from push_service import remove_subscription
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 4096:
        raise web.HTTPBadRequest(text="Некорректный endpoint")
    await asyncio.to_thread(remove_subscription, d.data_dir, uid, endpoint)
    return web.json_response({"removed": True})


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


async def api_duties_get(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    today = datetime.now(d.tz).date()
    start, end = (today - timedelta(days=30)).isoformat(), (today + timedelta(days=180)).isoformat()
    async with d.users_lock:
        def read():
            db = _db_connect(d)
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS duty_no_school_days (
                    duty_date TEXT PRIMARY KEY,
                    marked_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )""")
                roster = sorted(_class_students(db, d), key=lambda x: x["full_name"].casefold())
                if roster:
                    rebuilt = db.execute("SELECT 1 FROM duty_schedule_meta WHERE meta_key='alphabetical_pair_reset_v1'").fetchone()
                    if not rebuilt:
                        # Однократный сброс будущего графика и пересборка строго парами по алфавиту.
                        db.execute("DELETE FROM class_duties WHERE duty_date>=?", (today.isoformat(),))
                        day = today
                        pair_index = 0
                        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        while day <= today + timedelta(days=180):
                            if day.weekday() < 5 and len(roster) >= 2 and not db.execute(
                                "SELECT 1 FROM duty_no_school_days WHERE duty_date=?", (day.isoformat(),)
                            ).fetchone():
                                first = pair_index % len(roster)
                                second = (pair_index + 1) % len(roster)
                                for pos in (first, second):
                                    db.execute("""INSERT INTO class_duties
                                        (duty_date, student_id, assigned_by, note, completed_at, created_at)
                                        VALUES (?, ?, 0, 'Автоматическое назначение по алфавиту', '', ?)""",
                                        (day.isoformat(), int(roster[pos]["telegram_id"]), stamp))
                                pair_index = (pair_index + 2) % len(roster)
                            day += timedelta(days=1)
                        db.execute("INSERT INTO duty_schedule_meta(meta_key, meta_value) VALUES ('alphabetical_pair_reset_v1', ?)",
                                   (stamp,))
                        db.commit()
                    else:
                        # Поддерживаем два назначения на каждом новом буднем дне.
                        day = today
                        while day <= today + timedelta(days=180):
                            if day.weekday() < 5 and not db.execute(
                                "SELECT 1 FROM duty_no_school_days WHERE duty_date=?", (day.isoformat(),)
                            ).fetchone():
                                current_ids = {int(r["student_id"]) for r in db.execute(
                                    "SELECT student_id FROM class_duties WHERE duty_date=?", (day.isoformat(),)).fetchall()}
                                if len(current_ids) > 2:
                                    db.execute("DELETE FROM class_duties WHERE duty_date=? AND student_id NOT IN (SELECT student_id FROM class_duties WHERE duty_date=? ORDER BY created_at, student_id LIMIT 2)",
                                               (day.isoformat(), day.isoformat()))
                                    current_ids = {int(r["student_id"]) for r in db.execute(
                                        "SELECT student_id FROM class_duties WHERE duty_date=?", (day.isoformat(),)).fetchall()}
                                if len(current_ids) < 2:
                                    previous = db.execute("SELECT student_id FROM class_duties WHERE duty_date<? ORDER BY duty_date DESC, created_at DESC LIMIT 1", (day.isoformat(),)).fetchone()
                                    previous_id = int(previous["student_id"]) if previous else None
                                    previous_index = next((i for i, s in enumerate(roster) if int(s["telegram_id"]) == previous_id), -1)
                                    for offset in range(len(roster)):
                                        if len(current_ids) >= 2:
                                            break
                                        chosen = int(roster[(previous_index + 1 + offset) % len(roster)]["telegram_id"])
                                        if chosen in current_ids:
                                            continue
                                        db.execute("""INSERT OR IGNORE INTO class_duties
                                            (duty_date, student_id, assigned_by, note, completed_at, created_at)
                                            VALUES (?, ?, 0, 'Автоматическое назначение', '', ?)""",
                                            (day.isoformat(), chosen, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                                        current_ids.add(chosen)
                            day += timedelta(days=1)
                        db.commit()
                students = {int(item["telegram_id"]): item for item in roster}
                rows = db.execute("""SELECT duty_date, student_id, note, completed_at, assigned_by
                    FROM class_duties WHERE duty_date BETWEEN ? AND ?
                    ORDER BY duty_date, student_id""", (start, end)).fetchall()
                duties = []
                for row in rows:
                    item = dict(row)
                    student = students.get(int(item["student_id"]))
                    if not student:
                        continue
                    item["full_name"] = student["full_name"]
                    item["registered"] = bool(student["registered"])
                    duties.append(item)
                skipped_today = bool(db.execute(
                    "SELECT 1 FROM duty_no_school_days WHERE duty_date=?", (today.isoformat(),)
                ).fetchone())
                return duties, skipped_today
            finally:
                db.close()
        duties, today_skipped = await asyncio.to_thread(read)
    uid = int((request.get("user") or {}).get("id", 0))
    roles = _journal_roles(d, uid)
    return web.json_response({"duties": duties, "can_manage": _journal_can_edit(d, uid),
                              "can_view_preview": bool(set(roles) & {"starosta", "deputy"}),
                              "today": today.isoformat(), "today_skipped": today_skipped})

async def api_duties_skip_day(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    if not _journal_can_edit(d, uid):
        return web.json_response({"error": "forbidden", "message": "Пропускать учебный день могут староста, заместитель и администрация."}, status=403)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректный JSON")
    if not isinstance(body, dict) or set(body) != {"date"}:
        raise web.HTTPBadRequest(text="Укажите дату")
    try:
        duty_day = date.fromisoformat(body["date"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Некорректная дата")
    if duty_day != datetime.now(d.tz).date() or duty_day.weekday() > 4:
        raise web.HTTPBadRequest(text="Можно пропустить только текущий учебный день")
    try:
        raw_lessons = await d.get_lessons(duty_day)
        fourth_pair_present = _has_fourth_pair(raw_lessons)
        lessons = _day_json(d, duty_day, raw_lessons, False)["lessons"]
    except Exception as exc:
        log.warning("Не удалось проверить расписание для пропуска дежурства %s: %s", duty_day, exc)
        return web.json_response({"error": "schedule_unavailable", "message": "Не удалось проверить расписание. Попробуйте позже."}, status=503)
    if fourth_pair_present:
        raise web.HTTPBadRequest(text="Четвёртая пара заполнена в расписании — пропустить этот день нельзя")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with d.users_lock:
        def shift_schedule():
            db = _db_connect(d)
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS duty_no_school_days (
                    duty_date TEXT PRIMARY KEY,
                    marked_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )""")
                if db.execute("SELECT 1 FROM duty_no_school_days WHERE duty_date=?", (duty_day.isoformat(),)).fetchone():
                    return False
                rows = db.execute("""SELECT duty_date, student_id, assigned_by, note, completed_at, created_at
                    FROM class_duties WHERE duty_date>=? ORDER BY duty_date, student_id""",
                    (duty_day.isoformat(),)).fetchall()
                if not rows:
                    return False
                old_skip_days = db.execute(
                    "SELECT duty_date, marked_by, created_at FROM duty_no_school_days WHERE duty_date>=? ORDER BY duty_date",
                    (duty_day.isoformat(),)
                ).fetchall()
                shifted_skip_days = []
                for skip_row in old_skip_days:
                    target = date.fromisoformat(skip_row["duty_date"]) + timedelta(days=1)
                    while target.weekday() > 4:
                        target += timedelta(days=1)
                    shifted_skip_days.append((target.isoformat(), int(skip_row["marked_by"]), skip_row["created_at"]))
                shifted = []
                for row in rows:
                    target = date.fromisoformat(row["duty_date"]) + timedelta(days=1)
                    while target.weekday() > 4:
                        target += timedelta(days=1)
                    shifted.append((target.isoformat(), int(row["student_id"]), int(row["assigned_by"]),
                                    row["note"], row["completed_at"], row["created_at"]))
                db.execute("DELETE FROM class_duties WHERE duty_date>=?", (duty_day.isoformat(),))
                db.executemany("""INSERT INTO class_duties
                    (duty_date, student_id, assigned_by, note, completed_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""", shifted)
                db.execute("DELETE FROM duty_no_school_days WHERE duty_date>=?", (duty_day.isoformat(),))
                db.execute("INSERT INTO duty_no_school_days(duty_date, marked_by, created_at) VALUES (?, ?, ?)",
                           (duty_day.isoformat(), uid, stamp))
                db.executemany("INSERT INTO duty_no_school_days(duty_date, marked_by, created_at) VALUES (?, ?, ?)",
                               shifted_skip_days)
                db.commit()
                return True
            finally:
                db.close()
        shifted = await asyncio.to_thread(shift_schedule)
    if not shifted:
        raise web.HTTPConflict(text="Этот день уже пропущен или график на него не найден")
    return web.json_response({"saved": True, "date": duty_day.isoformat(), "shifted": True})


async def api_duties_rebuild(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    if not _journal_can_edit(d, uid):
        return web.json_response({"error": "forbidden", "message": "Перестраивать список могут староста, заместитель и администрация."}, status=403)
    today = datetime.now(d.tz).date()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with d.users_lock:
        def rebuild():
            db = _db_connect(d)
            try:
                roster = sorted(_class_students(db, d), key=lambda x: x["full_name"].casefold())
                if len(roster) < 2:
                    raise ValueError("Для составления графика нужны минимум два студента")
                db.execute("DELETE FROM class_duties WHERE duty_date>=?", (today.isoformat(),))
                day, pair_index, count = today, 0, 0
                db.execute("""CREATE TABLE IF NOT EXISTS duty_no_school_days (
                    duty_date TEXT PRIMARY KEY,
                    marked_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )""")
                no_duty_days = {row["duty_date"] for row in db.execute(
                    "SELECT duty_date FROM duty_no_school_days WHERE duty_date>=?", (today.isoformat(),)
                ).fetchall()}
                while day <= today + timedelta(days=180):
                    if day.weekday() < 5 and day.isoformat() not in no_duty_days:
                        first = pair_index % len(roster)
                        second = (pair_index + 1) % len(roster)
                        for pos in (first, second):
                            db.execute("""INSERT INTO class_duties
                                (duty_date, student_id, assigned_by, note, completed_at, created_at)
                                VALUES (?, ?, ?, 'Автоматическое назначение по алфавиту', '', ?)""",
                                (day.isoformat(), int(roster[pos]["telegram_id"]), uid, stamp))
                        pair_index = (pair_index + 2) % len(roster)
                        count += 2
                    day += timedelta(days=1)
                db.execute("""INSERT INTO duty_schedule_meta(meta_key, meta_value)
                    VALUES ('alphabetical_pair_reset_v1', ?)
                    ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value""", (stamp,))
                db.commit()
                return count
            finally:
                db.close()
        try:
            assigned = await asyncio.to_thread(rebuild)
        except ValueError as exc:
            return web.json_response({"error": "invalid", "message": str(exc)}, status=400)
    return web.json_response({"rebuilt": True, "assigned": assigned, "from": today.isoformat()})

async def api_duties_save(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    if not _journal_can_edit(d, uid):
        return web.json_response({"error": "forbidden", "message": "Назначать дежурства могут староста, заместитель и администрация."}, status=403)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    if not isinstance(body, dict) or set(body) != {"date", "student_ids", "note"}:
        raise web.HTTPBadRequest(text="Передайте дату, список студентов и примечание")
    try:
        duty_day = date.fromisoformat(body["date"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Некорректная дата")
    if abs((duty_day - datetime.now(d.tz).date()).days) > 180:
        raise web.HTTPBadRequest(text="Можно назначать дежурства только в пределах 180 дней")
    if duty_day.weekday() > 4:
        raise web.HTTPBadRequest(text="Дежурства можно назначать только с понедельника по пятницу")
    ids, note = body["student_ids"], body["note"]
    if not isinstance(ids, list) or len(ids) != 2 or any(type(x) is not int or x == 0 for x in ids) or len(ids) != len(set(ids)):
        raise web.HTTPBadRequest(text="Выберите ровно двух разных студентов")
    if not isinstance(note, str) or len(note.strip()) > 160:
        raise web.HTTPBadRequest(text="Примечание — не более 160 символов")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with d.users_lock:
        def save():
            db = _db_connect(d)
            try:
                placeholders = ",".join("?" for _ in ids)
                known = {int(item["telegram_id"]) for item in _class_students(db, d)
                         if int(item["telegram_id"]) in set(ids)}
                if known != set(ids):
                    raise ValueError("В списке есть неизвестные студенты")
                db.execute("DELETE FROM class_duties WHERE duty_date=?", (duty_day.isoformat(),))
                db.executemany("""INSERT INTO class_duties
                    (duty_date, student_id, assigned_by, note, completed_at, created_at)
                    VALUES (?, ?, ?, ?, '', ?)""",
                    [(duty_day.isoformat(), student_id, uid, note.strip(), now) for student_id in ids])
                db.commit()
            finally:
                db.close()
        try:
            await asyncio.to_thread(save)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({"saved": True, "date": duty_day.isoformat(), "assigned": len(ids)})


async def api_duties_skip(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    if not _journal_can_edit(d, uid):
        return web.json_response({"error": "forbidden", "message": "Пропускать дежурство могут староста, заместитель и администрация."}, status=403)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректный JSON")
    if not isinstance(body, dict) or set(body) != {"date", "student_id"} or type(body["student_id"]) is not int or body["student_id"] == 0:
        raise web.HTTPBadRequest(text="Укажите дату и студента")
    try:
        duty_day = date.fromisoformat(body["date"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Некорректная дата")
    if duty_day != datetime.now(d.tz).date():
        raise web.HTTPBadRequest(text="Можно перенести дежурство только с текущей даты")
    if duty_day.weekday() > 4:
        raise web.HTTPBadRequest(text="Дежурства назначаются только по будням")
    next_day = duty_day + timedelta(days=1)
    while next_day.weekday() > 4:
        next_day += timedelta(days=1)
    student_id = body["student_id"]
    async with d.users_lock:
        def move():
            db = _db_connect(d)
            try:
                roster = sorted(_class_students(db, d), key=lambda x: x["full_name"].casefold())
                known = [int(item["telegram_id"]) for item in roster]
                today_rows = db.execute("SELECT student_id FROM class_duties WHERE duty_date=? ORDER BY created_at, student_id", (duty_day.isoformat(),)).fetchall()
                today_ids = [int(row["student_id"]) for row in today_rows]
                if student_id not in known or student_id not in today_ids or len(today_ids) != 2:
                    return False
                # Следующий по алфавитной очереди занимает пропущенное место сегодня.
                skipped_index = known.index(student_id)
                replacement = next((known[(skipped_index + step) % len(known)]
                                    for step in range(1, len(known) + 1)
                                    if known[(skipped_index + step) % len(known)] not in today_ids), None)
                if replacement is None:
                    return False
                db.execute("DELETE FROM class_duties WHERE duty_date=? AND student_id=?", (duty_day.isoformat(), student_id))
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                db.execute("""INSERT INTO class_duties
                    (duty_date, student_id, assigned_by, note, completed_at, created_at)
                    VALUES (?, ?, ?, 'Замена пропустившего дежурство', '', ?)""",
                    (duty_day.isoformat(), replacement, uid, now))
                # Пропустивший занимает место заменившего на следующем учебном дне.
                next_rows = db.execute("SELECT student_id FROM class_duties WHERE duty_date=? ORDER BY created_at, student_id", (next_day.isoformat(),)).fetchall()
                next_ids = [int(row["student_id"]) for row in next_rows]
                if replacement in next_ids:
                    db.execute("DELETE FROM class_duties WHERE duty_date=? AND student_id=?", (next_day.isoformat(), replacement))
                elif len(next_ids) >= 2:
                    db.execute("DELETE FROM class_duties WHERE duty_date=? AND student_id=?", (next_day.isoformat(), next_ids[-1]))
                db.execute("""INSERT INTO class_duties
                    (duty_date, student_id, assigned_by, note, completed_at, created_at)
                    VALUES (?, ?, ?, 'Перенесён после пропуска', '', ?)
                    ON CONFLICT(duty_date, student_id) DO UPDATE SET
                    assigned_by=excluded.assigned_by, note=excluded.note, completed_at=''""",
                    (next_day.isoformat(), student_id, uid, now))
                db.commit()
                return True
            finally:
                db.close()
        moved = await asyncio.to_thread(move)
    if not moved:
        raise web.HTTPNotFound(text="Назначение не найдено")
    return web.json_response({"saved": True, "next_date": next_day.isoformat()})


async def api_duties_complete(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    uid = int((request.get("user") or {}).get("id", 0))
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректный JSON")
    if not isinstance(body, dict) or set(body) != {"date", "student_id", "completed"} or type(body["student_id"]) is not int or type(body["completed"]) is not bool:
        raise web.HTTPBadRequest(text="Некорректные данные отметки")
    try:
        duty_day = date.fromisoformat(body["date"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Некорректная дата")
    student_id = body["student_id"]
    async with d.users_lock:
        def update():
            db = _db_connect(d)
            try:
                row = db.execute("SELECT student_id FROM class_duties WHERE duty_date=? AND student_id=?",
                                 (duty_day.isoformat(), student_id)).fetchone()
                if not row:
                    return False
                if uid != student_id and not _journal_can_edit(d, uid):
                    raise PermissionError
                completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds") if body["completed"] else ""
                db.execute("UPDATE class_duties SET completed_at=? WHERE duty_date=? AND student_id=?",
                           (completed_at, duty_day.isoformat(), student_id))
                db.commit()
                return True
            finally:
                db.close()
        try:
            updated = await asyncio.to_thread(update)
        except PermissionError:
            return web.json_response({"error": "forbidden", "message": "Отметить выполнение может назначенный студент или староста."}, status=403)
    if not updated:
        raise web.HTTPNotFound(text="Дежурство не найдено")
    return web.json_response({"saved": True, "completed": body["completed"]})


def create_app(deps: Deps) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=1024 * 1024)
    app["deps"] = deps
    app.router.add_get("/", index)
    app.router.add_get("/manifest.webmanifest", app_manifest)
    app.router.add_get("/sw.js", app_service_worker)
    app.router.add_get("/icon.svg", app_icon)
    app.router.add_get("/apple-touch-icon.png", apple_touch_icon)
    app.router.add_get("/healthz", health)
    app.router.add_post("/auth/telegram", auth_telegram)
    app.router.add_post("/auth/logout", auth_logout)
    app.router.add_get("/api/day", api_day)
    app.router.add_get("/api/week", api_week)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/students", api_students)
    app.router.add_get("/api/students/presence", api_student_presence)
    app.router.add_post("/api/presence", api_presence)
    app.router.add_get("/api/duties", api_duties_get)
    app.router.add_post("/api/duties/rebuild", api_duties_rebuild)
    app.router.add_post("/api/duties/skip-day", api_duties_skip_day)
    app.router.add_put("/api/duties", api_duties_save)
    app.router.add_post("/api/duties/skip", api_duties_skip)
    app.router.add_post("/api/duties/complete", api_duties_complete)
    app.router.add_get("/api/journal", api_journal_get)
    app.router.add_get("/api/journal/history", api_journal_history)
    app.router.add_post("/api/journal/send-today-report", api_journal_send_today_report)
    app.router.add_post("/api/journal", api_journal_save)
    app.router.add_post("/api/journal/bulk", api_journal_bulk_save)
    app.router.add_post("/api/access", api_access)
    app.router.add_get("/api/admin/access-code", api_admin_current_access_code)
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
    app.router.add_get("/api/push/config", api_push_config)
    app.router.add_post("/api/push/subscribe", api_push_subscribe)
    app.router.add_post("/api/push/unsubscribe", api_push_unsubscribe)
    app.router.add_post("/api/admin/push/test", api_admin_push_test)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Mini App слушает %s:%d", host, port)
    return runner
