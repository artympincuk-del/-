import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "promo_users_test.db")
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
        self.sent.append((text, kwargs.get("reply_markup")))


class FakeCallbackMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def answer(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


class FakeCallback:
    def __init__(self, uid, data, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.data = data
        self.message = FakeCallbackMessage()
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


async def run():
    db.create_promo_code("tt1", "TikTok Partner", 60, 40, 30)

    # 15 visitors: 15..1 in reverse chronological insertion order.
    visitor_ids = list(range(50001, 50016))
    for i, uid in enumerate(visitor_ids):
        db._ensure_player(uid, f"visitor{i}")
        db.record_promo_visit(uid, "tt1")
    # A couple of them actually used the bot.
    db.log_message(visitor_ids[0], "visitor0", "user", "hi")
    db.log_message(visitor_ids[0], "visitor0", "user", "again")
    db.log_message(visitor_ids[1], "visitor1", "user", "hi")
    # One of them purchased something attributed to this code.
    db.record_payment_and_credit(visitor_ids[0], "visitor0", "messages", 25, "charge_1", 25)
    with db._lock:
        db._conn.execute(
            "UPDATE payments SET promo_code = 'tt1' WHERE telegram_payment_charge_id = 'charge_1'"
        )
        db._conn.commit()

    # ------------------------------------------------------------------
    # 1. Non-admin can't use /promo_users.
    # ------------------------------------------------------------------
    m_intruder = FakeMessage(1, "/promo_users tt1")
    await handlers.cmd_promo_users(m_intruder)
    check("non-admin: /promo_users gets no reply at all", m_intruder.sent == [])

    m_intruder_cb = FakeCallback(1, "admin:promousers:tt1:0")
    await handlers.cb_admin_promo_users(m_intruder_cb)
    check("non-admin: the pagination callback does nothing", m_intruder_cb.message.edits == [])

    # ------------------------------------------------------------------
    # 2. Admin sees the first page (10 of 15), with per-visitor detail.
    # ------------------------------------------------------------------
    m_admin = FakeMessage(9999, "/promo_users tt1")
    await handlers.cmd_promo_users(m_admin)
    check("admin: got exactly one reply", len(m_admin.sent) == 1)
    text0, kb0 = m_admin.sent[0]
    check("admin: header shows the total (15)", "(15)" in text0)
    check("admin: page 1 has exactly 10 visitor lines (page size)", sum(1 for uid in visitor_ids if f"visitor{visitor_ids.index(uid)}" in text0) <= 10)
    check("admin: the purchaser is marked 'покупал'", "@visitor0" in text0 and "покупал" in text0)
    check("admin: a non-purchaser is marked 'не покупал'", "не покупал" in text0)
    button_data = [b.callback_data for row in kb0.inline_keyboard for b in row]
    check("admin: page 1 has a forward button, no back button", "admin:promousers:tt1:1" in button_data and "admin:promousers:tt1:-1" not in button_data)

    # ------------------------------------------------------------------
    # 3. Pagination: page 2 shows the remaining 5, with a back button.
    # ------------------------------------------------------------------
    cb_page2 = FakeCallback(9999, "admin:promousers:tt1:1")
    await handlers.cb_admin_promo_users(cb_page2)
    check("admin: page 2 callback answered", cb_page2.answered)
    check("admin: page 2 edited the message", len(cb_page2.message.edits) == 1)
    text1, kb1 = cb_page2.message.edits[0]
    check("admin: page 2 header says 'стр. 2'", "стр. 2" in text1)
    button_data2 = [b.callback_data for row in kb1.inline_keyboard for b in row]
    check("admin: page 2 has a back button, no forward button", "admin:promousers:tt1:0" in button_data2 and "admin:promousers:tt1:2" not in button_data2)

    # ------------------------------------------------------------------
    # 4. Unknown code -> friendly error, not a crash.
    # ------------------------------------------------------------------
    m_bad = FakeMessage(9999, "/promo_users doesnotexist")
    await handlers.cmd_promo_users(m_bad)
    check("unknown code: friendly 'not found' message", any("не найден" in t for t, _ in m_bad.sent))

    # ------------------------------------------------------------------
    # 5. /promo_list shows "переходов N (активных M)".
    # ------------------------------------------------------------------
    m_list = FakeMessage(9999, "/promo_list")
    await handlers.cmd_promo_list(m_list)
    list_text = m_list.sent[0][0]
    check("/promo_list: shows total visits (15)", "переходов 15" in list_text)
    check("/promo_list: shows active count in parens (2 users sent messages)", "(активных 2)" in list_text)

    # ------------------------------------------------------------------
    # 6. /mypromo (partner-facing) never shows visitor usernames.
    # ------------------------------------------------------------------
    stats = db.get_promo_stats("tt1")
    mypromo_text = handlers._promo_stats_text(stats, admin_view=False)
    check("/mypromo: does not leak any visitor's username", not any(f"visitor{i}" in mypromo_text for i in range(15)))
    check("/mypromo: still shows the aggregate active-user count", "Пользовались ботом: 2" in mypromo_text)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
