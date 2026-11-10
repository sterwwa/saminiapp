"""Получение расписания с guspoliteh.ru прямыми HTTP-запросами (без браузера).

Протокол сайта (из cal.js):
  POST https://www.guspoliteh.ru/studentu/raspisanie-zanyatiy/
  form-data: id=<id группы>, date=YYYY-MM-DD, modal2=true
  ответ: HTML-фрагмент (см. parser.py)
Id группы берём со страницы (<select id="sel_group">) по названию.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

import httpx
from bs4 import BeautifulSoup

from lesson_parser import DaySchedule, ParseError, parse_schedule

log = logging.getLogger("schedule")

PAGE_URL = "https://www.guspoliteh.ru/studentu/raspisanie-zanyatiy/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SA2-25-schedule-bot/1.0)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class ScheduleError(Exception):
    """Не удалось получить расписание (после всех повторов)."""


class ScheduleClient:
    def __init__(self, group_name: str, retries: int = 2, timeout: float = 5.0):
        self.group_name = group_name
        self.retries = retries
        self._client = httpx.AsyncClient(
            headers=HEADERS, timeout=timeout, follow_redirects=True
        )
        self._group_id: str | None = None
        self._group_id_lock = asyncio.Lock()
        self._group_id_retry_after = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _with_retries(self, what: str, func, retries: int | None = None):
        delay = 2.0
        last: Exception | None = None
        attempts = retries if retries is not None else self.retries
        for attempt in range(1, attempts + 1):
            try:
                return await func()
            except (httpx.HTTPError, ParseError, ScheduleError) as e:
                last = e
                log.warning("%s: попытка %d/%d не удалась: %s", what, attempt, attempts, e)
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay *= 2
        raise ScheduleError(f"{what}: {last}") from last

    async def _load_group_id(self) -> str:
        async def go() -> str:
            r = await self._client.get(PAGE_URL, timeout=4.0)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for opt in soup.select("#sel_group option"):
                if opt.get_text(strip=True).upper() == self.group_name.upper():
                    return opt["value"]
            raise ScheduleError(f"Группа {self.group_name} не найдена на странице")

        return await self._with_retries("Поиск id группы", go, retries=2)

    async def _ensure_group_id(self) -> str:
        if self._group_id is not None:
            return self._group_id
        async with self._group_id_lock:
            if self._group_id is not None:
                return self._group_id
            now = asyncio.get_running_loop().time()
            if now < self._group_id_retry_after:
                raise ScheduleError("Источник расписания временно не отвечает")
            try:
                self._group_id = await self._load_group_id()
                self._group_id_retry_after = 0.0
                log.info("Группа %s: id=%s", self.group_name, self._group_id)
                return self._group_id
            except ScheduleError:
                self._group_id_retry_after = asyncio.get_running_loop().time() + 15.0
                raise

    async def get_day(self, day: date) -> DaySchedule:
        """Расписание группы на дату. Пустой lessons = занятий нет."""
        await self._ensure_group_id()

        async def go() -> DaySchedule:
            result = parse_schedule(await self._post(day))
            if result.day is not None and result.day != day:
                raise ScheduleError(f"Сайт вернул {result.day}, а запрошено {day}")
            return result

        try:
            return await self._with_retries(f"Расписание на {day}", go, retries=2)
        except ScheduleError:
            self._group_id = None  # id мог измениться — перечитаем при следующем вызове
            raise

    async def _post(self, day: date) -> str:
        await self._ensure_group_id()
        r = await self._client.post(
            PAGE_URL,
            data={"id": self._group_id, "date": day.isoformat(), "modal2": "true"},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": PAGE_URL},
        )
        r.raise_for_status()
        return r.content.decode("utf-8", errors="replace")

    async def get_raw(self, day: date) -> str:
        """Сырой HTML-ответ сайта (для диагностики, команда /raw)."""
        return await self._with_retries(f"Сырой ответ на {day}", lambda: self._post(day))
