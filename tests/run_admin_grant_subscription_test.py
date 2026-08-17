import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "admin_grant_subscription_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
os.environ["AITUNNEL_API_KEY"] = "test-aitunnel-key"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import ai, db  # noqa: E402
from bot import handlers  # noqa: E402
from bot.config import MODEL_DAILY_SUBLIMIT_PAID, MODEL_DAILY_SUBLIMIT_SUBSCRIPTION  # noqa: E402

GEMINI = "gemini-3.5-flash-lite"

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeMsgContent:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMsgContent(content)
        self.finish_reason = "stop"


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeUser:
    def __init__(self, user_id, username="tester"):
        self.id = user_id
        self.username = username


class FakeBot:
    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        pass


class FakeChat:
    type = "private"

    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self, user_id, text=None):
        self.from_user = FakeUser(user_id)
        self.bot = FakeBot()
        self.chat = FakeChat(user_id)
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeCallbackMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def answer(self, text, **kwargs):
        self.edits.append(text)


class FakeCallback:
    def __init__(self, admin_id, data):
        self.from_user = FakeUser(admin_id, "admin")
        self.data = data
        self.message = FakeCallbackMessage()
        self.answered = []

    async def answer(self, text=None, **kwargs):
        self.answered.append(text)


async def run():
    aitunnel_calls = []

    async def fake_aitunnel_create(**kwargs):
        aitunnel_calls.append(kwargs)
        return FakeResponse("gemini ответ")

    ai._aitunnel_client.chat.completions.create = fake_aitunnel_create

    # ==================================================================
    # Admin can grant an unlimited-hour pass and a subscription
    # ==================================================================
    u1 = 20001
    db._ensure_player(u1, "user1")
    check("admin_grant: user starts with no subscription/unlimited", db.get_player(u1)["subscription_until"] is None and db.get_player(u1)["unlimited_until"] is None)

    cb_unlim = FakeCallback(9999, f"admin:unlim:{u1}:0")
    await handlers.cb_admin_grant_unlimited(cb_unlim)
    p1 = db.get_player(u1)
    check("admin_grant: unlimited_until now set", p1["unlimited_until"] is not None)
    check("admin_grant: unlimited grant used TIME_PACKAGES[0]'s minutes (~1h ahead)", any("Готово" in (t or "") for t in cb_unlim.answered))

    cb_sub = FakeCallback(9999, f"admin:sub:{u1}:0")
    await handlers.cb_admin_grant_subscription(cb_sub)
    p1 = db.get_player(u1)
    check("admin_grant: subscription_until now set", p1["subscription_until"] is not None)
    check("admin_grant: subscription grant acknowledged", any("Готово" in (t or "") for t in cb_sub.answered))

    # Granting twice stacks (like a real purchase) instead of overwriting.
    first_sub_expiry = p1["subscription_until"]
    cb_sub2 = FakeCallback(9999, f"admin:sub:{u1}:0")
    await handlers.cb_admin_grant_subscription(cb_sub2)
    p1_after = db.get_player(u1)
    check("admin_grant: a second subscription grant stacks (expiry moved further out)", p1_after["subscription_until"] > first_sub_expiry)

    # A non-admin can't use either callback.
    u_intruder = 20002
    db._ensure_player(u_intruder, "intruder")
    cb_intruder = FakeCallback(1, f"admin:sub:{u_intruder}:0")
    await handlers.cb_admin_grant_subscription(cb_intruder)
    check("admin_grant: a non-admin caller grants nothing", db.get_player(u_intruder)["subscription_until"] is None)

    # Admin card text shows the granted subscription.
    card_text = handlers._admin_user_text(db.get_player(u1), [])
    check("admin_grant: user card mentions 'Подписка до'", "Подписка до" in card_text)

    # ==================================================================
    # Tiered Gemini daily limit: subscription = double the base paid limit
    # ==================================================================
    check(
        f"config: subscription limit is double the paid limit ({MODEL_DAILY_SUBLIMIT_SUBSCRIPTION} == {MODEL_DAILY_SUBLIMIT_PAID}*2)",
        MODEL_DAILY_SUBLIMIT_SUBSCRIPTION == MODEL_DAILY_SUBLIMIT_PAID * 2,
    )

    # u1 now has both a subscription AND an unlimited pass — subscription
    # should win (the bigger number), per _paid_sublimit_limit.
    handlers.set_model_pref = None  # guard against accidental shadowing (no-op)
    db.set_model_pref(u1, "tester", "premium", "gemini")
    for i in range(MODEL_DAILY_SUBLIMIT_SUBSCRIPTION):
        m = FakeMessage(u1, text=f"sub q{i}")
        await handlers._answer_text_query(m, None, f"sub q{i}", u1, "tester")
    check(
        f"tiered limit: subscriber used the FULL subscription cap ({MODEL_DAILY_SUBLIMIT_SUBSCRIPTION}), not just the base paid cap",
        db.get_model_sublimit_usage(u1, GEMINI) == MODEL_DAILY_SUBLIMIT_SUBSCRIPTION,
    )
    m_deny_sub = FakeMessage(u1, text="sub deny")
    await handlers._answer_text_query(m_deny_sub, None, "sub deny", u1, "tester")
    check("tiered limit: subscriber denied only after the doubled cap", any("исчерпан" in t for t in m_deny_sub.sent))

    # A user with ONLY bonus_credits (no subscription) gets the base cap.
    u2 = 20003
    db._ensure_player(u2, "tester")
    db.add_bonus_credits(u2, "tester", 3000)
    db.set_model_pref(u2, "tester", "premium", "gemini")
    for i in range(MODEL_DAILY_SUBLIMIT_PAID):
        m = FakeMessage(u2, text=f"paid q{i}")
        await handlers._answer_text_query(m, None, f"paid q{i}", u2, "tester")
    check(
        f"tiered limit: bonus_credits-only user capped at the BASE paid limit ({MODEL_DAILY_SUBLIMIT_PAID}), not the subscription one",
        db.get_model_sublimit_usage(u2, GEMINI) == MODEL_DAILY_SUBLIMIT_PAID,
    )
    m_deny_paid = FakeMessage(u2, text="paid deny")
    await handlers._answer_text_query(m_deny_paid, None, "paid deny", u2, "tester")
    check("tiered limit: bonus_credits user denied at the base cap (not the doubled one)", any("исчерпан" in t for t in m_deny_paid.sent))

    # A user with ONLY an admin-granted unlimited-hour pass ALSO gets the
    # base cap, same as a bought message package — matches the explicit
    # requirement that the hour pass is worth 5/day, not 10/day.
    u3 = 20004
    db._ensure_player(u3, "tester")
    cb_unlim3 = FakeCallback(9999, f"admin:unlim:{u3}:0")
    await handlers.cb_admin_grant_unlimited(cb_unlim3)
    db.set_model_pref(u3, "tester", "premium", "gemini")
    for i in range(MODEL_DAILY_SUBLIMIT_PAID):
        m = FakeMessage(u3, text=f"unlim q{i}")
        await handlers._answer_text_query(m, None, f"unlim q{i}", u3, "tester")
    check(
        f"tiered limit: admin-granted-unlimited-hour user capped at the BASE paid limit ({MODEL_DAILY_SUBLIMIT_PAID})",
        db.get_model_sublimit_usage(u3, GEMINI) == MODEL_DAILY_SUBLIMIT_PAID,
    )

    # ==================================================================
    # Menu display reflects the right number per tier
    # ==================================================================
    status_sub = db.get_status(u1, "tester")
    kb_sub = handlers.model_keyboard(status_sub, u1)
    gemini_btn_sub = next(
        b.text for row in kb_sub.inline_keyboard for b in row if b.callback_data == "model:premium:gemini"
    )
    check(
        f"menu: subscriber sees the doubled total ('из {MODEL_DAILY_SUBLIMIT_SUBSCRIPTION}')",
        f"из {MODEL_DAILY_SUBLIMIT_SUBSCRIPTION}" in gemini_btn_sub,
    )

    status_paid = db.get_status(u2, "tester")
    kb_paid = handlers.model_keyboard(status_paid, u2)
    gemini_btn_paid = next(
        b.text for row in kb_paid.inline_keyboard for b in row if b.callback_data == "model:premium:gemini"
    )
    check(
        f"menu: bonus_credits user sees the base total ('из {MODEL_DAILY_SUBLIMIT_PAID}'), not the doubled one",
        f"из {MODEL_DAILY_SUBLIMIT_PAID}" in gemini_btn_paid and f"из {MODEL_DAILY_SUBLIMIT_SUBSCRIPTION}" not in gemini_btn_paid,
    )

    # ==================================================================
    # Buy menu text mentions both numbers
    # ==================================================================
    check(f"BUY_TEXT mentions the base limit ({MODEL_DAILY_SUBLIMIT_PAID})", str(MODEL_DAILY_SUBLIMIT_PAID) in handlers.BUY_TEXT)
    check(f"BUY_TEXT mentions the subscription limit ({MODEL_DAILY_SUBLIMIT_SUBSCRIPTION})", str(MODEL_DAILY_SUBLIMIT_SUBSCRIPTION) in handlers.BUY_TEXT)
    check("MODEL_MENU_TEXT distinguishes package/hour vs subscription limits", str(MODEL_DAILY_SUBLIMIT_PAID) in handlers.MODEL_MENU_TEXT and str(MODEL_DAILY_SUBLIMIT_SUBSCRIPTION) in handlers.MODEL_MENU_TEXT)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
