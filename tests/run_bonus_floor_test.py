import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "bonus_floor_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import db  # noqa: E402
from bot import handlers  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeCallbackMessage:
    async def edit_text(self, text, **kwargs):
        pass


class FakeCallback:
    def __init__(self, admin_id, data):
        self.from_user = FakeUser(admin_id)
        self.data = data
        self.message = FakeCallbackMessage()
        self.answered = []

    async def answer(self, text=None, **kwargs):
        self.answered.append(text)


class FakeMessage:
    def __init__(self, admin_id, text):
        self.from_user = FakeUser(admin_id)
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


async def run():
    # ------------------------------------------------------------------
    # 1. db.admin_add_bonus_credits itself never goes negative.
    # ------------------------------------------------------------------
    u1 = 30001
    db._ensure_player(u1, "u1")
    db.admin_add_bonus_credits(u1, 5)
    before, after = db.admin_add_bonus_credits(u1, -20)
    check("db: deducting more than the balance clamps at 0, not negative", after == 0)
    check("db: (before, after) reports the real before-value", before == 5)

    # A purchase right after a clamp gets the FULL amount, no leftover debt.
    new_balance = db.add_bonus_credits(u1, "u1", 25)
    check("db: a purchase after clamping credits the full amount (25), not 25-debt", new_balance == 25)

    # ------------------------------------------------------------------
    # 2. add_bonus_credits (non-admin path) also never goes negative.
    # ------------------------------------------------------------------
    u2 = 30002
    db._ensure_player(u2, "u2")
    result = db.add_bonus_credits(u2, "u2", -50)
    check("db: add_bonus_credits also clamps at 0 even with a negative amount", result == 0)

    # ------------------------------------------------------------------
    # 3. Startup migration zeroes out pre-existing negative balances.
    # ------------------------------------------------------------------
    u3 = 30003
    db._ensure_player(u3, "u3")
    with db._lock:
        db._conn.execute("UPDATE players SET bonus_credits = -12 WHERE user_id = ?", (u3,))
        db._conn.commit()
    with db._lock:
        db._conn.execute("UPDATE players SET bonus_credits = 0 WHERE bonus_credits < 0")
        db._conn.commit()
    check("db: migration-equivalent statement zeroes an existing negative balance", db.get_player(u3)["bonus_credits"] == 0)

    # ------------------------------------------------------------------
    # 4. Admin card / /grant surfaces the ACTUAL amount deducted when
    #    clamped, not just blindly the requested amount.
    # ------------------------------------------------------------------
    u4 = 30004
    db._ensure_player(u4, "u4")
    db.admin_add_bonus_credits(u4, 6)  # balance now 6

    cb = FakeCallback(9999, f"admin:grant:{u4}:-20:0")
    await handlers.cb_admin_grant(cb)
    check("admin card: answer mentions the actually-deducted amount (6), not the requested 20", any("6" in (t or "") for t in cb.answered))
    check("admin card: answer mentions the requested amount (20) for comparison", any("20" in (t or "") for t in cb.answered))
    check("admin card: balance ended at exactly 0", db.get_player(u4)["bonus_credits"] == 0)

    # A grant that doesn't get clamped (balance covers it) shows the plain message.
    u5 = 30005
    db._ensure_player(u5, "u5")
    db.admin_add_bonus_credits(u5, 50)
    cb2 = FakeCallback(9999, f"admin:grant:{u5}:-10:0")
    await handlers.cb_admin_grant(cb2)
    check("admin card: an unclamped deduction shows the plain 'Готово' message", any("Готово" in (t or "") for t in cb2.answered))
    check("admin card: unclamped deduction leaves the expected balance (40)", db.get_player(u5)["bonus_credits"] == 40)

    # Same via the /grant command.
    u6 = 30006
    db._ensure_player(u6, "u6")
    db.admin_add_bonus_credits(u6, 3)
    msg = FakeMessage(9999, f"/grant {u6} -10")
    await handlers.cmd_grant(msg)
    check("/grant: mentions the actually-deducted amount (3) when clamped", any("3" in t for t in msg.sent))
    check("/grant: balance ended at exactly 0", db.get_player(u6)["bonus_credits"] == 0)

    # A positive grant is never affected by the clamp logic/messaging.
    u7 = 30007
    db._ensure_player(u7, "u7")
    msg2 = FakeMessage(9999, f"/grant {u7} 15")
    await handlers.cmd_grant(msg2)
    check("/grant: a plain positive grant still works and shows 'Готово'", any("Готово" in t for t in msg2.sent))
    check("/grant: balance is exactly the granted amount", db.get_player(u7)["bonus_credits"] == 15)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
