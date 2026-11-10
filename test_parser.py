"""Запуск: python -m pytest tests  (или python tests/test_parser.py)"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lesson_parser import ParseError, parse_schedule  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "real_2026-11-11.html"


def test_real_sample():
    s = parse_schedule(SAMPLE.read_text(encoding="utf-8"))
    assert s.day == date(2026, 11, 11) and s.group == "СА-2/25"
    assert [(l.number, l.subject, l.room, l.teacher) for l in s.lessons] == [
        (1, "История", "209", "Перепелов В. Н."),
        (2, "История", "209", "Перепелов В. Н."),
        (3, "Основы электротехники", "301М", "Зараковский И. В."),
        (4, "Иностранный язык", "406", "Лигостаева А. Н."),
    ]


def test_no_data_is_empty_day():
    assert parse_schedule("Нет данных").lessons == ()


def test_garbage_is_error():
    for bad in ("", "<html>Ошибка запроса!</html>"):
        try:
            parse_schedule(bad)
        except ParseError:
            continue
        raise AssertionError("ожидалась ParseError")


if __name__ == "__main__":
    test_real_sample(); test_no_data_is_empty_day(); test_garbage_is_error(); print("tests OK")
