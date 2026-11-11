"""Persistent Web Push subscriptions and delivery ledger for the Mini App."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from pywebpush import WebPushException, webpush


def _connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "miniapp_users.sqlite3", timeout=15)
    db.execute("""CREATE TABLE IF NOT EXISTS webpush_subscriptions (
        telegram_id INTEGER NOT NULL, endpoint TEXT NOT NULL, subscription_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (telegram_id, endpoint))""")
    db.execute("""CREATE TABLE IF NOT EXISTS webpush_deliveries (
        telegram_id INTEGER NOT NULL, endpoint TEXT NOT NULL, event_key TEXT NOT NULL,
        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (telegram_id, endpoint, event_key))""")
    db.commit()
    return db


def save_subscription(data_dir: Path, telegram_id: int, subscription: dict) -> None:
    db = _connect(data_dir)
    try:
        db.execute("""INSERT INTO webpush_subscriptions(telegram_id,endpoint,subscription_json,updated_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(telegram_id,endpoint) DO UPDATE SET
            subscription_json=excluded.subscription_json,updated_at=CURRENT_TIMESTAMP""",
            (telegram_id, subscription["endpoint"], json.dumps(subscription, ensure_ascii=False)))
        db.commit()
    finally:
        db.close()


def remove_subscription(data_dir: Path, telegram_id: int, endpoint: str) -> None:
    db = _connect(data_dir)
    try:
        db.execute("DELETE FROM webpush_subscriptions WHERE telegram_id=? AND endpoint=?", (telegram_id, endpoint))
        db.commit()
    finally:
        db.close()


def pending_subscriptions(data_dir: Path, event_key: str, pref_key: str = "lesson_reminders") -> list[dict]:
    try:
        prefs = json.loads((data_dir / "miniapp_preferences.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}
    db = _connect(data_dir)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "registered_users" not in tables:
            return []
        if "access_bans" in tables:
            rows = db.execute("""SELECT s.telegram_id,s.endpoint,s.subscription_json
                FROM webpush_subscriptions s JOIN registered_users u ON u.telegram_id=s.telegram_id
                LEFT JOIN access_bans b ON b.telegram_id=s.telegram_id WHERE b.telegram_id IS NULL""").fetchall()
        else:
            rows = db.execute("""SELECT s.telegram_id,s.endpoint,s.subscription_json
                FROM webpush_subscriptions s JOIN registered_users u ON u.telegram_id=s.telegram_id""").fetchall()
        result = []
        for uid, endpoint, raw in rows:
            user_prefs = prefs.get(str(uid), {}) if isinstance(prefs, dict) else {}
            if isinstance(user_prefs, dict) and user_prefs.get(pref_key, True) is False:
                continue
            if db.execute("SELECT 1 FROM webpush_deliveries WHERE telegram_id=? AND endpoint=? AND event_key=?",
                          (uid, endpoint, event_key)).fetchone():
                continue
            try:
                subscription = json.loads(raw)
            except (TypeError, ValueError):
                continue
            result.append({"telegram_id": int(uid), "endpoint": endpoint, "subscription": subscription})
        return result
    finally:
        db.close()


def send_push(data_dir: Path, target: dict, event_key: str, payload: dict, private_key: str, subject: str) -> bool:
    try:
        webpush(subscription_info=target["subscription"], data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=private_key, vapid_claims={"sub": subject}, ttl=900)
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            remove_subscription(data_dir, target["telegram_id"], target["endpoint"])
        print(f"Web Push failed for user {target['telegram_id']}: HTTP {status}: {exc}")
        return False
    db = _connect(data_dir)
    try:
        db.execute("INSERT OR IGNORE INTO webpush_deliveries(telegram_id,endpoint,event_key) VALUES(?,?,?)",
                   (target["telegram_id"], target["endpoint"], event_key))
        db.execute("DELETE FROM webpush_deliveries WHERE sent_at < datetime('now','-45 days')")
        db.commit()
    finally:
        db.close()
    return True


def active_subscriptions(data_dir: Path) -> list[dict]:
    """All registered, non-banned users with an active Web Push subscription."""
    db = _connect(data_dir)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "registered_users" not in tables:
            return []
        if "access_bans" in tables:
            rows = db.execute("""SELECT s.telegram_id,s.endpoint,s.subscription_json
                FROM webpush_subscriptions s
                JOIN registered_users u ON u.telegram_id=s.telegram_id
                LEFT JOIN access_bans b ON b.telegram_id=s.telegram_id
                WHERE b.telegram_id IS NULL""").fetchall()
        else:
            rows = db.execute("""SELECT s.telegram_id,s.endpoint,s.subscription_json
                FROM webpush_subscriptions s
                JOIN registered_users u ON u.telegram_id=s.telegram_id""").fetchall()
        result = []
        for uid, endpoint, raw in rows:
            try:
                subscription = json.loads(raw)
            except (TypeError, ValueError):
                continue
            result.append({"telegram_id": int(uid), "endpoint": endpoint, "subscription": subscription})
        return result
    finally:
        db.close()


def send_test_push(data_dir: Path, telegram_id: int, payload: dict, private_key: str, subject: str) -> int:
    db = _connect(data_dir)
    try:
        rows = db.execute(
            "SELECT endpoint, subscription_json FROM webpush_subscriptions WHERE telegram_id=?",
            (telegram_id,),
        ).fetchall()
    finally:
        db.close()
    sent = 0
    for endpoint, raw in rows:
        try:
            subscription = json.loads(raw)
        except (TypeError, ValueError):
            continue
        target = {"telegram_id": telegram_id, "endpoint": endpoint, "subscription": subscription}
        event_key = f"test:{time.time_ns()}"
        if send_push(data_dir, target, event_key, payload, private_key, subject):
            sent += 1
    return sent

