"""Проверка подлинности Telegram Mini App initData по HMAC-SHA-256."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age: int = 3600,
    now: float | None = None,
) -> dict | None:
    """Возвращает пользователя только при корректной подписи и свежем auth_date."""
    if not init_data or not bot_token or len(init_data) > 8192:
        return None
    try:
        items = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    # Повторяющиеся параметры неоднозначны — отклоняем их.
    keys = [key for key, _ in items]
    if len(keys) != len(set(keys)):
        return None
    pairs = dict(items)
    received = pairs.pop("hash", None)
    if not received or not re.fullmatch(r"[0-9a-fA-F]{64}", received):
        return None
    auth_date_raw = pairs.get("auth_date")
    user_raw = pairs.get("user")
    if not auth_date_raw or not user_raw:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received.lower()):
        return None
    try:
        age = (now if now is not None else time.time()) - int(auth_date_raw)
        user = json.loads(user_raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if age > max_age or age < -60 or not isinstance(user, dict):
        return None
    uid = user.get("id")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        return None
    return user
