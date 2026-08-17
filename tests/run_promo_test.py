import datetime
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "promo_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import db  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- 1. Create a promo code, attach a user, verify unlimited is granted ---
# create_promo_code returns the claim token (a string) on success, or None
# on a duplicate code — NOT a bool (changed when claim-token ownership was
# added; see claim_promo_code below).
token1 = db.create_promo_code("tt1", "TikTok Partner", 3 * 1440, 40, 30)
assert token1 is not None
check("duplicate code creation rejected (returns None, not False)", db.create_promo_code("tt1", "dup", 60, 10, 5) is None)

U1 = 5001
db._ensure_player(U1, "user1")
assert db.record_promo_visit(U1, "tt1") is True
new_expiry = db.activate_unlimited(U1, "user1", 3 * 1440)
status = db.get_status(U1, "user1")
check("unlimited granted to first-time promo visitor", status["unlimited_until"] == new_expiry)

# --- 2. Repeat visit (same or different code) doesn't grant bonus again / doesn't change attribution ---
check("second visit to same code returns False", db.record_promo_visit(U1, "tt1") is False)
db.create_promo_code("tt2", "Other Partner", 60, 20, 10)
check("visit to a different code also returns False (first wins)", db.record_promo_visit(U1, "tt2") is False)
visit = db.get_promo_visit(U1)
check("attribution still points at the original code (tt1)", visit["code"] == "tt1")

# --- 3. Payment inside window gets promo_code set; payment after window doesn't ---
U2 = 5002
db._ensure_player(U2, "user2")
db.record_promo_visit(U2, "tt1")  # window_days=30 for tt1
outcome, _ = db.record_payment_and_credit(U2, "user2", "messages", 25, "charge_inside_window", 25)
check("payment inside window recorded", outcome == "credited")
payment = db.get_payment_by_charge_id("charge_inside_window")
check("payment inside window gets promo_code=tt1", payment["promo_code"] == "tt1")

U3 = 5003
db._ensure_player(U3, "user3")
db.record_promo_visit(U3, "tt1")
# Backdate this user's joined_at to 40 days ago (window_days=30) so the code's
# window has already lapsed for them specifically.
old_joined = (datetime.datetime.utcnow() - datetime.timedelta(days=40)).isoformat(timespec="seconds")
with db._lock:
    db._conn.execute("UPDATE promo_visits SET joined_at = ? WHERE user_id = ?", (old_joined, U3))
    db._conn.commit()
outcome, _ = db.record_payment_and_credit(U3, "user3", "messages", 25, "charge_outside_window", 25)
check("payment outside window still recorded", outcome == "credited")
payment2 = db.get_payment_by_charge_id("charge_outside_window")
check("payment outside window gets promo_code=None", payment2["promo_code"] is None)

# --- 4. Partner share calc matches manual calc on three payments ---
U4 = 5004
db._ensure_player(U4, "user4")
db.create_promo_code("tt3", "Share Test Partner", 60, 37, 30)  # 37% share
db.record_promo_visit(U4, "tt3")
amounts = [25, 75, 10]
for i, amt in enumerate(amounts):
    db.record_payment_and_credit(U4, "user4", "messages", amt, f"charge_share_{i}", amt)
stats = db.get_promo_stats("tt3")
manual_total = sum(amounts)
manual_share = manual_total * 37 // 100
check(
    f"revenue_stars matches manual sum (expected {manual_total}, got {stats['revenue_stars']})",
    stats["revenue_stars"] == manual_total,
)
check(
    f"partner_share_stars matches manual floor calc (expected {manual_share}, got {stats['partner_share_stars']})",
    stats["partner_share_stars"] == manual_share,
)
check("payments_count is 3", stats["payments_count"] == 3)

# --- 5. Ownership: partner claims via the secret token (claim_promo_code);
#        admin can force-(re)assign without one (admin_set_promo_owner).
#        set_promo_owner no longer exists — ownership is gated by the
#        per-code claim token now, not a bare "first claimant wins" call. ---
OWNER = 6001
NOT_OWNER = 6002
token_tt1 = db.get_promo_claim_token("tt1")
check("owner claim on unclaimed code with the correct token succeeds", db.claim_promo_code("tt1", token_tt1, OWNER) == "ok")
check(
    "a different user cannot claim an already-owned code, even with the correct token",
    db.claim_promo_code("tt1", token_tt1, NOT_OWNER) == "already_owned",
)
check("claiming with the wrong token is rejected as invalid", db.claim_promo_code("tt1", "wrongword", 6003) == "invalid")
check("claiming an unknown code is also just invalid (no code-existence leak)", db.claim_promo_code("doesnotexist", "anything", 6003) == "invalid")
promo_for_owner = db.get_promo_code_by_owner(OWNER)
promo_for_stranger = db.get_promo_code_by_owner(NOT_OWNER)
check("owner sees their own code via get_promo_code_by_owner", promo_for_owner is not None and promo_for_owner["code"] == "tt1")
check("non-owner (/mypromo from a different account) sees nothing", promo_for_stranger is None)

# Admin can force-reassign regardless of the claim token or current owner.
check("admin_set_promo_owner force-reassigns ownership", db.admin_set_promo_owner("tt1", NOT_OWNER) == "ok")
check("ownership actually moved to NOT_OWNER", db.get_promo_code_by_owner(NOT_OWNER)["code"] == "tt1")
check("the old owner no longer sees the code", db.get_promo_code_by_owner(OWNER) is None)
check("admin_set_promo_owner(None) clears ownership", db.admin_set_promo_owner("tt1", None) == "ok")
check("cleared code has no owner", db.get_promo_code_by_owner(NOT_OWNER) is None)
check("admin_set_promo_owner on an unknown code returns not_found", db.admin_set_promo_owner("doesnotexist", OWNER) == "not_found")

# --- 6. Disabled code: no bonus for new visitors, already-attributed payments still count ---
db.create_promo_code("tt4", "Disable Test", 60, 25, 30)
U5 = 5005
db._ensure_player(U5, "user5")
db.record_promo_visit(U5, "tt4")  # attributed while still active
disable_result = db.disable_promo_code("tt4")
check("disable_promo_code returns ok", disable_result == "ok")
check("disabling an already-off code returns already_off", db.disable_promo_code("tt4") == "already_off")
check("disabling an unknown code returns not_found", db.disable_promo_code("doesnotexist") == "not_found")

promo_tt4 = db.get_promo_code("tt4")
check("disabled code is inactive", promo_tt4["active"] is False)

U6 = 5006
db._ensure_player(U6, "user6")
new_visit = db.record_promo_visit(U6, "tt4")
check("record_promo_visit still mechanically succeeds (bonus-gating happens in handlers, not db)", new_visit is True)
# The bonus-gating (active check) lives in handlers._apply_promo, not db —
# simulate the handler's own check here:
promo_lookup = db.get_promo_code("tt4")
would_grant_bonus = promo_lookup is not None and promo_lookup["active"]
check("handler-level check would refuse a bonus for a disabled code", would_grant_bonus is False)

# Payment from the already-attributed U5 should still count toward tt4.
outcome, _ = db.record_payment_and_credit(U5, "user5", "messages", 25, "charge_after_disable", 25)
check("payment from a pre-disable visitor still credits", outcome == "credited")
payment3 = db.get_payment_by_charge_id("charge_after_disable")
check("payment from a pre-disable visitor still attributes to the (now disabled) code", payment3["promo_code"] == "tt4")

# --- Bonus 7: end-to-end _apply_promo() behavior (owner self-activation guard,
# real visitor gets the bonus message, unknown code is silent) ---
import asyncio  # noqa: E402

from bot.handlers import _apply_promo  # noqa: E402


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


async def run_apply_promo_tests():
    db.create_promo_code("selftest", "Self Test", 60, 10, 30)
    owner_id = 7001
    db.admin_set_promo_owner("selftest", owner_id)

    msg = FakeMessage("/start promo_selftest", owner_id)
    await _apply_promo(msg)
    check("owner clicking their own promo link gets no message", msg.sent == [])
    check("owner clicking their own link is not recorded as a visit", db.get_promo_visit(owner_id) is None)

    visitor_id = 7002
    msg2 = FakeMessage("/start promo_selftest", visitor_id)
    await _apply_promo(msg2)
    # The bonus grant text talks about the promo bonus and its expiry, not
    # "безлимит" (that word belonged to an older version of this feature,
    # before the promo bonus became a capped daily allowance rather than
    # genuinely unlimited access — see MODEL comment history / PROMO_BONUS_*).
    check(
        "a real visitor gets a bonus message mentioning the promo bonus and its expiry",
        len(msg2.sent) == 1 and "бонус по промокоду" in msg2.sent[0].lower() and "действует до" in msg2.sent[0].lower(),
    )
    visit = db.get_promo_visit(visitor_id)
    check("real visitor is recorded with the right code", visit is not None and visit["code"] == "selftest")

    unknown_id = 7003
    msg3 = FakeMessage("/start promo_doesnotexist", unknown_id)
    await _apply_promo(msg3)
    check("unknown promo code sends no message (silent)", msg3.sent == [])
    check("unknown promo code creates no visit row", db.get_promo_visit(unknown_id) is None)


asyncio.run(run_apply_promo_tests())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
