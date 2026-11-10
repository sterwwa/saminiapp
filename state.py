"""Хранение состояния в JSON-файле (переживает перезапуск сервера)."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("state")


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {
            "last_sent_for": None, "last_sent_at": None, "snapshots": {},
            "last_ok": None, "last_error": None, "pinned_id": None, "reminded": {},
        }
        try:
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("Не удалось прочитать состояние (%s), начинаем с чистого", e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # атомарная запись

    # --- отправка в беседу
    @property
    def last_sent_for(self) -> str | None:
        return self.data.get("last_sent_for")

    def mark_sent(self, day: date, at: datetime | None = None) -> None:
        self.data["last_sent_for"] = day.isoformat()
        if at:
            self.data["last_sent_at"] = at.isoformat()
        self.save()

    # --- снимки расписания (для проверки изменений)
    def get_snapshot(self, day: date) -> list[dict] | None:
        return self.data["snapshots"].get(day.isoformat())

    def set_snapshot(self, day: date, lessons: list[dict]) -> None:
        self.data["snapshots"][day.isoformat()] = lessons
        self.save()

    # --- здоровье источника
    def note_ok(self, at: datetime) -> None:
        self.data["last_ok"] = at.isoformat()
        self.save()

    def note_error(self, msg: str, at: datetime) -> None:
        self.data["last_error"] = {"at": at.isoformat(), "msg": msg[:200]}
        self.save()

    # --- закреп
    @property
    def pinned_id(self) -> int | None:
        return self.data.get("pinned_id")

    def set_pinned(self, message_id: int | None) -> None:
        self.data["pinned_id"] = message_id
        self.save()

    # --- напоминания о парах
    def get_reminded(self, day: date) -> set[int]:
        return set(self.data["reminded"].get(day.isoformat(), []))

    def add_reminded(self, day: date, number: int) -> None:
        cur = self.data["reminded"].setdefault(day.isoformat(), [])
        if number not in cur:
            cur.append(number)
        self.save()

    def prune(self, today: date, keep_days: int = 3) -> None:
        limit = (today - timedelta(days=keep_days)).isoformat()
        for section in ("snapshots", "reminded"):
            d = self.data[section]
            for k in [k for k in d if k < limit]:
                del d[k]
