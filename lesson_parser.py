"""Разбор HTML-фрагмента расписания guspoliteh.ru (ответ на POST modal2=true).

Структура ответа (проверена на реальном ответе сайта):
  .rpanel > .rlarge                      -> дата («11 ноября 2026») или группа («СА-2/25»)
  .rpanel > .rtext-muted + .rlarge + .rsmall -> пара: номер словом, «Предмет (кабинет)», преподаватель

Разметка сайта невалидна (есть теги <divr>), поэтому ищем по CSS-классам,
а не по вложенности.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date

from bs4 import BeautifulSoup

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

ORDINALS = {
    "первая": 1, "вторая": 2, "третья": 3, "четвертая": 4, "четвёртая": 4,
    "пятая": 5, "шестая": 6, "седьмая": 7, "восьмая": 8, "девятая": 9,
}

NO_DATA_RE = re.compile(r"нет\s+данн", re.I)
_SUBJ_RE = re.compile(r"^(?P<subject>.*?)\s*\((?P<room>[^()]*)\)\s*$", re.S)


class ParseError(Exception):
    """Ответ сайта не похож на расписание."""


@dataclass(frozen=True)
class Lesson:
    number: int | None   # номер пары (None, если не удалось распознать)
    number_text: str     # как написано на сайте («Первая»)
    subject: str
    room: str
    teacher: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DaySchedule:
    day: date | None
    group: str | None
    lessons: tuple[Lesson, ...]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _parse_date(text: str) -> date | None:
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text.lower())
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None


def parse_schedule(html: str) -> DaySchedule:
    soup = BeautifulSoup(html, "html.parser")
    panels = soup.select(".rpanel")
    has_lessons = any(pn.select_one(".rtext-muted") for pn in panels)
    if not has_lessons and NO_DATA_RE.search(soup.get_text(" ")):
        # Сайт сообщает «нет данных» в дни без занятий (выходные, праздники)
        day = group = None
        for pn in panels:
            large = pn.select_one(".rlarge")
            if large is None:
                continue
            text = _clean(large.get_text(" "))
            parsed = _parse_date(text)
            if parsed and day is None:
                day = parsed
            elif group is None and not parsed:
                group = text
        return DaySchedule(day=day, group=group, lessons=())
    if not panels:
        raise ParseError("В ответе нет панелей .rpanel и нет фразы «нет данных»")

    day: date | None = None
    group: str | None = None
    lessons: list[Lesson] = []

    for panel in panels:
        muted = panel.select_one(".rtext-muted")
        large = panel.select_one(".rlarge")
        small = panel.select_one(".rsmall")
        if large is None:
            continue
        large_text = _clean(large.get_text(" "))

        if muted is None:
            # служебные панели: дата или группа
            parsed = _parse_date(large_text)
            if parsed and day is None:
                day = parsed
            elif group is None:
                group = large_text
            continue

        number_text = _clean(muted.get_text(" "))
        m = _SUBJ_RE.match(large_text)
        if m:
            subject, room = _clean(m.group("subject")), _clean(m.group("room"))
        else:
            subject, room = large_text, ""

        # Если сайт оставил карточку пары без названия предмета,
        # это не занятие: не показываем его как «Занятие без названия».
        if subject.casefold() in {"", "-", "—", "без названия", "занятие без названия"}:
            continue

        teacher = _clean(small.get_text(" ")) if small is not None else ""
        lessons.append(
            Lesson(
                number=ORDINALS.get(number_text.lower()),
                number_text=number_text,
                subject=subject,
                room=room,
                teacher=teacher,
            )
        )

    return DaySchedule(day=day, group=group, lessons=tuple(lessons))
