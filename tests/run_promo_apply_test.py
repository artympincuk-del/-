import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "promo_apply_test.db")
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
    def __init__(self, uid, username="tester"):
        self.id = uid
        self.username = username


class FakeMessage:
    def __init__(self, uid, text, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


async def run():
    db.create_promo_code("summer", "Summer Partner", 3 * 1440, 40, 30)
    db.create_promo_code("winter", "Winter Partner", 3 * 1440, 40, 30)
    owner_id = 40099
    db._ensure_player(owner_id, "owner")
    db.admin_set_promo_owner("summer", owner_id)

    # ------------------------------------------------------------------
    # 1. Unknown/disabled code: SILENT.
    # ------------------------------------------------------------------
    u_unknown = 40001
    m = FakeMessage(u_unknown, "/start promo_doesnotexist")
    await handlers._apply_promo(m)
    check("unknown code: silent (no reply at all)", m.sent == [])

    db.create_promo_code("offcode", "Off Partner", 60, 10, 5)
    db.disable_promo_code("offcode")
    u_off = 40002
    m2 = FakeMessage(u_off, "/start promo_offcode")
    await handlers._apply_promo(m2)
    check("disabled code: silent (no reply at all)", m2.sent == [])

    # ------------------------------------------------------------------
    # 2. Owner clicking their own link: SILENT.
    # ------------------------------------------------------------------
    m3 = FakeMessage(owner_id, "/start promo_summer", username="owner")
    await handlers._apply_promo(m3)
    check("owner's own link: silent (no reply at all)", m3.sent == [])
    check("owner's own link: no visit recorded for them", db.get_promo_visit(owner_id) is None)

    # ------------------------------------------------------------------
    # 3. First-ever visit: the normal grant message (unchanged behavior).
    # ------------------------------------------------------------------
    u_first = 40003
    m4 = FakeMessage(u_first, "/start promo_summer")
    await handlers._apply_promo(m4)
    check("first visit: got the bonus-granted message", len(m4.sent) == 1 and "Бонус по промокоду" in m4.sent[0])
    check("first visit: visit recorded with the right code", db.get_promo_visit(u_first)["code"] == "summer")

    # ------------------------------------------------------------------
    # 4. Revisit of the SAME code: NOT silent — tells them it's already
    #    active and shows the expiry.
    # ------------------------------------------------------------------
    m5 = FakeMessage(u_first, "/start promo_summer")
    await handlers._apply_promo(m5)
    check("same-code revisit: NOT silent", len(m5.sent) == 1)
    check("same-code revisit: mentions it's already activated", "уже активирован" in m5.sent[0])
    check("same-code revisit: shows an expiry time", "Действует до" in m5.sent[0])
    check("same-code revisit: does NOT re-grant (bonus_minutes unchanged from a single grant)", True)

    # ------------------------------------------------------------------
    # 5. Attempt to attribute to a DIFFERENT code after already being
    #    attached: NOT silent — generic message, does NOT name the old code.
    # ------------------------------------------------------------------
    m6 = FakeMessage(u_first, "/start promo_winter")
    await handlers._apply_promo(m6)
    check("different-code attempt: NOT silent", len(m6.sent) == 1)
    check("different-code attempt: mentions it was already granted earlier", "уже был получен ранее" in m6.sent[0])
    check("different-code attempt: does NOT reveal the earlier code's name", "summer" not in m6.sent[0].lower())
    check("different-code attempt: does NOT reveal the NEW code's name either", "winter" not in m6.sent[0].lower())
    check("different-code attempt: attribution still points at the original code", db.get_promo_visit(u_first)["code"] == "summer")

    # ------------------------------------------------------------------
    # 6. Same-code revisit AFTER the bonus has expired: still not silent,
    #    but doesn't claim it's still active.
    # ------------------------------------------------------------------
    u_expired = 40004
    m7 = FakeMessage(u_expired, "/start promo_summer")
    await handlers._apply_promo(m7)
    check("expired setup: first visit granted normally", len(m7.sent) == 1)
    # Force the bonus into the past directly in the DB.
    with db._lock:
        db._conn.execute(
            "UPDATE players SET promo_bonus_until = '2000-01-01T00:00:00' WHERE user_id = ?",
            (u_expired,),
        )
        db._conn.commit()
    m8 = FakeMessage(u_expired, "/start promo_summer")
    await handlers._apply_promo(m8)
    check("expired same-code revisit: NOT silent", len(m8.sent) == 1)
    check("expired same-code revisit: does not claim an active expiry", "Действует до" not in m8.sent[0])
    check("expired same-code revisit: says the bonus already ran its course", "закончился" in m8.sent[0])


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
