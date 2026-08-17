import datetime
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "behavior_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for _ext in ("", "-wal", "-shm"):
    _p = os.environ["DB_PATH"] + _ext
    if os.path.exists(_p):
        os.remove(_p)

from bot import db  # noqa: E402
from bot.config import (  # noqa: E402
    DAILY_FREE_MESSAGES,
    SUBSCRIPTION_DAILY_MESSAGES,
)
from bot.payments import PRICE_VERSION, resolve_package  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- 1. Free user's limit is still DAILY_FREE_MESSAGES ---
FREE_UID = 1001
count = 0
while True:
    allowed, status = db.try_consume_message(FREE_UID, "free_user", DAILY_FREE_MESSAGES, 3, 3)
    if not allowed:
        break
    count += 1
check(f"free user gets exactly DAILY_FREE_MESSAGES={DAILY_FREE_MESSAGES} (got {count})", count == DAILY_FREE_MESSAGES)
check("free user denial has limit_source=free", status["limit_source"] == "free")

# --- 2/3/4. Subscriber: consumes SUBSCRIPTION_DAILY_MESSAGES, then bonus_credits, then denial ---
SUB_UID = 1002
db._ensure_player(SUB_UID, "sub_user")
future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)).strftime(
    "%Y-%m-%d %H:%M:%S"
)
with db._lock:
    db._conn.execute(
        "UPDATE players SET subscription_until = ?, subscription_status = 'active', bonus_credits = 2 "
        "WHERE user_id = ?",
        (future, SUB_UID),
    )
    db._conn.commit()

sub_count = 0
last_status = None
while True:
    allowed, status = db.try_consume_message(SUB_UID, "sub_user", DAILY_FREE_MESSAGES, 3, 3)
    if not allowed:
        last_status = status
        break
    sub_count += 1
    last_status = status
    if sub_count > SUBSCRIPTION_DAILY_MESSAGES + 10:
        break

check(
    f"subscriber consumes SUBSCRIPTION_DAILY_MESSAGES + bonus_credits(2) = "
    f"{SUBSCRIPTION_DAILY_MESSAGES + 2} messages before denial (got {sub_count})",
    sub_count == SUBSCRIPTION_DAILY_MESSAGES + 2,
)
check("subscriber final denial has limit_source=subscription", last_status["limit_source"] == "subscription")
check("subscriber final denial has bonus_credits=0", last_status["bonus_credits"] == 0)

# consumed buckets: first SUBSCRIPTION_DAILY_MESSAGES should be fast_quota, then bonus_credits
db._ensure_player(9999, "probe")  # no-op, just making sure import chain is fine

# Re-run to inspect bucket sequence explicitly on a fresh subscriber
SUB_UID2 = 1003
db._ensure_player(SUB_UID2, "sub_user2")
with db._lock:
    db._conn.execute(
        "UPDATE players SET subscription_until = ?, subscription_status = 'active', bonus_credits = 1 "
        "WHERE user_id = ?",
        (future, SUB_UID2),
    )
    db._conn.commit()

buckets = []
while True:
    allowed, status = db.try_consume_message(SUB_UID2, "sub_user2", DAILY_FREE_MESSAGES, 3, 3)
    if not allowed:
        break
    buckets.append(status["consumed"]["bucket"])

expected_buckets = ["fast_quota"] * SUBSCRIPTION_DAILY_MESSAGES + ["bonus_credits"]
check("subscriber bucket sequence is fast_quota*N then bonus_credits", buckets == expected_buckets)

# --- refund_consumed_message restores a subscriber's counter without going negative ---
SUB_UID3 = 1004
db._ensure_player(SUB_UID3, "sub_user3")
with db._lock:
    db._conn.execute(
        "UPDATE players SET subscription_until = ?, subscription_status = 'active' WHERE user_id = ?",
        (future, SUB_UID3),
    )
    db._conn.commit()

allowed, status = db.try_consume_message(SUB_UID3, "sub_user3", DAILY_FREE_MESSAGES, 3, 3)
before_refund = db.get_status(SUB_UID3, "sub_user3")["used_today"]
db.refund_consumed_message(SUB_UID3, status["consumed"])
after_refund = db.get_status(SUB_UID3, "sub_user3")["used_today"]
check(
    f"refund restores subscriber's used_today (before={before_refund}, after={after_refund})",
    before_refund == 1 and after_refund == 0,
)

# refund again on an already-zero counter must not go negative
db.refund_consumed_message(SUB_UID3, status["consumed"])
after_double_refund = db.get_status(SUB_UID3, "sub_user3")["used_today"]
check(f"double refund doesn't go negative (got {after_double_refund})", after_double_refund == 0)

# --- Active hourly unlimited pass: consumed stays None, no limits applied ---
PASS_UID = 1005
db._ensure_player(PASS_UID, "pass_user")
with db._lock:
    db._conn.execute(
        "UPDATE players SET unlimited_until = ? WHERE user_id = ?",
        (future, PASS_UID),
    )
    db._conn.commit()

allowed, status = db.try_consume_message(PASS_UID, "pass_user", 1, 1, 3)
check("active unlimited pass: allowed=True", allowed is True)
check("active unlimited pass: consumed is None", status["consumed"] is None)
# Hammer past what would be the free limit — should never be denied.
still_ok = True
for _ in range(20):
    allowed, _ = db.try_consume_message(PASS_UID, "pass_user", 1, 1, 3)
    still_ok = still_ok and allowed
check("active unlimited pass: 20 more requests all allowed", still_ok)

# --- resolve_package rejects any version string that isn't the CURRENT
#     PRICE_VERSION (hardcoding "v1"/"v2" here would go stale the next time
#     a price change bumps PRICE_VERSION, exactly what broke this check
#     before) ---
stale_version = "not-" + PRICE_VERSION
check(f"resolve_package with a stale version ({stale_version!r}) returns None", resolve_package("messages", stale_version, 0) is None)
check(
    f"resolve_package with the current version ({PRICE_VERSION!r}) returns a package",
    resolve_package("messages", PRICE_VERSION, 0) is not None,
)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
