"""Расписание звонков (bells.json), таймлайн пар и логика напоминаний."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path

log = logging.getLogger("bells")
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _t(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


class Bells:
    """bells.json: {"default": {"1": ["HH:MM","HH:MM"], ...}, "sat": {...}}.
    Ключи mon..sun необязательны и переопределяют default для конкретных номеров пар."""

    def __init__(self, path: Path | None = None, table: dict | None = None):
        self.table: dict[str, dict[int, tuple[time, time]]] = {}
        try:
            raw = table if table is not None else json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("корень конфигурации должен быть объектом")
            for key, nums in raw.items():
                if key.startswith("_"):
                    continue
                if key != "default" and key not in WEEKDAY_KEYS:
                    raise ValueError(f"неизвестный день недели: {key}")
                if not isinstance(nums, dict):
                    raise ValueError(f"настройка {key} должна быть объектом")
                parsed = {}
                for n, pair in nums.items():
                    number = int(n)
                    if number < 1 or number > 12 or not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        raise ValueError(f"некорректная пара {n} в {key}")
                    start, end = _t(pair[0]), _t(pair[1])
                    if start >= end:
                        raise ValueError(f"окончание пары {n} должно быть позже начала ({key})")
                    parsed[number] = (start, end)
                self.table[key] = parsed
        except FileNotFoundError:
            log.warning("Файл звонков не найден — время пар не показывается")
        except (OSError, ValueError, TypeError) as e:
            log.error("Ошибка в bells.json (%s) — время пар не показывается", e)
            self.table = {}

    @property
    def configured(self) -> bool:
        return any(self.table.values())

    def day_times(self, day: date) -> dict[int, tuple[time, time]]:
        merged = dict(self.table.get("default", {}))
        merged.update(self.table.get(WEEKDAY_KEYS[day.weekday()], {}))
        return merged

    def day_ranges(self, day: date) -> dict[int, str]:
        return {n: f"{a:%H:%M}–{b:%H:%M}" for n, (a, b) in self.day_times(day).items()}


def timeline(bells: Bells, day: date, lessons: list[dict], tz: tzinfo):
    """[(lesson, start_dt, end_dt)] для пар, у которых известно время."""
    times = bells.day_times(day)
    out = []
    for l in lessons:
        n = l.get("number")
        if n in times:
            a, b = times[n]
            out.append((l, datetime.combine(day, a, tzinfo=tz), datetime.combine(day, b, tzinfo=tz)))
    out.sort(key=lambda x: x[1])
    return out


def find_current_next(tl, n: datetime):
    current = next((x for x in tl if x[1] <= n < x[2]), None)
    upcoming = next((x for x in tl if x[1] > n), None)
    return current, upcoming


def due_reminders(tl, reminded: set[int], n: datetime, minutes: int, mode: str):
    """Какие пары пора анонсировать сейчас. mode: first | all | off."""
    if mode == "off" or not tl:
        return []
    targets = tl[:1] if mode == "first" else tl
    out = []
    for l, start, _end in targets:
        num = l.get("number")
        if num not in reminded and start - timedelta(minutes=minutes) <= n < start:
            out.append((l, start))
    return out


def human_delta(td: timedelta) -> str:
    total = max(int(td.total_seconds() // 60), 0)
    d, rem = divmod(total, 1440)
    h, m = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if m or not parts:
        parts.append(f"{m} мин")
    return " ".join(parts)


def validate_bells(bells: Bells) -> list[str]:
    """Диагностика формата и пересечений; не определяет, совпадает ли конфиг с реальным расписанием техникума."""
    issues: list[str] = []
    if not bells.configured:
        return ["Время звонков не настроено."]
    for day_key in ["default", *WEEKDAY_KEYS]:
        if day_key == "default":
            slots = bells.table.get("default", {})
        else:
            slots = dict(bells.table.get("default", {}))
            slots.update(bells.table.get(day_key, {}))
        ordered = sorted(slots.items(), key=lambda item: item[1][0])
        for (n1, (_a1, b1)), (n2, (a2, _b2)) in zip(ordered, ordered[1:]):
            if a2 < b1:
                issues.append(f"Пересечение пар {n1} и {n2} в {day_key}.")
    return issues
