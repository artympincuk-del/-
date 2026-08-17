import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "image_billing_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for _ext in ("", "-wal", "-shm"):
    _p = os.environ["DB_PATH"] + _ext
    if os.path.exists(_p):
        os.remove(_p)

from bot import db  # noqa: E402
from bot.config import DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# A "fast" pref user still gets billed against the PREMIUM bucket when force_premium=True.
U1 = 4001
db._ensure_player(U1, "u1")
db.set_model_pref(U1, "u1", "fast", "gptoss")
allowed, status = db.try_consume_message(
    U1, "u1", DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST, force_premium=True
)
check("force_premium bills premium bucket even with model_pref=fast", allowed and status["consumed"]["bucket"] == "premium_quota")
check("fast_quota (used_today) untouched by a force_premium call", status["used_today"] == 0)
check("premium_used_today incremented", status["premium_used_today"] == 1)

# Exhaust the daily premium allotment, then confirm fallback to bonus_credits at PREMIUM_CREDIT_COST.
db.admin_add_bonus_credits(U1, 100)
count = 1  # already spent 1 above
while True:
    allowed, status = db.try_consume_message(
        U1, "u1", DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST, force_premium=True
    )
    if status["consumed"]["bucket"] != "premium_quota":
        break
    count += 1
check(f"exactly DAILY_FREE_PREMIUM_MESSAGES={DAILY_FREE_PREMIUM_MESSAGES} premium slots used (got {count})", count == DAILY_FREE_PREMIUM_MESSAGES)
check("first bonus-funded image costs PREMIUM_CREDIT_COST", status["consumed"]["bucket"] == "bonus_credits" and status["consumed"]["amount"] == PREMIUM_CREDIT_COST)

# Denial with 0 bonus credits after the daily allotment is gone.
U2 = 4002
db._ensure_player(U2, "u2")
count = 0
while True:
    allowed, status2 = db.try_consume_message(
        U2, "u2", DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST, force_premium=True
    )
    if not allowed:
        break
    count += 1
check(f"denial after {DAILY_FREE_PREMIUM_MESSAGES} slots with 0 bonus credits (got {count})", count == DAILY_FREE_PREMIUM_MESSAGES)
check("denial has limit_source=free", status2["limit_source"] == "free")

# refund_consumed_message reverses a force_premium spend correctly.
U3 = 4003
db._ensure_player(U3, "u3")
allowed, status3 = db.try_consume_message(
    U3, "u3", DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST, force_premium=True
)
before = db.get_status(U3, "u3")["premium_used_today"]
db.refund_consumed_message(U3, status3["consumed"])
after = db.get_status(U3, "u3")["premium_used_today"]
check(f"refund reverses the force_premium spend (before={before}, after={after})", before == 1 and after == 0)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
