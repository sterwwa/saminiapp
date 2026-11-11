"""Telegram-бот расписания группы СА-2/25 (guspoliteh.ru)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

from bells import Bells, due_reminders, find_current_next, human_delta, timeline, validate_bells
from formatting import (NUM_EMOJI, date_ru, escape, format_change, format_day, format_reminder,
                        format_week)
from schedule import ScheduleClient, ScheduleError
from state import State
from webapp_server import Deps, create_app, start_server

load_dotenv()
log = logging.getLogger("bot")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = int(os.getenv("CHAT_ID", "-1003998580449"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kaliningrad")
GROUP_NAME = os.getenv("GROUP_NAME", "СА-2/25")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
SEND_HOUR = env_int("SEND_HOUR", 20)
CHECK_MINUTES = env_int("CHECK_MINUTES", 30)
CHECK_DAYS_AHEAD = env_int("CHECK_DAYS_AHEAD", 3)
REMIND_MODE = os.getenv("REMIND_MODE", "first").strip().lower()  # first | all | off
REMIND_MINUTES = env_int("REMIND_MINUTES", 30)
PIN_DAILY = os.getenv("PIN_DAILY", "1").strip() not in ("0", "false", "no")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}

# --- Mini App
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()      # публичный https-адрес приложения (для личных чатов и кнопки меню)
MINIAPP_LINK = os.getenv("MINIAPP_LINK", "").strip()  # t.me/<бот>/<short_name> — для кнопки в беседе
WEBAPP_PORT = env_int("WEBAPP_PORT", env_int("SERVER_PORT", env_int("PORT", 8080)))
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_DEV = os.getenv("WEBAPP_DEV", "0").strip() in ("1", "true", "yes")  # без проверки подписи, только для отладки
WEBAPP_MEMBERS_ONLY = os.getenv("WEBAPP_MEMBERS_ONLY", "0").strip() in ("1", "true", "yes")
WEBAPP_AUTH_MAX_AGE = env_int("WEBAPP_AUTH_MAX_AGE", 3600)

TZ = ZoneInfo(TIMEZONE)
ERR_TEXT = "⚠️ Не удалось получить расписание с сайта guspoliteh.ru. Попробуйте позже."

client = ScheduleClient(GROUP_NAME)
state = State(DATA_DIR / "state.json")
bells = Bells(Path(os.getenv("BELLS_FILE", "bells.json")))
scheduler: AsyncIOScheduler  # создаётся в main()
dp = Dispatcher()
bot: Bot  # создаётся в main()
_lock = asyncio.Lock()
_cache: dict[date, tuple[float, list[dict]]] = {}


def now() -> datetime:
    return datetime.now(TZ)


async def get_lessons(day: date, fresh: bool = False) -> list[dict]:
    """Пары на день (список dict). Кэш 60 с, чтобы команды не нагружали сайт."""
    hit = _cache.get(day)
    if hit and not fresh and time.monotonic() - hit[0] < 60:
        return hit[1]
    try:
        sched = await client.get_day(day)
    except ScheduleError as e:
        state.note_error(str(e), now())
        # Если сайт техникума временно недоступен, показываем последний
        # сохранённый снимок этого дня вместо пустого экрана Mini App.
        snapshot = state.get_snapshot(day)
        if snapshot is not None:
            log.warning("Источник расписания недоступен; используем сохранённый снимок на %s", day)
            _cache[day] = (time.monotonic(), snapshot)
            return snapshot
        raise
    state.note_ok(now())
    lessons = [l.to_dict() for l in sched.lessons]
    _cache[day] = (time.monotonic(), lessons)
    return lessons


async def get_miniapp_lessons(day: date) -> list[dict]:
    """Mini App fetches the requested day directly from the college site."""
    return await get_lessons(day, fresh=True)


def keyboard() -> InlineKeyboardMarkup:
    # В приветствии оставляем только переход в Mini App.
    rows = []
    if MINIAPP_LINK:  # в беседах web_app-кнопки запрещены, поэтому обычная ссылка t.me
        rows.append([InlineKeyboardButton(text="📱 Открыть приложение", url=MINIAPP_LINK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_member_cache: dict[int, tuple[float, bool]] = {}


async def is_chat_member(user_id: int) -> bool:
    """Состоит ли пользователь в беседе группы (кэш 10 минут). Нужен при WEBAPP_MEMBERS_ONLY=1."""
    hit = _member_cache.get(user_id)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        cm = await bot.get_chat_member(CHAT_ID, user_id)
        ok = cm.status not in ("left", "kicked")
    except TelegramAPIError as e:
        log.warning("get_chat_member(%s): %s", user_id, e)
        ok = False
    _member_cache[user_id] = (time.monotonic(), ok)
    return ok


async def send_long(chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    """Telegram ограничивает сообщение 4096 символами — режем по блокам, кнопки — на последнем."""
    while len(text) > 4000:
        cut = text.rfind("\n\n", 0, 4000)
        cut = cut if cut > 0 else 4000
        await bot.send_message(chat_id, text[:cut])
        text = text[cut:].lstrip()
    return await bot.send_message(chat_id, text, reply_markup=markup)


def render_day(day: date, lessons: list[dict], weekday: bool = False) -> str:
    return format_day(GROUP_NAME, day, lessons, times=bells.day_ranges(day), today=now().date())


def is_admin(m: Message) -> bool:
    return bool(ADMIN_IDS) and m.from_user is not None and m.from_user.id in ADMIN_IDS


# ---------------------------------------------------------------- команды
HELP = (
    "Привет! Это бот-помощник старосты: список группы, расписание и учёт посещаемости — всё в приложении.\n\n"
    "Нажми кнопку ниже, чтобы открыть его."
)


@dp.message(Command("start"))
async def cmd_start(m: Message) -> None:
    await m.answer(HELP, reply_markup=keyboard())


async def send_view(chat_id: int, what: str) -> None:
    """what: today | tomorrow | week — общий код для команд и кнопок."""
    today = now().date()
    try:
        if what == "week":
            monday = today - timedelta(days=today.weekday())
            if today.weekday() == 6:  # в воскресенье показываем следующую неделю
                monday += timedelta(days=7)
            days = []
            for i in range(7):
                d = monday + timedelta(days=i)
                lessons = await get_lessons(d)
                if i < 6 or lessons:  # воскресенье — только если есть занятия
                    days.append((d, lessons, bells.day_ranges(d)))
            await send_long(chat_id, format_week(GROUP_NAME, days), keyboard())
        else:
            day = today if what == "today" else today + timedelta(days=1)
            lessons = await get_lessons(day)
            await bot.send_message(chat_id, render_day(day, lessons, weekday=True), reply_markup=keyboard())
    except ScheduleError as e:
        log.error("Запрос %s: %s", what, e)
        await bot.send_message(chat_id, ERR_TEXT)


@dp.message(Command("today"))
async def cmd_today(m: Message) -> None:
    await send_view(m.chat.id, "today")


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message) -> None:
    await send_view(m.chat.id, "tomorrow")


@dp.message(Command("week"))
async def cmd_week(m: Message) -> None:
    await send_view(m.chat.id, "week")


@dp.callback_query(F.data.startswith("v:"))
async def cb_view(c: CallbackQuery) -> None:
    await c.answer()  # убирает «часики» на кнопке
    what = (c.data or "")[2:]
    if what in ("today", "tomorrow", "week") and c.message is not None:
        await send_view(c.message.chat.id, what)


@dp.message(Command("schedule"))
async def cmd_schedule(m: Message) -> None:
    """Ближайший день (начиная с завтра), в котором есть занятия."""
    start = now().date() + timedelta(days=1)
    try:
        for i in range(14):
            day = start + timedelta(days=i)
            lessons = await get_lessons(day)
            if lessons:
                await m.answer(render_day(day, lessons, weekday=True), reply_markup=keyboard())
                return
    except ScheduleError as e:
        log.error("Команда /schedule: %s", e)
        await m.answer(ERR_TEXT)
        return
    await m.answer("В ближайшие 2 недели занятий в расписании нет.")


def _lesson_text(l: dict, rng: str | None = None) -> str:
    n = l.get("number")
    parts = [f"{NUM_EMOJI.get(n, '▫️')} {escape(l['subject'])}"]
    if rng:
        parts.append(f"🕐 {rng}")
    if l.get("room"):
        parts.append(f"🏫 Кабинет: {escape(l['room'])}")
    if l.get("teacher"):
        parts.append(f"👨‍🏫 {escape(l['teacher'])}")
    return "\n".join(parts)


@dp.message(Command("next"))
async def cmd_next(m: Message) -> None:
    """Ближайшая пара и сколько времени до неё."""
    n = now()
    today = n.date()
    try:
        lessons = await get_lessons(today)
        if lessons and not bells.day_times(today):
            await m.answer("ℹ️ Время пар не заполнено в bells.json, поэтому не могу посчитать, сколько осталось.\n\n"
                           + render_day(today, lessons, weekday=True), reply_markup=keyboard())
            return
        current, upcoming = find_current_next(timeline(bells, today, lessons, TZ), n)
        parts: list[str] = []
        if current:
            l, a, b_ = current
            parts.append(f"▶️ <b>Сейчас идёт</b> (до {b_:%H:%M}, ещё {human_delta(b_ - n)})\n\n"
                         + _lesson_text(l, f"{a:%H:%M}–{b_:%H:%M}"))
        if upcoming:
            l, a, b_ = upcoming
            parts.append(f"⏭ <b>Следующая пара через {human_delta(a - n)}</b>\n\n"
                         + _lesson_text(l, f"{a:%H:%M}–{b_:%H:%M}"))
            await m.answer("\n\n➖➖➖\n\n".join(parts), reply_markup=keyboard())
            return
        # сегодня пар больше нет — ищем ближайший день с занятиями
        for i in range(1, 15):
            day = today + timedelta(days=i)
            lessons = await get_lessons(day)
            if not lessons:
                continue
            tl = timeline(bells, day, lessons, TZ)
            if tl:
                l, a, b_ = tl[0]
                label = "завтра" if i == 1 else date_ru(day, True)
                parts.append(f"😌 Сегодня пар больше нет.\n⏭ <b>Ближайшая пара — {label}, через {human_delta(a - n)}</b>\n\n"
                             + _lesson_text(l, f"{a:%H:%M}–{b_:%H:%M}"))
            else:
                parts.append(f"⏭ Ближайшие занятия: {date_ru(day, True)}\n\n{_lesson_text(lessons[0])}\n\n"
                             "ℹ️ Время пар не заполнено в bells.json.")
            await m.answer("\n\n➖➖➖\n\n".join(parts), reply_markup=keyboard())
            return
        await m.answer("В ближайшие 2 недели занятий в расписании нет.")
    except ScheduleError as e:
        log.error("Команда /next: %s", e)
        await m.answer(ERR_TEXT)


@dp.message(Command("app"))
async def cmd_app(m: Message) -> None:
    """Кнопка запуска Mini App: в личке — web_app, в беседе — ссылка t.me (web_app там не поддерживается)."""
    if m.chat.type == "private" and WEBAPP_URL:
        btn = InlineKeyboardButton(text="📱 Открыть расписание", web_app=WebAppInfo(url=WEBAPP_URL))
    elif MINIAPP_LINK:
        btn = InlineKeyboardButton(text="📱 Открыть расписание", url=MINIAPP_LINK)
    else:
        await m.answer("Мини-приложение ещё не настроено (нужны WEBAPP_URL и MINIAPP_LINK).")
        return
    await m.answer("Расписание группы в приложении:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]))


@dp.message(Command("id"))
async def cmd_id(m: Message) -> None:
    """Показывает ваш Telegram id — вписать в ADMIN_IDS, чтобы /status и /raw были только у вас."""
    await m.answer(f"Ваш id: <code>{m.from_user.id if m.from_user else '?'}</code>")


@dp.message(Command("status"))
async def cmd_status(m: Message) -> None:
    if not is_admin(m):
        await m.answer("Команда доступна только администратору.")
        return
    n = now()

    def ago(iso: str | None) -> str:
        if not iso:
            return "—"
        t = datetime.fromisoformat(iso)
        return f"{t:%d.%m %H:%M} ({human_delta(n - t)} назад)"

    def nxt(job_id: str) -> str:
        job = scheduler.get_job(job_id)
        t = job.next_run_time.astimezone(TZ) if job and job.next_run_time else None
        return f"{t:%d.%m %H:%M}" if t else "—"

    err = state.data.get("last_error")
    lines = [
        "📊 <b>Статус бота</b>",
        f"🕒 Сейчас: {n:%d.%m.%Y %H:%M} ({escape(TIMEZONE)})",
        f"✅ Последнее успешное получение расписания: {ago(state.data.get('last_ok'))}",
        f"❌ Последняя ошибка: {ago(err['at']) + ' — ' + escape(err['msg']) if err else 'не было'}",
        f"📤 Последняя отправка в беседу: на {state.last_sent_for or '—'}, отправлено {ago(state.data.get('last_sent_at'))}",
        f"⏭ Следующая отправка: {nxt('daily')}",
        f"🔍 Следующая проверка изменений: {nxt('changes')}",
        f"⏰ Напоминания: {REMIND_MODE}" + (f", за {REMIND_MINUTES} мин" if REMIND_MODE != "off" else ""),
        f"🕐 Время пар: {'заполнено' if bells.configured else 'НЕ заполнено (bells.json)'}",
        *(f"⚠️ Звонки: {escape(issue)}" for issue in validate_bells(bells)),
        f"📌 Закрепление: {'вкл' if PIN_DAILY else 'выкл'}" + (f", сообщение {state.pinned_id}" if state.pinned_id else ""),
        f"💾 Дней в памяти: {len(state.data['snapshots'])}",
    ]
    await m.answer("\n".join(lines))


@dp.message(Command("raw"))
async def cmd_raw(m: Message, command: CommandObject) -> None:
    """Диагностика: присылает файлом сырой ответ сайта. /raw 2026-11-11"""
    if not is_admin(m):
        await m.answer("Команда доступна только администратору.")
        return
    try:
        day = date.fromisoformat(command.args.strip()) if command.args else now().date() + timedelta(days=1)
        html = await client.get_raw(day)
    except ValueError:
        await m.answer("Формат: /raw ГГГГ-ММ-ДД")
        return
    except ScheduleError as e:
        await m.answer(f"Ошибка: {e}")
        return
    await m.answer_document(BufferedInputFile(html.encode("utf-8"), f"raw_{day}.html"))


# ------------------------------------------------------- автоматические задачи
async def pin_new(message_id: int) -> None:
    """Закрепляет новое вечернее сообщение и снимает закрепление с прошлого (боту нужно право «Закреплять»)."""
    if not PIN_DAILY:
        return
    try:
        await bot.pin_chat_message(CHAT_ID, message_id, disable_notification=True)
    except TelegramAPIError as e:
        log.warning("Не удалось закрепить сообщение (нужны права администратора с правом закрепления): %s", e)
        return
    old = state.pinned_id
    state.set_pinned(message_id)
    if old and old != message_id:
        try:
            await bot.unpin_chat_message(CHAT_ID, old)
        except TelegramAPIError as e:
            log.info("Не удалось открепить старое сообщение: %s", e)


async def reminder_tick() -> None:
    """Раз в минуту: анонс пары за REMIND_MINUTES минут (по сохранённому расписанию, сайт не дёргаем)."""
    if REMIND_MODE == "off" or not bells.configured:
        return
    n = now()
    today = n.date()
    lessons = state.get_snapshot(today)
    if not lessons:
        return
    tl = timeline(bells, today, lessons, TZ)
    for lesson, start in due_reminders(tl, state.get_reminded(today), n, REMIND_MINUTES, REMIND_MODE):
        minutes = max(1, round((start - n).total_seconds() / 60))
        rng = bells.day_ranges(today).get(lesson.get("number"), f"{start:%H:%M}")
        try:
            await bot.send_message(CHAT_ID, format_reminder(lesson, rng, minutes))
        except TelegramAPIError as e:
            log.error("Не удалось отправить напоминание: %s", e)
            continue
        state.add_reminded(today, lesson.get("number"))
        log.info("Напоминание о %s-й паре", lesson.get("number"))


async def daily_send() -> None:
    """Каждый день в 20:00: расписание на завтра -> в беседу."""
    async with _lock:
        day = now().date() + timedelta(days=1)
        if state.last_sent_for == day.isoformat():
            return  # уже отправляли (например, после перезапуска)
        lessons = None
        for attempt in range(1, 7):  # при сбое сайта пробуем ещё 5 раз с паузой 10 мин
            try:
                lessons = await get_lessons(day, fresh=True)
                break
            except ScheduleError as e:
                log.error("20:00, попытка %d/6: %s", attempt, e)
                if attempt < 6:
                    await asyncio.sleep(600)
        if lessons is None:
            await bot.send_message(CHAT_ID, ERR_TEXT.replace("Попробуйте позже.", "Расписание на завтра не получено, воспользуйтесь командой /tomorrow позже."))
            state.mark_sent(day, now())  # не спамим ошибкой; проверка изменений подхватит расписание позже
            return
        msg = await bot.send_message(CHAT_ID, render_day(day, lessons), reply_markup=keyboard())
        state.set_snapshot(day, lessons)
        state.mark_sent(day, now())
        await pin_new(msg.message_id)
        log.info("Отправлено расписание на %s (%d пар)", day, len(lessons))


async def check_changes() -> None:
    """Сравнение с последним известным расписанием; уведомление — только при реальном изменении."""
    async with _lock:
        today = now().date()
        state.prune(today)
        for i in range(CHECK_DAYS_AHEAD + 1):
            day = today + timedelta(days=i)
            try:
                new = await get_lessons(day, fresh=True)
            except ScheduleError as e:
                log.warning("Проверка изменений %s: %s", day, e)
                continue
            old = state.get_snapshot(day)
            if old is None:
                state.set_snapshot(day, new)  # первое наблюдение — просто запоминаем
            elif old != new:
                await bot.send_message(CHAT_ID, format_change(GROUP_NAME, day, old, new, times=bells.day_ranges(day)))
                state.set_snapshot(day, new)  # после доставки: повторов не будет
                log.info("Уведомление об изменении на %s", day)
            await asyncio.sleep(1)


async def catch_up() -> None:
    """После перезапуска: если время отправки уже прошло, а за сегодня не отправляли — отправить."""
    n = now()
    if n.hour >= SEND_HOUR:
        await daily_send()


async def main() -> None:
    global bot
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # URL с токеном не должны попадать в лог
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    if not BOT_TOKEN:
        log.critical("Не задана переменная окружения BOT_TOKEN")
        sys.exit(1)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="today", description="Расписание на сегодня"),
        BotCommand(command="tomorrow", description="Расписание на завтра"),
        BotCommand(command="week", description="Расписание на неделю"),
        BotCommand(command="schedule", description="Ближайший учебный день"),
        BotCommand(command="next", description="Ближайшая пара и время до неё"),
        BotCommand(command="app", description="Открыть мини-приложение"),
        BotCommand(command="status", description="Статус бота (админ)"),
    ])

    global scheduler
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(daily_send, CronTrigger(hour=SEND_HOUR, minute=0, timezone=TZ),
                      id="daily", misfire_grace_time=3600, coalesce=True)
    scheduler.add_job(check_changes, IntervalTrigger(minutes=CHECK_MINUTES),
                      id="changes", next_run_time=now() + timedelta(minutes=1),
                      coalesce=True, max_instances=1)
    scheduler.add_job(reminder_tick, IntervalTrigger(minutes=1), id="reminders",
                      coalesce=True, max_instances=1)
    scheduler.start()
    for issue in validate_bells(bells):
        log.warning("Проверка bells.json: %s", issue)
    runner = None
    if WEBAPP_URL or WEBAPP_DEV:
        runner = await start_server(create_app(Deps(
            token=BOT_TOKEN, group=GROUP_NAME, tz=TZ, tz_name=TIMEZONE, get_lessons=get_miniapp_lessons, bells=bells,
            is_member=is_chat_member if WEBAPP_MEMBERS_ONLY else None, dev=WEBAPP_DEV,
            data_dir=DATA_DIR, auth_max_age=WEBAPP_AUTH_MAX_AGE)), WEBAPP_HOST, WEBAPP_PORT)
        if WEBAPP_URL:
            try:  # кнопка «Расписание» рядом с полем ввода во всех личных чатах с ботом
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Расписание", web_app=WebAppInfo(url=WEBAPP_URL)))
            except TelegramAPIError as e:
                log.warning("Не удалось поставить кнопку меню: %s", e)
    asyncio.create_task(catch_up())
    log.info("Бот запущен: группа %s, чат %s, отправка в %02d:00 %s", GROUP_NAME, CHAT_ID, SEND_HOUR, TIMEZONE)
    try:
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()
        scheduler.shutdown(wait=False)
        await client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

