import hashlib, hmac, json, sys, time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tgauth import validate_init_data  # noqa: E402

TOKEN = "123456:TEST"


def sign(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def fields(age: int = 10) -> dict:
    return {"auth_date": str(int(time.time()) - age), "query_id": "AAA", "user": json.dumps({"id": 42, "first_name": "Аня"}, ensure_ascii=False)}


def test_valid():
    assert validate_init_data(sign(fields()), TOKEN)["id"] == 42


def test_wrong_token_and_tamper():
    assert validate_init_data(sign(fields()), "999:OTHER") is None
    assert validate_init_data(sign(fields()).replace("AAA", "BBB"), TOKEN) is None


def test_expired_and_empty():
    assert validate_init_data(sign(fields(age=90000)), TOKEN) is None
    assert validate_init_data("", TOKEN) is None
    assert validate_init_data("auth_date=1", TOKEN) is None
    assert validate_init_data(sign(fields()) + "&auth_date=1", TOKEN) is None


if __name__ == "__main__":
    test_valid(); test_wrong_token_and_tamper(); test_expired_and_empty(); print("tgauth tests OK")
