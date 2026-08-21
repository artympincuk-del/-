import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "image_intent_payers_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import ai, db  # noqa: E402
from bot import handlers  # noqa: E402
from bot.config import DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES  # noqa: E402

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


class FakeChat:
    type = "private"

    def __init__(self, chat_id):
        self.id = chat_id


class FakeBot:
    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        pass


class FakeMessage:
    def __init__(self, uid, text=None, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.chat = FakeChat(uid)
        self.bot = FakeBot()
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    async def answer_photo(self, photo, **kwargs):
        self.sent.append(("[photo]", kwargs))
        return self

    async def delete(self):
        pass

    async def edit_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


class FakeCallbackMessage(FakeMessage):
    def __init__(self, uid):
        super().__init__(uid)
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, uid, data, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.data = data
        self.message = FakeCallbackMessage(uid)
        self.bot = FakeBot()
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.data = {}
        self.state = None


def buttons(markup):
    if markup is None or not getattr(markup, "inline_keyboard", None):
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def run():
    # ==================================================================
    # Правка 1: image intent recognised in plain text
    # ==================================================================
    check("intent: 'нарисуй кота в шляпе' -> 'кота в шляпе'", handlers.extract_image_intent("нарисуй кота в шляпе") == "кота в шляпе")
    check("intent: 'Нарисуй кота' is case-insensitive", handlers.extract_image_intent("Нарисуй кота") == "кота")
    check("intent: 'сгенерируй девушку'", handlers.extract_image_intent("сгенерируй девушку") == "девушку")
    check("intent: filler noun after the verb is dropped", handlers.extract_image_intent("сгенерируй картинку кота") == "кота")
    check("intent: 'сделай картинку заката'", handlers.extract_image_intent("сделай картинку заката") == "заката")
    check("intent: 'сделай фото робота'", handlers.extract_image_intent("сделай фото робота") == "робота")
    check("intent: 'сделай изображение дома'", handlers.extract_image_intent("сделай изображение дома") == "дома")
    check("intent: 'покажи картинку моря'", handlers.extract_image_intent("покажи картинку моря") == "моря")
    check("intent: 'изобрази дракона'", handlers.extract_image_intent("изобрази дракона") == "дракона")
    check("intent: polite -те form works", handlers.extract_image_intent("нарисуйте кота") == "кота")
    check("intent: colon after the verb is stripped", handlers.extract_image_intent("нарисуй: кота") == "кота")
    check("intent: 'нарисуй мне кота' drops the filler pronoun", handlers.extract_image_intent("нарисуй мне кота") == "кота")
    check("intent: 'нарисуй, пожалуйста, кота'", handlers.extract_image_intent("нарисуй, пожалуйста, кота") == "кота")

    # ...and NOT recognised where the verb is merely mentioned mid-sentence.
    not_intent = [
        "объясни, как художники рисуют перспективу",
        "что такое перспектива в рисунке",
        "как нарисовать куб карандашом",
        "сколько будет 2+2",
        "расскажи, зачем нарисуй используется в боте",
        "покажи решение задачи",       # «покажи» без «картинку» — не генерация
        "сделай домашку по алгебре",   # «сделай» без «картинку/фото» — не генерация
    ]
    for text in not_intent:
        check(f"intent: NOT a generation request — {text!r}", handlers.extract_image_intent(text) is None)

    check("intent: a bare command with no subject is not a request", handlers.extract_image_intent("нарисуй") is None)

    # A recognised request offers a button, spends nothing, calls no model.
    groq_calls = []

    async def fake_groq_create(**kwargs):
        # Recorded only to prove it's never called on the intent path — a
        # recognised "draw me X" must not reach the text model at all.
        groq_calls.append(kwargs)
        raise AssertionError("text model must not be called for an image-intent message")

    ai._client.chat.completions.create = fake_groq_create

    u1 = 95001
    db._ensure_player(u1, "drawer")
    state1 = FakeState()
    msg1 = FakeMessage(u1, "нарисуй кота в шляпе")
    before_msgs = db.get_status(u1, "drawer")["used_today"]
    before_imgs = db.get_status(u1, "drawer")["images_used_today"]
    await handlers._process_text_query(msg1, state1, "нарисуй кота в шляпе")

    check("text intent: replied once", len(msg1.sent) == 1)
    check("text intent: offers the generate button", "img:gen" in buttons(msg1.sent[0][1].get("reply_markup")))
    check("text intent: did NOT call the text model", groq_calls == [])
    check("text intent: did NOT spend a message quota unit", db.get_status(u1, "drawer")["used_today"] == before_msgs)
    check("text intent: did NOT spend an image quota unit", db.get_status(u1, "drawer")["images_used_today"] == before_imgs)
    check("text intent: the prompt stored is the subject only", state1.data.get("pending_image_prompt") == "кота в шляпе")
    check("text intent: the offer text shows the prompt back", "кота в шляпе" in msg1.sent[0][0])

    # Tapping the button really generates, and spends the image quota.
    generated = []

    async def fake_generate_image(prompt, **kwargs):
        generated.append(prompt)
        return b"\xff\xd8\xff-fake"

    original_generate = ai.generate_image
    ai.generate_image = fake_generate_image

    cb1 = FakeCallback(u1, "img:gen")
    await handlers.cb_image_generate_offer(cb1, state1)
    check("button tap: generation actually ran", len(generated) == 1)
    check("button tap: the prompt sent is the subject only", generated[0] == "кота в шляпе")
    check("button tap: image quota WAS spent", db.get_status(u1, "drawer")["images_used_today"] == before_imgs + 1)
    check("button tap: the one-shot offer is cleared", not state1.data.get("pending_image_prompt"))

    # Tapping again after it's been used says so instead of regenerating.
    generated.clear()
    cb_again = FakeCallback(u1, "img:gen")
    await handlers.cb_image_generate_offer(cb_again, state1)
    check("stale button: does not generate again", generated == [])
    check("stale button: tells the user the offer is gone", any("неактуально" in t for t, _ in cb_again.message.sent))
    ai.generate_image = original_generate

    # Правка 1.6: writing something else retires the pending offer.
    u2 = 95002
    db._ensure_player(u2, "ignorer")
    state2 = FakeState()
    await handlers._process_text_query(FakeMessage(u2, "нарисуй кота"), state2, "нарисуй кота")
    check("one-shot: offer stored", state2.data.get("pending_image_prompt") == "кота")

    async def fake_answer_text_query(*args, **kwargs):
        return None

    original_answer = handlers._answer_text_query
    handlers._answer_text_query = fake_answer_text_query
    await handlers._process_text_query(FakeMessage(u2, "а сколько будет 2+2"), state2, "а сколько будет 2+2")
    handlers._answer_text_query = original_answer
    check("one-shot: writing something else clears the pending offer", not state2.data.get("pending_image_prompt"))

    # Правка 1.5: a blocked prompt gets the filter refusal and NO button.
    u3 = 95003
    db._ensure_player(u3, "blocked")
    state3 = FakeState()
    msg3 = FakeMessage(u3, "нарисуй голую девушку")
    await handlers._process_text_query(msg3, state3, "нарисуй голую девушку")
    check("blocked intent: replied once", len(msg3.sent) == 1)
    check("blocked intent: NO generate button offered", "img:gen" not in buttons(msg3.sent[0][1].get("reply_markup")))
    check("blocked intent: shows the ordinary filter refusal", msg3.sent[0][0] == ai.IMAGE_BLOCKED_USER_MESSAGE)
    check("blocked intent: nothing stored to generate later", not state3.data.get("pending_image_prompt"))
    check("blocked intent: image quota untouched", db.get_status(u3, "blocked")["images_used_today"] == 0)

    # ==================================================================
    # Правка 2: /payers
    # ==================================================================
    with db._lock:
        db._conn.execute("DELETE FROM payments")
        db._conn.commit()

    # Three payers: one big repeat customer, one small repeat, one single.
    db._ensure_player(96001, "whale")
    db._ensure_player(96002, "repeat")
    db._ensure_player(96003, "once")
    db.record_payment_and_credit(96001, "whale", "messages", 100, "c1", 100)
    db.record_payment_and_credit(96001, "whale", "messages", 75, "c2", 75)
    db.record_payment_and_credit(96002, "repeat", "messages", 25, "c3", 25)
    db.record_payment_and_credit(96002, "repeat", "messages", 25, "c4", 25)
    db.record_payment_and_credit(96003, "once", "messages", 25, "c5", 25)
    # ...plus a refunded payment that must NOT count toward any total.
    db.record_payment_and_credit(96003, "once", "messages", 100, "c6", 100)
    db.refund_payment("c6")

    summary = db.get_payers_summary()
    check("payers summary: 3 distinct payers", summary["payers"] == 3)
    check(
        "payers summary: revenue excludes the refunded payment (100+75+25+25+25=250)",
        summary["revenue_stars"] == 250,
    )
    check("payers summary: 2 of them bought more than once", summary["repeat_payers"] == 2)
    check("payers summary: count_payers agrees", db.count_payers() == 3)

    payers = db.list_payers(10, 0)
    check("payers list: sorted by total spend, biggest first", [p["total_stars"] for p in payers] == [175, 50, 25])
    check("payers list: top payer is the whale", payers[0]["username"] == "whale")
    check("payers list: repeat customer's payment count is right", payers[1]["payments_count"] == 2)
    refunded_row = next(p for p in payers if p["username"] == "once")
    check("payers list: the refunded payment is excluded from that user's total", refunded_row["total_stars"] == 25)
    check("payers list: but the refund is flagged", refunded_row["has_refunds"] is True)
    check("payers list: a clean payer is not flagged", payers[0]["has_refunds"] is False)
    check("payers list: last payment date present", all(p["last_paid_at"] for p in payers))

    # Pagination.
    page0 = db.list_payers(2, 0)
    page1 = db.list_payers(2, 2)
    check("payers pagination: page 1 has 2 rows", len(page0) == 2)
    check("payers pagination: page 2 has the remaining 1", len(page1) == 1)
    check("payers pagination: no overlap between pages", page0[0]["user_id"] != page1[0]["user_id"])

    # Non-admin can't use either entry point.
    msg_intruder = FakeMessage(1, "/payers")
    await handlers.cmd_payers(msg_intruder)
    check("/payers: non-admin gets no reply", msg_intruder.sent == [])

    cb_intruder = FakeCallback(1, "admin:payers:0")
    await handlers.cb_admin_payers(cb_intruder)
    check("admin:payers: non-admin gets no screen", cb_intruder.message.edits == [])

    # Admin sees the list, header numbers included.
    msg_admin = FakeMessage(9999, "/payers")
    await handlers.cmd_payers(msg_admin)
    text = msg_admin.sent[0][0]
    check("/payers: header shows the payer count", "Всего платящих: <b>3</b>" in text)
    check("/payers: header shows total revenue", "250⭐" in text)
    check("/payers: header shows repeat buyers", "больше одного раза: <b>2</b>" in text)
    check("/payers: says the total excludes refunds", "без учёта возвратов" in text)
    check("/payers: shows a username", "@whale" in text)
    check("/payers: flags the user who had a refund", "были возвраты" in text)
    payer_buttons = buttons(msg_admin.sent[0][1].get("reply_markup"))
    check("/payers: tapping a payer opens the existing user card", "admin:user:96001:0" in payer_buttons)

    cb_page2 = FakeCallback(9999, "admin:payers:1")
    await handlers.cb_admin_payers(cb_page2)
    check("admin:payers: pagination callback renders a page", len(cb_page2.message.edits) == 1)
    check("admin:payers: page 2 header says so", "стр. 2" in cb_page2.message.edits[0][0])

    check("admin menu: has the 'Платившие' button", "admin:payers:0" in buttons(handlers.admin_menu_keyboard()))

    # ==================================================================
    # Правка 3: user screens show remaining, admin screens show spend
    # ==================================================================
    u_fmt = 97001
    db._ensure_player(u_fmt, "formatter")
    balance = handlers._balance_text(u_fmt, "formatter")
    check("balance (user): shows remaining", "осталось" in balance)
    check("balance (user): no bare N/M fraction", f"0/{DAILY_FREE_MESSAGES}" not in balance)

    card = handlers._admin_user_text(db.get_player(u_fmt), [])
    check("admin card: says 'Использовано сегодня'", "Использовано сегодня:" in card)
    check("admin card: spells the fraction out as 'X из Y'", f"0 из {DAILY_FREE_MESSAGES}" in card)
    check("admin card: no bare N/M fraction", f"0/{DAILY_FREE_MESSAGES}" not in card)
    check("admin card: does NOT use the user-facing 'осталось' wording", "осталось" not in card)

    msg_users = FakeMessage(9999, "/users")
    await handlers.cmd_users(msg_users)
    users_text = msg_users.sent[0][0]
    check("/users: uses the admin 'использовано X из Y' wording", "использовано" in users_text)
    check("/users: no bare N/M fraction", f"/{DAILY_FREE_PREMIUM_MESSAGES}," not in users_text)

    # Every user-facing denial message reads as remaining, not as "N/M".
    trial_denial = handlers._trial_denied_text("gemini-3.5-flash-lite", 5, 5)
    check("trial denial (user): reads as remaining", "осталось 0 из 5" in trial_denial)

    sub_denial = handlers._sublimit_denied_text("gemini-3.5-flash-lite", 10, 10)
    check("sub-limit denial (user): reads as remaining", "осталось 0 из 10" in sub_denial)

    image_denial = handlers._image_quota_denied_text({"images_used_today": 5})
    check("image denial (user): reads as remaining", "осталось" in image_denial)

    status_free = {
        "limit_source": "free", "model_pref": "premium",
        "premium_used_today": DAILY_FREE_PREMIUM_MESSAGES, "used_today": DAILY_FREE_MESSAGES,
        "promo_bonus_stacks": 0, "subscription_until": None,
    }
    quota_denial = handlers.quota_denied_text(status_free)
    check("quota denial (user): reads as remaining", "осталось" in quota_denial)

    # Model menu already used the remaining wording — confirm it still does.
    menu_status = db.get_status(u_fmt, "formatter")
    kb = handlers.model_keyboard(menu_status, u_fmt)
    gemini_btn = next(
        b.text for row in kb.inline_keyboard for b in row if b.callback_data == "model:premium:gemini"
    )
    check("model menu (user): reads as remaining", "осталось" in gemini_btn)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
