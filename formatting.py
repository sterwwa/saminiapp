"""Оформление сообщений (Telegram HTML)."""
from __future__ import annotations

from datetime import date
from html import escape

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
NUM_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣",
             6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣"}
LINE = "▬▬▬▬▬▬▬▬▬▬▬▬"
SOURCE = "Источник: guspoliteh.ru"
NO_CLASSES = "🎉 <b>Занятий нет</b>\nМожно отдыхать 😌"


def date_ru(d: date, weekday: bool = False) -> str:
    s = f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year} года"
    return f"{s} ({WEEKDAYS[d.weekday()]})" if weekday else s


def date_title(d: date, today: date | None = None) -> str:
    """«Среда, 11 ноября 2026 · завтра»"""
    s = f"{WEEKDAYS[d.weekday()].capitalize()}, {d.day} {MONTHS_GEN[d.month - 1]} {d.year}"
    rel = {0: "сегодня", 1: "завтра"}.get((d - today).days) if today else None
    return f"{s} · {rel}" if rel else s


def pairs_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "пара"
    if n % 10 in (2, 3, 4) and not 12 <= n % 100 <= 14:
        return "пары"
    return "пар"


def _meta(l: dict, times: dict | None) -> list[str]:
    parts = []
    rng = (times or {}).get(l.get("number"))
    if rng:
        parts.append(f"🕐 {rng}")
    if l.get("room"):
        parts.append(f"🏫 каб. {escape(l['room'])}")
    return parts


def _lesson_block(l: dict, times: dict | None = None) -> str:
    emoji = NUM_EMOJI.get(l.get("number"), "▫️")
    lines = [f"{emoji} <b>{escape(l['subject'])}</b>"]
    meta = _meta(l, times)
    if meta:
        lines.append("  ·  ".join(meta))
    if l.get("teacher"):
        lines.append(f"👨‍🏫 {escape(l['teacher'])}")
    return "\n".join(lines)


def _lesson_compact(l: dict, times: dict | None = None) -> str:
    """Короткий вид для недели: 2 строки."""
    emoji = NUM_EMOJI.get(l.get("number"), "▫️")
    first = f"{emoji} <b>{escape(l['subject'])}</b>"
    if l.get("room"):
        first += f" · 🏫 {escape(l['room'])}"
    second = []
    rng = (times or {}).get(l.get("number"))
    if rng:
        second.append(f"🕐 {rng}")
    if l.get("teacher"):
        second.append(escape(l["teacher"]))
    return first + ("\n" + " · ".join(second) if second else "")


def _summary(lessons: list[dict], times: dict | None) -> str:
    n = len(lessons)
    s = f"📌 {n} {pairs_word(n)}"
    spans = [times[l["number"]] for l in lessons if times and l.get("number") in times]
    if spans:
        s += f" · {min(x.split('–')[0] for x in spans)}–{max(x.split('–')[1] for x in spans)}"
    return s


def _day_body(lessons: list[dict], times: dict | None = None) -> str:
    if not lessons:
        return NO_CLASSES
    return "\n\n".join(_lesson_block(l, times) for l in lessons) + f"\n\n{LINE}\n{_summary(lessons, times)}"


def format_day(group: str, day: date, lessons: list[dict], weekday: bool = True,
               times: dict | None = None, today: date | None = None) -> str:
    return (f"📚 <b>РАСПИСАНИЕ {escape(group)}</b>\n"
            f"📅 <b>{date_title(day, today)}</b>\n{LINE}\n\n"
            f"{_day_body(lessons, times)}\n\n{SOURCE}")


def _range_title(first: date, last: date) -> str:
    if first.month == last.month:
        return f"{first.day}–{last.day} {MONTHS_GEN[last.month - 1]} {last.year}"
    return f"{first.day} {MONTHS_GEN[first.month - 1]} – {last.day} {MONTHS_GEN[last.month - 1]} {last.year}"


def format_week(group: str, days: list[tuple[date, list[dict], dict]]) -> str:
    """days: [(дата, пары, времена_пар)]"""
    blocks = []
    for d, lessons, times in days:
        title = f"📅 <b>{WEEKDAYS[d.weekday()].capitalize()}, {d.day} {MONTHS_GEN[d.month - 1]}</b>"
        if lessons:
            n = len(lessons)
            body = "\n\n".join(_lesson_compact(l, times) for l in lessons)
            blocks.append(f"{title} · {n} {pairs_word(n)}\n\n{body}")
        else:
            blocks.append(f"{title}\n\n🎉 Занятий нет")
    head = (f"📚 <b>РАСПИСАНИЕ {escape(group)}</b>\n"
            f"🗓 <b>Неделя {_range_title(days[0][0], days[-1][0])}</b>")
    return f"{head}\n{LINE}\n\n" + f"\n\n{LINE}\n\n".join(blocks) + f"\n\n{SOURCE}"


def _by_number(lessons: list[dict]) -> dict:
    out: dict = {}
    for l in lessons:
        out.setdefault(l.get("number"), []).append(l)
    return out


def describe_changes(old: list[dict], new: list[dict]) -> list[str]:
    """Человеческое описание: «каб. 406 → 407», «➕ добавлена…», «➖ отменена…»."""
    o_map, n_map = _by_number(old), _by_number(new)
    keys = sorted(set(o_map) | set(n_map), key=lambda k: (k is None, k or 0))
    out: list[str] = []
    for k in keys:
        o, n = o_map.get(k, []), n_map.get(k, [])
        emoji = NUM_EMOJI.get(k, "▫️")
        if len(o) == 1 and len(n) == 1:
            a, b = o[0], n[0]
            parts = []
            if a["subject"] != b["subject"]:
                parts.append(f"предмет: {escape(a['subject'])} → {escape(b['subject'])}")
            if (a.get("room") or "") != (b.get("room") or ""):
                parts.append(f"каб. {escape(a.get('room') or '—')} → {escape(b.get('room') or '—')}")
            if (a.get("teacher") or "") != (b.get("teacher") or ""):
                parts.append(f"преподаватель: {escape(a.get('teacher') or '—')} → {escape(b.get('teacher') or '—')}")
            if parts:
                out.append(f"🔄 {emoji} <b>{escape(b['subject'])}</b>\n     " + "\n     ".join(parts))
            continue
        for l in (x for x in o if x not in n):
            out.append(f"➖ {emoji} Отменена: <b>{escape(l['subject'])}</b>")
        for l in (x for x in n if x not in o):
            extra = ", ".join(x for x in (f"каб. {escape(l['room'])}" if l.get("room") else "",
                                            escape(l.get("teacher") or "")) if x)
            out.append(f"➕ {emoji} Добавлена: <b>{escape(l['subject'])}</b>" + (f" ({extra})" if extra else ""))
    return out


def format_change(group: str, day: date, old: list[dict], new: list[dict],
                  times: dict | None = None) -> str:
    title = "🆕 <b>ПОЯВИЛОСЬ РАСПИСАНИЕ</b>" if not old else "⚠️ <b>ИЗМЕНЕНИЕ В РАСПИСАНИИ</b>"
    head = f"{title}\n📚 {escape(group)}\n📅 <b>{date_title(day)}</b>\n{LINE}"
    if not old:
        return f"{head}\n\n{_day_body(new, times)}\n\n{SOURCE}"
    changes = describe_changes(old, new)
    body = "\n\n".join(changes) if changes else "Состав занятий изменился."
    return f"{head}\n\n{body}\n\n{LINE}\n<b>Актуальное расписание</b>\n\n{_day_body(new, times)}\n\n{SOURCE}"


def format_reminder(lesson: dict, rng: str, minutes: int) -> str:
    n = lesson.get("number")
    return f"⏰ <b>Через {minutes} мин — {n}-я пара</b>\n\n{_lesson_block(lesson, {n: rng})}"
