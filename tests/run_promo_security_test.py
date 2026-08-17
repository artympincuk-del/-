import asyncio
import datetime
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "promo_security_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for _ext in ("", "-wal", "-shm"):
    _p = os.environ["DB_PATH"] + _ext
    if os.path.exists(_p):
        os.remove(_p)

from bot import db  # noqa: E402
from bot.config import (  # noqa: E402
    PROMO_BONUS_DAILY_MESSAGES,
    PROMO_BONUS_DAILY_PREMIUM_MESSAGES,
    SUBSCRIPTION_DAILY_MESSAGES,
)
from bot.handlers import (  # noqa: E402
    HELP_TEXT,
    _apply_promo,
    _resolve_target,
    cmd_promo,
    cmd_promo_owner,
    cmd_promo_token,
)
from bot.main import BOT_COMMANDS  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeUser:
    def __init__(self, id, username=None):
        self.id = id
        self.username = username


class FakeMessage:
    def __init__(self, text, user_id, username=None):
        self.text = text
        self.from_user = FakeUser(user_id, username)
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


def make_future(days=10):
    return (datetime.datetime.utcnow() + datetime.timedelta(days=days)).isoformat(timespec="seconds")


def make_past(days=10):
    return (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat(timespec="seconds")


# --- Setup: one promo code ---
token = db.create_promo_code("tt1", "Test Partner", 3 * 1440, 40, 30)
check("create_promo_code returns a token", bool(token) and len(token) == 8)

ADMIN_ID = 9999
OWNER_ID = 8001
STRANGER_ID = 8002


async def run():
    # --- 1. /promo with correct word claims, wrong word doesn't, no word doesn't ---
    msg_no_word = FakeMessage("/promo tt1", OWNER_ID)
    await cmd_promo(msg_no_word)
    check("/promo with no word: usage message, no claim", "Использование" in msg_no_word.sent[0])
    check("/promo with no word: code stays unclaimed", db.get_promo_code("tt1")["owner_user_id"] is None)

    msg_wrong_word = FakeMessage("/promo tt1 wrongword", OWNER_ID)
    await cmd_promo(msg_wrong_word)
    check("/promo with wrong word: rejected", "неверные" in msg_wrong_word.sent[0])
    check("/promo with wrong word: code stays unclaimed", db.get_promo_code("tt1")["owner_user_id"] is None)

    msg_right_word = FakeMessage(f"/promo tt1 {token}", OWNER_ID)
    await cmd_promo(msg_right_word)
    check("/promo with correct word: claimed", db.get_promo_code("tt1")["owner_user_id"] == OWNER_ID)

    # --- 2. Occupied code isn't handed to someone else, even with the right word ---
    msg_steal = FakeMessage(f"/promo tt1 {token}", STRANGER_ID)
    await cmd_promo(msg_steal)
    check("occupied code rejects a different claimant with the right word", "уже привязан" in msg_steal.sent[0])
    check("owner is unchanged after the steal attempt", db.get_promo_code("tt1")["owner_user_id"] == OWNER_ID)

    # Same owner re-running /promo is fine (idempotent)
    msg_reclaim = FakeMessage(f"/promo tt1 {token}", OWNER_ID)
    await cmd_promo(msg_reclaim)
    check("same owner re-claiming succeeds", "теперь привязан" in msg_reclaim.sent[0])

    # --- 3. /promo_owner reassigns; /promo_owner <code> 0 clears ---
    msg_owner_set = FakeMessage(f"/promo_owner tt1 {STRANGER_ID}", ADMIN_ID)
    await cmd_promo_owner(msg_owner_set)
    check("/promo_owner reassigns owner", db.get_promo_code("tt1")["owner_user_id"] == STRANGER_ID)

    msg_owner_clear = FakeMessage("/promo_owner tt1 0", ADMIN_ID)
    await cmd_promo_owner(msg_owner_clear)
    check("/promo_owner <code> 0 clears the owner", db.get_promo_code("tt1")["owner_user_id"] is None)

    # --- 4. /promo_token <code> new invalidates the old word ---
    old_token = db.get_promo_claim_token("tt1")
    msg_token_new = FakeMessage("/promo_token tt1 new", ADMIN_ID)
    await cmd_promo_token(msg_token_new)
    new_token = db.get_promo_claim_token("tt1")
    check("regenerated token differs from the old one", new_token != old_token)

    msg_old_token_fails = FakeMessage(f"/promo tt1 {old_token}", OWNER_ID)
    await cmd_promo(msg_old_token_fails)
    check("old (regenerated-away) token no longer works", "неверные" in msg_old_token_fails.sent[0])

    msg_new_token_works = FakeMessage(f"/promo tt1 {new_token}", OWNER_ID)
    await cmd_promo(msg_new_token_works)
    check("new token works", db.get_promo_code("tt1")["owner_user_id"] == OWNER_ID)

    # --- 5. Backfill gave a token to a code created before this migration ---
    # Simulate a pre-migration row by blanking claim_token directly, then
    # re-running the same backfill logic the module runs at import time.
    with db._lock:
        db._conn.execute("UPDATE promo_codes SET claim_token = '' WHERE code = 'tt1'")
        db._conn.commit()
    check("claim_token blanked for backfill simulation", db.get_promo_claim_token("tt1") == "")
    rows = db._conn.execute(
        "SELECT code FROM promo_codes WHERE claim_token IS NULL OR claim_token = ''"
    ).fetchall()
    for (code,) in rows:
        with db._lock:
            db._conn.execute(
                "UPDATE promo_codes SET claim_token = ? WHERE code = ?",
                (db._generate_claim_token(), code),
            )
            db._conn.commit()
    check("backfill re-populates a blank claim_token", bool(db.get_promo_claim_token("tt1")))

    # --- 6. Promo visit grants the bonus; consumed is no longer None, spend hits the daily counter ---
    U_BONUS = 5101
    db.create_promo_code("tt2", "Bonus Partner", 3 * 1440, 40, 30)
    msg_start = FakeMessage("/start promo_tt2", U_BONUS)
    await _apply_promo(msg_start)
    check("promo visit sends a bonus message", len(msg_start.sent) == 1)
    status = db.get_status(U_BONUS, "u")
    check("promo_bonus_until is set", status["promo_bonus_until"] is not None)

    allowed, consume_status = db.try_consume_message(U_BONUS, "u", 10, 3, 3)
    check("promo bonus message is allowed", allowed is True)
    check("consumed is NOT None on a promo bonus (daily counter, not true unlimited)", consume_status["consumed"] is not None)
    check("limit_source is 'promo'", consume_status["limit_source"] == "promo")
    check("consumed bucket is fast_quota (same counter as free tier)", consume_status["consumed"]["bucket"] == "fast_quota")

    # --- 7. After PROMO_BONUS_DAILY_MESSAGES requests in a day, denial (not infinite access) ---
    U_CAP = 5102
    db.record_promo_visit(U_CAP, "tt2")
    db.activate_promo_bonus(U_CAP, "u", 3 * 1440)
    count = 0
    last_allowed = True
    while last_allowed:
        last_allowed, last_status = db.try_consume_message(U_CAP, "u", 10, 3, 3)
        if last_allowed:
            count += 1
    check(
        f"promo bonus caps at PROMO_BONUS_DAILY_MESSAGES={PROMO_BONUS_DAILY_MESSAGES} (got {count})",
        count == PROMO_BONUS_DAILY_MESSAGES,
    )
    check("denial after cap has limit_source=promo", last_status["limit_source"] == "promo")

    # --- 8. Purchased hourly unlimited pass still doesn't spend the counter ---
    U_UNLIMITED = 5103
    db._ensure_player(U_UNLIMITED, "u")
    db.activate_unlimited(U_UNLIMITED, "u", 60)
    allowed, unlimited_status = db.try_consume_message(U_UNLIMITED, "u", 10, 3, 3)
    check("purchased unlimited pass: allowed", allowed is True)
    check("purchased unlimited pass: consumed is None (still a true unlimited)", unlimited_status["consumed"] is None)

    # --- 9. Existing subscriber who then clicks a promo link gets the LARGER
    # of the two caps, not the sum: promo_bonus_stacks == 0 ---
    U_SUB_FIRST = 5104
    db._ensure_player(U_SUB_FIRST, "u")
    with db._lock:
        db._conn.execute(
            "UPDATE players SET subscription_until = ?, subscription_status = 'active' WHERE user_id = ?",
            (make_future(), U_SUB_FIRST),
        )
        db._conn.commit()
    db.record_promo_visit(U_SUB_FIRST, "tt2")
    db.activate_promo_bonus(U_SUB_FIRST, "u", 3 * 1440)
    status_sub_first = db.get_status(U_SUB_FIRST, "u")
    check("subscriber-then-promo: promo_bonus_stacks == 0", status_sub_first["promo_bonus_stacks"] is False)
    fast_cap, _ = db.promo_effective_limits(True, False, True)
    check(
        f"subscriber-then-promo: effective cap is max(), not sum (got {fast_cap})",
        fast_cap == max(PROMO_BONUS_DAILY_MESSAGES, SUBSCRIPTION_DAILY_MESSAGES),
    )
    count = 0
    last_allowed = True
    while last_allowed:
        last_allowed, last_status = db.try_consume_message(U_SUB_FIRST, "u", 10, 3, 3)
        if last_allowed:
            count += 1
    check(f"subscriber-then-promo: actually capped at max() = {fast_cap} (got {count})", count == fast_cap)

    # --- 10. No subscription at promo join, subscribes DURING the bonus window: limits stack ---
    U_PROMO_FIRST = 5105
    db._ensure_player(U_PROMO_FIRST, "u")
    db.record_promo_visit(U_PROMO_FIRST, "tt2")
    db.activate_promo_bonus(U_PROMO_FIRST, "u", 3 * 1440)
    status_promo_first = db.get_status(U_PROMO_FIRST, "u")
    check("promo-then-subscriber: promo_bonus_stacks == 1", status_promo_first["promo_bonus_stacks"] is True)
    # Now they subscribe, still within the bonus window.
    with db._lock:
        db._conn.execute(
            "UPDATE players SET subscription_until = ?, subscription_status = 'active' WHERE user_id = ?",
            (make_future(), U_PROMO_FIRST),
        )
        db._conn.commit()
    fast_cap_summed, _ = db.promo_effective_limits(True, True, True)
    check(
        f"promo-then-subscriber: effective cap is the SUM (got {fast_cap_summed})",
        fast_cap_summed == PROMO_BONUS_DAILY_MESSAGES + SUBSCRIPTION_DAILY_MESSAGES,
    )
    count = 0
    last_allowed = True
    while last_allowed:
        last_allowed, last_status = db.try_consume_message(U_PROMO_FIRST, "u", 10, 3, 3)
        if last_allowed:
            count += 1
    check(
        f"promo-then-subscriber: actually capped at the summed limit = {fast_cap_summed} (got {count})",
        count == fast_cap_summed,
    )

    # --- 11. In both cases, one message spends the counter exactly once ---
    U_ONCE = 5106
    db._ensure_player(U_ONCE, "u")
    db.record_promo_visit(U_ONCE, "tt2")
    db.activate_promo_bonus(U_ONCE, "u", 3 * 1440)
    before = db.get_status(U_ONCE, "u")["used_today"]
    db.try_consume_message(U_ONCE, "u", 10, 3, 3)
    after = db.get_status(U_ONCE, "u")["used_today"]
    check(f"one message increments used_today by exactly 1 (before={before}, after={after})", after == before + 1)

    # --- 12. Subscription stays visible in status even while a bonus is active ---
    check(
        "status shows subscription_until even when promo bonus is also active",
        status_sub_first["subscription_until"] is not None and status_sub_first["promo_bonus_until"] is not None,
    )

    # --- 13. After promo_bonus_until expires, falls back to the free tier; stacks flag has no effect ---
    U_EXPIRED = 5107
    db._ensure_player(U_EXPIRED, "u")
    db.record_promo_visit(U_EXPIRED, "tt2")
    db.activate_promo_bonus(U_EXPIRED, "u", 3 * 1440)
    with db._lock:
        db._conn.execute(
            "UPDATE players SET promo_bonus_until = ?, promo_bonus_stacks = 1 WHERE user_id = ?",
            (make_past(), U_EXPIRED),
        )
        db._conn.commit()
    allowed, expired_status = db.try_consume_message(U_EXPIRED, "u", 10, 3, 3)
    check("expired promo bonus: limit_source falls back to free", expired_status["limit_source"] == "free")
    check("expired promo bonus: promo_bonus_until reads as None (inactive)", expired_status["promo_bonus_until"] is None)

    # --- 14. Admin commands resolve @username, @USERNAME, and bare id identically ---
    TARGET_ID = 5200
    db._ensure_player(TARGET_ID, "MixedCaseUser")
    check("_resolve_target: numeric id", _resolve_target(str(TARGET_ID)) == TARGET_ID)
    check("_resolve_target: @lowercase", _resolve_target("@mixedcaseuser") == TARGET_ID)
    check("_resolve_target: @UPPERCASE", _resolve_target("@MIXEDCASEUSER") == TARGET_ID)
    check("_resolve_target: bare username, no @", _resolve_target("MixedCaseUser") == TARGET_ID)

    # --- 15. Unknown username gives a clear error, not silence ---
    msg_unknown_owner = FakeMessage("/promo_owner tt1 @doesnotexist123", ADMIN_ID)
    await cmd_promo_owner(msg_unknown_owner)
    check(
        "unknown username in /promo_owner gets an explanatory message, not silence",
        len(msg_unknown_owner.sent) == 1 and "не найден" in msg_unknown_owner.sent[0],
    )

    # --- 17. Help mentions the promo bonus and its daily limit ---
    check(
        "HELP_TEXT mentions промокод",
        "промокод" in HELP_TEXT.lower(),
    )
    check(
        f"HELP_TEXT includes the promo bonus daily limit numbers ({PROMO_BONUS_DAILY_MESSAGES}/{PROMO_BONUS_DAILY_PREMIUM_MESSAGES})",
        str(PROMO_BONUS_DAILY_MESSAGES) in HELP_TEXT and str(PROMO_BONUS_DAILY_PREMIUM_MESSAGES) in HELP_TEXT,
    )

    # --- 18. Public command list has no admin/partner commands ---
    public_commands = {c.command for c in BOT_COMMANDS}
    forbidden = {
        "promo", "mypromo", "promo_add", "promo_off", "promo_list", "promo_stat",
        "promo_owner", "promo_token", "grant", "users", "chatlog", "refund", "admin",
    }
    check(
        f"BOT_COMMANDS has no admin/partner commands (public={public_commands})",
        public_commands.isdisjoint(forbidden),
    )


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
