import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bells import Bells, due_reminders, human_delta, timeline, validate_bells  # noqa: E402

TZ = ZoneInfo("Europe/Kaliningrad")
# ТЕСТОВЫЕ значения, не настоящее расписание звонков
B = Bells(table={"default": {"1": ["08:00", "09:30"], "2": ["09:40", "11:10"]}, "sat": {"2": ["09:40", "10:40"]}})


def test_weekday_override():
    from datetime import date
    assert B.day_ranges(date(2026, 11, 11))[2] == "09:40–11:10"   # среда
    assert B.day_ranges(date(2026, 11, 14))[2] == "09:40–10:40"   # суббота
    assert B.day_ranges(date(2026, 11, 14))[1] == "08:00–09:30"   # из default


def test_reminders_once_and_modes():
    from datetime import date
    L = [{"number": 1}, {"number": 2}]
    tl = timeline(B, date(2026, 11, 11), L, TZ)
    n = datetime(2026, 11, 11, 7, 40, tzinfo=TZ)
    assert len(due_reminders(tl, set(), n, 30, "first")) == 1
    assert due_reminders(tl, {1}, n, 30, "first") == []
    assert due_reminders(tl, set(), n, 30, "off") == []
    assert [l["number"] for l, _ in due_reminders(tl, set(), datetime(2026, 11, 11, 9, 20, tzinfo=TZ), 30, "all")] == [2]


def test_human_delta():
    assert human_delta(timedelta(minutes=45)) == "45 мин"
    assert human_delta(timedelta(minutes=80)) == "1 ч 20 мин"
    assert human_delta(timedelta(hours=26)) == "1 д 2 ч"


def test_broken_config_is_safe():
    assert Bells(table={"default": {"1": ["oops"]}}).configured is False


def test_bells_reject_invalid_order_and_report_overlap():
    assert Bells(table={"default": {"1": ["10:00", "09:00"]}}).configured is False
    b = Bells(table={"default": {"1": ["08:00", "09:30"], "2": ["09:00", "10:00"]}})
    assert any("Пересечение" in issue for issue in validate_bells(b))


if __name__ == "__main__":
    test_weekday_override(); test_reminders_once_and_modes(); test_human_delta(); test_broken_config_is_safe(); test_bells_reject_invalid_order_and_report_overlap(); print("bells tests OK")
