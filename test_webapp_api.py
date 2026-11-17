"""Интеграционная проверка API Mini App (запуск: python tests/test_webapp_api.py)."""
import asyncio
import sys
import tempfile
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bells import Bells  # noqa: E402
from webapp_server import Deps, create_app, _db_connect, _save_telegram_photo_url, _class_students  # noqa: E402


async def get_lessons(day: date):
    return [{"number": 1, "subject": "Тестовый предмет", "room": "101", "teacher": "Иванов И. И."}]


async def run_tests():
    with tempfile.TemporaryDirectory() as tmp:
        deps = Deps(token="test", group="СА-2/25", tz=ZoneInfo("Europe/Kaliningrad"),
                    tz_name="Europe/Kaliningrad", get_lessons=get_lessons,
                    bells=Bells(table={"default": {"1": ["08:30", "10:00"]}}),
                    dev=True, data_dir=Path(tmp))
        db = _db_connect(deps)
        try:
            db.execute(
                "INSERT INTO registered_users (telegram_id, full_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (123, "Тестовый Студент", "now", "now"),
            )
            db.commit()
        finally:
            db.close()
        await _save_telegram_photo_url(deps, 123, "https://t.me/i/userpic/320/test.jpg")
        db = _db_connect(deps)
        try:
            student = next(item for item in _class_students(db, deps) if item["telegram_id"] == 123)
            assert student["telegram_photo_url"] == "https://t.me/i/userpic/320/test.jpg"
        finally:
            db.close()

        client = TestClient(TestServer(create_app(deps)))
        await client.start_server()
        try:
            me = await client.get("/api/me")
            assert me.status == 200
            payload = await me.json()
            assert payload["group"] == "СА-2/25"
            assert payload["user"]["id"] == 1

            saved = await client.post("/api/preferences", json={"lesson_reminders": False})
            assert saved.status == 200
            assert (await saved.json())["preferences"]["lesson_reminders"] is False
            assert (Path(tmp) / "miniapp_preferences.json").exists()

            week = await client.get(f"/api/week?date={date.today().isoformat()}")
            assert week.status == 200
            days = (await week.json())["days"]
            assert len(days) == 7
            assert days[0]["lessons"][0]["subject"] == "Тестовый предмет"

            invalid = await client.post("/api/preferences", json={"unknown": True})
            assert invalid.status == 400
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(run_tests())
    print("webapp API tests OK")
