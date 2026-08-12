import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from bot.config import BOT_TOKEN

MAX_AUTH_AGE_SECONDS = 24 * 60 * 60


def validate_init_data(init_data: str) -> dict | None:
    """Validates Telegram WebApp initData per Telegram's HMAC scheme.
    Returns the parsed `user` dict on success, None if invalid/expired.
    """
    if not init_data:
        return None

    try:
        pairs = parse_qsl(init_data, strict_parsing=True)
    except ValueError:
        return None

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = data.get("auth_date")
    if auth_date and time.time() - int(auth_date) > MAX_AUTH_AGE_SECONDS:
        return None

    user_raw = data.get("user")
    if not user_raw:
        return None

    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None
