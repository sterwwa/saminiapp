"""HTTP-сервер Telegram Mini App: статика, расписание, профиль и пользовательские настройки."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from bells import Bells
from schedule import ScheduleError
from tgauth import validate_init_data

log = logging.getLogger("webapp")
INDEX = Path(__file__).resolve().parent / "webapp" / "index.html"
MAX_SHIFT_DAYS = 90
DEFAULT_PREFS = {"lesson_reminders": True, "schedule_changes": True, "daily_summary": True}
FULL_NAME_RE = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*(?:\s+[^\W\d_]+(?:[-'][^\W\d_]+)*){1,3}$", re.UNICODE)


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
        if request.path.startswith("/api/") and request.path not in {"/api/me", "/api/register", "/api/admin/users"}:
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


def _day_json(d: Deps, day: date, lessons: list[dict], error: bool) -> dict:
    times = d.bells.day_times(day)
    out = []
    for l in lessons:
        t = times.get(l.get("number"))
        out.append({
            "number": l.get("number"), "subject": l.get("subject", ""),
            "room": l.get("room", ""), "teacher": l.get("teacher", ""),
            "start": f"{t[0]:%H:%M}" if t else None,
            "end": f"{t[1]:%H:%M}" if t else None,
        })
    return {"date": day.isoformat(), "lessons": out, "error": error}


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

    async def one(day: date) -> dict:
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
                rows = db.execute("""SELECT telegram_id, full_name, first_name, last_name,
                    username, language_code, created_at, updated_at
                    FROM registered_users ORDER BY created_at DESC""").fetchall()
                return [dict(row) for row in rows]
            finally:
                db.close()
        users = await asyncio.to_thread(read)
    return web.json_response({"users": users, "total": len(users)})


async def api_me(request: web.Request) -> web.Response:
    d: Deps = request.app["deps"]
    user = request.get("user") or {}
    uid = int(user.get("id", 1))
    registered = await _get_registered_user(d, uid)
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
    app.router.add_get("/api/week", api_week)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/admin/overview", api_admin_overview)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_post("/api/register", api_register)
    app.router.add_post("/api/preferences", api_preferences)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Mini App слушает %s:%d", host, port)
    return runner

