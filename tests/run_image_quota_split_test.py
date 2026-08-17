import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "image_split_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for _ext in ("", "-wal", "-shm"):
    _p = os.environ["DB_PATH"] + _ext
    if os.path.exists(_p):
        os.remove(_p)

from bot import db  # noqa: E402
from bot.config import DAILY_FREE_IMAGE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# Images no longer compete with premium chat for the same daily slots.
U1 = 3001
db._ensure_player(U1, "u1")

# Spend all premium chat quota first.
for _ in range(DAILY_FREE_PREMIUM_MESSAGES):
    allowed, s = db.try_consume_message(U1, "u1", 10, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST, force_premium=True)
    assert allowed
check("premium chat quota exhausted", s["premium_used_today"] == DAILY_FREE_PREMIUM_MESSAGES)

# Images should still work — separate pool, untouched by the above.
allowed, img_status = db.try_consume_image(U1, "u1", DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST)
check("image still allowed after premium chat quota is exhausted (separate pool)", allowed is True)
check("image consumed bucket is image_quota, not premium_quota", img_status["consumed"]["bucket"] == "image_quota")

# Exhaust the image pool specifically, then give bonus credits for the fallback check.
count = 1  # already spent one above
while True:
    allowed, img_status = db.try_consume_image(U1, "u1", DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST)
    if not allowed or img_status["consumed"]["bucket"] != "image_quota":
        break
    count += 1
check(f"image pool caps at DAILY_FREE_IMAGE_MESSAGES={DAILY_FREE_IMAGE_MESSAGES} (got {count})", count == DAILY_FREE_IMAGE_MESSAGES)
check("denied once image pool + 0 bonus credits are both exhausted", allowed is False)

db.admin_add_bonus_credits(U1, 100)
allowed, img_status = db.try_consume_image(U1, "u1", DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST)
check("falls back to bonus_credits at PREMIUM_CREDIT_COST after the image pool", img_status["consumed"]["bucket"] == "bonus_credits" and img_status["consumed"]["amount"] == PREMIUM_CREDIT_COST)

# Premium chat quota is untouched by all the image spending above.
status_after = db.get_status(U1, "u1")
check("premium chat quota unaffected by image spending", status_after["premium_used_today"] == DAILY_FREE_PREMIUM_MESSAGES)

# Denial with 0 bonus credits.
U2 = 3002
db._ensure_player(U2, "u2")
count = 0
while True:
    allowed, s2 = db.try_consume_image(U2, "u2", DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST)
    if not allowed:
        break
    count += 1
check(f"denial after {DAILY_FREE_IMAGE_MESSAGES} images with 0 bonus credits (got {count})", count == DAILY_FREE_IMAGE_MESSAGES)

# refund_consumed_message reverses an image_quota spend correctly.
U3 = 3003
db._ensure_player(U3, "u3")
allowed, s3 = db.try_consume_image(U3, "u3", DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST)
before = db.get_status(U3, "u3")["images_used_today"]
db.refund_consumed_message(U3, s3["consumed"])
after = db.get_status(U3, "u3")["images_used_today"]
check(f"refund reverses image_quota spend (before={before}, after={after})", before == 1 and after == 0)

# Purchased unlimited pass bypasses the image pool entirely (consumed=None).
U4 = 3004
db._ensure_player(U4, "u4")
db.activate_unlimited(U4, "u4", 60)
allowed, s4 = db.try_consume_image(U4, "u4", DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST)
check("unlimited pass: image allowed", allowed is True)
check("unlimited pass: consumed is None (doesn't touch the pool)", s4["consumed"] is None)

# get_status exposes images_used_today.
check("get_status includes images_used_today", "images_used_today" in db.get_status(U1, "u1"))

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
