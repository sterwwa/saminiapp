"""Independently poll the college schedule site and sync dates to the Mini App."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from schedule import ScheduleClient, ScheduleError

log = logging.getLogger("schedule_sync")
GROUP_NAME = os.getenv("GROUP_NAME", "СА-2/25").strip()
SYNC_URL = os.getenv("SCHEDULE_SYNC_URL", "").strip()
SYNC_SECRET = os.getenv("SCHEDULE_SYNC_SECRET", "").strip()
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kaliningrad"))


def bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(high, max(low, value))


async def sync_once() -> tuple[int, int]:
    # Use the configured local date, not the host's timezone.
    today = datetime.now(TIMEZONE).date()
    horizon = bounded_int("SYNC_HORIZON_DAYS", 30, 1, 90)
    days = [today + timedelta(days=offset) for offset in range(horizon + 1)]
    synced = errors = 0
    source = ScheduleClient(GROUP_NAME, retries=1, timeout=8)
    async with httpx.AsyncClient(timeout=3) as publisher:
        try:
            consecutive_errors = 0
            for day in days:
                try:
                    result = await source.get_day(day)
                    if result.day != day:
                        raise ScheduleError(f"Источник не подтвердил дату {day}")
                    lessons = []
                    for lesson in result.lessons:
                        if lesson.number is None or not lesson.subject.strip():
                            raise ScheduleError(f"Не удалось разобрать занятие на {day}")
                        lessons.append({
                            "number": lesson.number,
                            "subject": lesson.subject,
                            "room": lesson.room,
                            "teacher": lesson.teacher,
                        })
                    response = await publisher.post(
                        SYNC_URL,
                        headers={"X-Schedule-Sync-Token": SYNC_SECRET},
                        json={"group": GROUP_NAME, "date": day.isoformat(), "lessons": lessons},
                    )
                    response.raise_for_status()
                    synced += 1
                    consecutive_errors = 0
                    log.info("Передано %s: %d пар", day, len(lessons))
                except (ScheduleError, httpx.HTTPError) as exc:
                    errors += 1
                    consecutive_errors += 1
                    log.warning("Не удалось обновить %s (%s)", day, type(exc).__name__)
                    if consecutive_errors >= 2:
                        log.warning("Останавливаю цикл после двух ошибок подряд")
                        break
                await asyncio.sleep(0.5)
        finally:
            await source.close()
    return synced, errors


async def main() -> None:
    if not SYNC_URL or not SYNC_SECRET:
        raise SystemExit("Задайте SCHEDULE_SYNC_URL и SCHEDULE_SYNC_SECRET")
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    interval = bounded_int("SYNC_INTERVAL_MINUTES", 30, 5, 240)
    while True:
        try:
            synced, errors = await sync_once()
            log.info("Цикл завершён: передано дней %d, ошибок %d", synced, errors)
        except Exception:
            log.exception("Ошибка цикла синхронизации")
        await asyncio.sleep(interval * 60)


if __name__ == "__main__":
    asyncio.run(main())

