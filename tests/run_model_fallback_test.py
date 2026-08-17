import asyncio
import datetime
import io
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "model_fallback_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
os.environ["AITUNNEL_API_KEY"] = "test-aitunnel-key"
os.environ["MODEL_DAILY_SUBLIMIT_PAID"] = "10"
os.environ["MODEL_TRIAL_TOTAL_FREE"] = "2"
os.environ["FALLBACK_DAILY_CAP"] = "300"
os.environ["FALLBACK_USER_DAILY_CAP"] = "20"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

import httpx  # noqa: E402
import httpx2  # noqa: E402
import groq  # noqa: E402
import openai  # noqa: E402
from PIL import Image  # noqa: E402

from bot import ai, db  # noqa: E402
from bot import handlers  # noqa: E402
from bot.config import (  # noqa: E402
    DAILY_FREE_PREMIUM_MESSAGES,
    MODEL_COST_PER_REQUEST,
    MODEL_DAILY_SUBLIMIT_PAID,
    MODEL_TRIAL_TOTAL_FREE,
    FALLBACK_DAILY_CAP,
    FALLBACK_USER_DAILY_CAP,
)

GEMINI = "gemini-3.5-flash-lite"

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def make_groq_413(detail="too large"):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(413, request=request, json={"error": {"message": detail}})
    return groq.APIStatusError("413", response=response, body=response.json())


def make_groq_status(code, detail="error"):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(code, request=request, json={"error": {"message": detail}})
    return groq.APIStatusError(str(code), response=response, body=response.json())


def make_aitunnel_status(code, detail="error"):
    request = httpx2.Request("POST", "https://api.aitunnel.ru/v1/chat/completions")
    response = httpx2.Response(code, request=request, json={"error": {"message": detail}})
    return openai.APIStatusError(str(code), response=response, body=response.json())


class FakeMsgContent:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = FakeMsgContent(content)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


_UNSET = object()


class FakeUser:
    def __init__(self, user_id, username="tester"):
        self.id = user_id
        self.username = username


class FakeChat:
    type = "private"

    def __init__(self, chat_id):
        self.id = chat_id


_buf = io.BytesIO()
Image.new("RGB", (30, 30), color=(10, 20, 30)).save(_buf, format="JPEG")
FAKE_PHOTO_BYTES = _buf.getvalue()


class FakeDownload:
    def read(self):
        return FAKE_PHOTO_BYTES


class FakeBot:
    def __init__(self):
        self.admin_messages = []

    async def send_chat_action(self, chat_id, action):
        pass

    async def download(self, file_id):
        return FakeDownload()

    async def send_message(self, chat_id, text, **kwargs):
        self.admin_messages.append((chat_id, text))


class FakeSentMessage:
    def __init__(self, text):
        self.text = text

    async def edit_text(self, text, **kwargs):
        self.text = text

    async def delete(self):
        pass


class FakeMessage:
    def __init__(self, user_id, username="tester", text=None, caption=None, photo=False):
        self.from_user = FakeUser(user_id, username)
        self.chat = FakeChat(user_id)
        self.bot = FakeBot()
        self.text = text
        self.caption = caption
        self.photo = [type("P", (), {"file_id": "photo-1"})()] if photo else None
        self.sent = []

    async def answer(self, text, reply_markup=None, parse_mode=_UNSET):
        msg = FakeSentMessage(text)
        self.sent.append(text)
        return msg


def grant_paid(user_id, username="tester", amount=3000):
    """Makes _is_paid_tier True via bonus_credits, with enough headroom that
    the NORMAL daily quota is never the thing that blocks a test."""
    db._ensure_player(user_id, username)
    db.add_bonus_credits(user_id, username, amount)


def set_model(user_id, username, pref, choice):
    db.set_model_pref(user_id, username, pref, choice)


async def run():
    # ==================================================================
    # Sublimits
    # ==================================================================
    groq_calls = []
    aitunnel_calls = []

    async def fake_groq_create(**kwargs):
        groq_calls.append(kwargs)
        return FakeResponse("groq ответ")

    async def fake_aitunnel_create(**kwargs):
        aitunnel_calls.append(kwargs)
        return FakeResponse("gemini ответ")

    ai._client.chat.completions.create = fake_groq_create
    ai._aitunnel_client.chat.completions.create = fake_aitunnel_create

    # --- 1. A Gemini request consumes both the normal limit and the sub-limit, by one each. ---
    u1 = 10001
    grant_paid(u1)
    set_model(u1, "tester", "premium", "gemini")
    status_before = db.get_status(u1, "tester")
    aitunnel_calls.clear()
    msg = FakeMessage(u1, text="привет")
    await handlers._answer_text_query(msg, None, "привет", u1, "tester")
    status_after = db.get_status(u1, "tester")
    check("sublimit: one aitunnel call made", len(aitunnel_calls) == 1)
    check(
        "sublimit: normal premium counter incremented by 1",
        status_after["premium_used_today"] == status_before["premium_used_today"] + 1,
    )
    check(
        "sublimit: model sub-limit counter incremented by 1",
        db.get_model_sublimit_usage(u1, GEMINI) == 1,
    )

    # --- 2. On the 11th Gemini request, a paid user is denied by the sub-limit,
    #        but a GPT-OSS request from the SAME user goes through right after. ---
    u2 = 10002
    grant_paid(u2)
    set_model(u2, "tester", "premium", "gemini")
    for i in range(MODEL_DAILY_SUBLIMIT_PAID):  # exactly fills the paid cap (10)
        m = FakeMessage(u2, text=f"вопрос {i}")
        await handlers._answer_text_query(m, None, f"вопрос {i}", u2, "tester")
    check(f"sublimit: after {MODEL_DAILY_SUBLIMIT_PAID} gemini requests, usage == cap", db.get_model_sublimit_usage(u2, GEMINI) == MODEL_DAILY_SUBLIMIT_PAID)

    premium_used_before_11th = db.get_status(u2, "tester")["premium_used_today"]
    aitunnel_calls.clear()
    m11 = FakeMessage(u2, text="11-й вопрос")
    await handlers._answer_text_query(m11, None, "11-й вопрос", u2, "tester")
    check("sublimit: 11th gemini request got a denial, not an answer", any("исчерпан" in t for t in m11.sent))
    check("sublimit: 11th request did NOT reach aitunnel", aitunnel_calls == [])
    check(
        "sublimit: normal quota refunded after the sub-limit denial (unchanged)",
        db.get_status(u2, "tester")["premium_used_today"] == premium_used_before_11th,
    )

    set_model(u2, "tester", "premium", "gptoss")
    groq_calls.clear()
    m_gptoss = FakeMessage(u2, text="а теперь на другой модели")
    await handlers._answer_text_query(m_gptoss, None, "а теперь на другой модели", u2, "tester")
    check(
        "sublimit: same user's GPT-OSS request succeeds even with gemini sub-limit exhausted",
        len(groq_calls) >= 1 and not any("исчерпан" in t for t in m_gptoss.sent),
    )

    # --- 3. A free-tier user gets a lifetime TRIAL instead of a daily
    #        sub-limit (Правка 1) — exhausts it, gets denied, and it does
    #        NOT come back even on a fresh day (unlike the paid sub-limit
    #        above, the trial counter has no quota_date to roll over at all). ---
    u3 = 10003
    db._ensure_player(u3, "tester")  # no bonus credits, no subscription -> free tier
    set_model(u3, "tester", "premium", "gemini")
    for i in range(MODEL_TRIAL_TOTAL_FREE):
        m = FakeMessage(u3, text=f"free q{i}")
        await handlers._answer_text_query(m, None, f"free q{i}", u3, "tester")
    check(
        f"trial: free user used up their trial ({MODEL_TRIAL_TOTAL_FREE})",
        db.get_model_trial_usage(u3, GEMINI) == MODEL_TRIAL_TOTAL_FREE,
    )
    check(
        "trial: free user's normal premium quota still has headroom left (proves it's the trial denying, not the normal quota)",
        db.get_status(u3, "tester")["premium_used_today"] < DAILY_FREE_PREMIUM_MESSAGES,
    )
    check(
        "trial: the PAID daily sub-limit counter is untouched by a free-tier trial user",
        db.get_model_sublimit_usage(u3, GEMINI) == 0,
    )
    premium_used_before_deny = db.get_status(u3, "tester")["premium_used_today"]
    m_deny = FakeMessage(u3, text="free q next")
    await handlers._answer_text_query(m_deny, None, "free q next", u3, "tester")
    check("trial: free user denied on their next gemini request", any("пробн" in t.lower() for t in m_deny.sent))
    check(
        "trial: free user's normal quota unaffected by the denied attempt",
        db.get_status(u3, "tester")["premium_used_today"] == premium_used_before_deny,
    )

    # "The next day" — force db._today() to report tomorrow (same trick the
    # reset test below uses for the daily sub-limit) and confirm the trial
    # is STILL exhausted, proving it doesn't roll over the way a daily
    # counter would.
    original_today_fn = db._today
    tomorrow = (datetime.datetime.now(db._QUOTA_TZINFO).date() + datetime.timedelta(days=1)).isoformat()
    db._today = lambda: tomorrow
    m_next_day = FakeMessage(u3, text="free q, следующие сутки")
    await handlers._answer_text_query(m_next_day, None, "free q, следующие сутки", u3, "tester")
    db._today = original_today_fn
    check(
        "trial: still denied 'the next day' (mocked _today) — a trial never resets",
        any("пробн" in t.lower() for t in m_next_day.sent),
    )
    check(
        "trial: usage count unchanged by the extra denied attempts",
        db.get_model_trial_usage(u3, GEMINI) == MODEL_TRIAL_TOTAL_FREE,
    )

    # --- 3b. Buying a package switches a user onto the daily PAID sub-limit
    #         — an already-exhausted trial no longer blocks them, and isn't
    #         itself restored by the purchase. ---
    u3b = 10016
    db._ensure_player(u3b, "tester")
    set_model(u3b, "tester", "premium", "gemini")
    for i in range(MODEL_TRIAL_TOTAL_FREE):
        m = FakeMessage(u3b, text=f"pre-purchase q{i}")
        await handlers._answer_text_query(m, None, f"pre-purchase q{i}", u3b, "tester")
    m_deny_pre = FakeMessage(u3b, text="pre-purchase deny")
    await handlers._answer_text_query(m_deny_pre, None, "pre-purchase deny", u3b, "tester")
    check("purchase: denied before purchase (trial exhausted)", any("пробн" in t.lower() for t in m_deny_pre.sent))

    grant_paid(u3b)  # simulates buying a message package
    m_post_purchase = FakeMessage(u3b, text="после покупки")
    await handlers._answer_text_query(m_post_purchase, None, "после покупки", u3b, "tester")
    check(
        "purchase: the same request succeeds after buying, via the daily sub-limit, not the exhausted trial",
        not any("пробн" in t.lower() for t in m_post_purchase.sent)
        and not any("исчерпан" in t for t in m_post_purchase.sent),
    )
    check("purchase: daily paid sub-limit counter now shows usage (1)", db.get_model_sublimit_usage(u3b, GEMINI) == 1)
    check(
        "purchase: the trial counter is NOT restored by the purchase (still exactly what it was)",
        db.get_model_trial_usage(u3b, GEMINI) == MODEL_TRIAL_TOTAL_FREE,
    )

    # --- 3c. A failed request refunds a free user's TRIAL use, and never
    #         touches the cost counter (only a SUCCESS ever does). ---
    u3c = 10017
    db._ensure_player(u3c, "tester")
    set_model(u3c, "tester", "premium", "gemini")
    trial_before_fail = db.get_model_trial_usage(u3c, GEMINI)
    cost_before_fail = db.get_today_cost()

    async def fake_aitunnel_fail_free(**kwargs):
        raise make_aitunnel_status(400, "bad request")

    ai._aitunnel_client.chat.completions.create = fake_aitunnel_fail_free
    m_fail_free = FakeMessage(u3c, text="упадёт у бесплатного")
    await handlers._answer_text_query(m_fail_free, None, "упадёт у бесплатного", u3c, "tester")
    check("refund: a failed free-tier request did NOT consume a trial use", db.get_model_trial_usage(u3c, GEMINI) == trial_before_fail)
    check("refund: a failed request did NOT add anything to the cost counter", db.get_today_cost() == cost_before_fail)
    ai._aitunnel_client.chat.completions.create = fake_aitunnel_create

    # --- 4. A failed request (paid tier) refunds BOTH counters. ---
    u4 = 10004
    grant_paid(u4)
    set_model(u4, "tester", "premium", "gemini")
    status_before4 = db.get_status(u4, "tester")
    sublimit_before4 = db.get_model_sublimit_usage(u4, GEMINI)

    async def fake_aitunnel_fail(**kwargs):
        raise make_aitunnel_status(400, "bad request")

    ai._aitunnel_client.chat.completions.create = fake_aitunnel_fail
    m_fail = FakeMessage(u4, text="упадёт")
    await handlers._answer_text_query(m_fail, None, "упадёт", u4, "tester")
    status_after4 = db.get_status(u4, "tester")
    check("refund: failed request did NOT keep the normal quota consumed", status_after4["premium_used_today"] == status_before4["premium_used_today"])
    check("refund: failed request did NOT keep the sub-limit consumed", db.get_model_sublimit_usage(u4, GEMINI) == sublimit_before4)
    ai._aitunnel_client.chat.completions.create = fake_aitunnel_create

    # --- 5. Sub-limit counter resets at local midnight (QUOTA_TZ). ---
    u5 = 10005
    grant_paid(u5)
    ok, _ = db.try_consume_model_sublimit(u5, GEMINI, MODEL_DAILY_SUBLIMIT_PAID)
    check("reset: consumed once today", ok and db.get_model_sublimit_usage(u5, GEMINI) == 1)
    yesterday = (datetime.datetime.now(db._QUOTA_TZINFO).date() - datetime.timedelta(days=1)).isoformat()
    with db._lock:
        db._conn.execute(
            "UPDATE model_sublimit_usage SET quota_date = ? WHERE user_id = ? AND model = ?",
            (yesterday, u5, GEMINI),
        )
        db._conn.commit()
    check("reset: reading after rolling quota_date back shows 0 (lazy reset)", db.get_model_sublimit_usage(u5, GEMINI) == 0)
    ok2, sub_status2 = db.try_consume_model_sublimit(u5, GEMINI, MODEL_DAILY_SUBLIMIT_PAID)
    check("reset: a fresh consume after the day rolled over starts back at 1, not 2", ok2 and sub_status2["used_today"] == 1)

    # --- 6. Model menu shows the actual remaining count. ---
    u6 = 10006
    grant_paid(u6)
    for _ in range(3):
        db.try_consume_model_sublimit(u6, GEMINI, MODEL_DAILY_SUBLIMIT_PAID)
    status6 = db.get_status(u6, "tester")
    kb = handlers.model_keyboard(status6, u6)
    gemini_btn_text = next(
        btn.text for row in kb.inline_keyboard for btn in row if btn.callback_data == "model:premium:gemini"
    )
    expected_remaining = MODEL_DAILY_SUBLIMIT_PAID - 3
    check(
        f"menu: remaining count shown matches the real counter ({expected_remaining} of {MODEL_DAILY_SUBLIMIT_PAID})",
        f"осталось {expected_remaining} из {MODEL_DAILY_SUBLIMIT_PAID}" in gemini_btn_text,
    )

    # ==================================================================
    # Multimodality
    # ==================================================================
    # --- 7. Photo with a multimodal model selected -> one request, VISION_MODEL untouched. ---
    u7 = 10007
    grant_paid(u7)
    set_model(u7, "tester", "premium", "gemini")
    groq_calls.clear()
    aitunnel_calls.clear()
    photo_msg = FakeMessage(u7, caption="реши", photo=True)
    await handlers._handle_photo_message_locked(photo_msg, None)
    check("multimodal photo: exactly one aitunnel call (single request)", len(aitunnel_calls) == 1)
    check("multimodal photo: Groq was never touched (no vision stage)", groq_calls == [])
    check("multimodal photo: sub-limit consumed", db.get_model_sublimit_usage(u7, GEMINI) >= 1)
    check("multimodal photo: got an answer, not an error", not any("Не удалось" in t for t in photo_msg.sent))

    # --- 8. Photo with a non-multimodal model still goes through two stages. ---
    u8 = 10008
    grant_paid(u8)
    set_model(u8, "tester", "fast", "gptoss")
    groq_call_log = []

    async def fake_groq_two_stage(**kwargs):
        groq_call_log.append(kwargs)
        if len(groq_call_log) == 1:
            return FakeResponse("2x+3=7, найти x")  # vision stage
        return FakeResponse("x=2\n\n✅ Проверка: 2*2+3=7")  # solve stage

    ai._client.chat.completions.create = fake_groq_two_stage
    photo_msg2 = FakeMessage(u8, caption="реши", photo=True)
    await handlers._handle_photo_message_locked(photo_msg2, None)
    check("two-stage photo: exactly 2 Groq calls (vision + solve)", len(groq_call_log) == 2)
    check(
        "two-stage photo: final answer has no stray tuple repr (vision_text unpack bug)",
        all("(', [" not in t and "', []" not in t for t in photo_msg2.sent),
    )
    ai._client.chat.completions.create = fake_groq_create

    # ==================================================================
    # Fallback
    # ==================================================================
    # --- 9. Groq 429 -> fallback triggers, user gets an answer, footer names the fallback model. ---
    u9 = 10009
    grant_paid(u9)
    set_model(u9, "tester", "fast", "gptoss")

    async def fake_groq_429(**kwargs):
        raise make_groq_status(429, "rate limited")

    ai._client.chat.completions.create = fake_groq_429
    aitunnel_calls.clear()
    fallback_before = db.get_fallback_usage_today()
    m9 = FakeMessage(u9, text="вопрос под 429")
    await handlers._answer_text_query(m9, None, "вопрос под 429", u9, "tester")
    check("fallback/429: aitunnel WAS called (fallback dispatched)", len(aitunnel_calls) == 1)
    check("fallback/429: user got a real answer", any("gemini ответ" in t or "ответ" in t for t in m9.sent) and not any("Сейчас слишком много" in t for t in m9.sent))
    check("fallback/429: footer names the model that actually answered (Qwen), not GPT-OSS", any("Qwen 3.7 Flash" in t for t in m9.sent))
    check("fallback/429: footer does NOT falsely claim the originally-selected model", not any("Быстрый ответ (GPT-OSS 20B)" in t for t in m9.sent))
    check("fallback/429: fallback usage counter incremented", db.get_fallback_usage_today() == fallback_before + 1)

    # --- 10. Groq 401/400 -> NO fallback, user sees the error. ---
    u10 = 10010
    grant_paid(u10)
    set_model(u10, "tester", "fast", "gptoss")

    async def fake_groq_401(**kwargs):
        raise make_groq_status(401, "invalid api key")

    ai._client.chat.completions.create = fake_groq_401
    aitunnel_calls.clear()
    m10 = FakeMessage(u10, text="вопрос под 401")
    await handlers._answer_text_query(m10, None, "вопрос под 401", u10, "tester")
    check("fallback/401: aitunnel NOT called (not a limit error)", aitunnel_calls == [])
    check("fallback/401: user sees an error message", any(m10.sent))

    # --- 11. FALLBACK_DAILY_CAP exhausted -> no fallback. ---
    with db._lock:
        db._conn.execute("DELETE FROM fallback_usage")
        db._conn.commit()
    # burn the whole global cap on a throwaway user id
    for _ in range(FALLBACK_DAILY_CAP):
        db.try_consume_fallback(999999, FALLBACK_DAILY_CAP, FALLBACK_USER_DAILY_CAP + 100000)
    check("fallback cap: global cap now exhausted", db.get_fallback_usage_today() == FALLBACK_DAILY_CAP)

    u11 = 10011
    grant_paid(u11)
    set_model(u11, "tester", "fast", "gptoss")
    ai._client.chat.completions.create = fake_groq_429
    aitunnel_calls.clear()
    m11b = FakeMessage(u11, text="вопрос при исчерпанном общем лимите")
    await handlers._answer_text_query(m11b, None, "вопрос при исчерпанном общем лимите", u11, "tester")
    check("fallback cap: global cap exhausted -> aitunnel NOT called", aitunnel_calls == [])
    check("fallback cap: user sees the original rate-limit error", any("слишком много запросов" in t.lower() for t in m11b.sent))

    with db._lock:
        db._conn.execute("DELETE FROM fallback_usage")
        db._conn.commit()

    # --- 12. FALLBACK_USER_DAILY_CAP exhausted for ONE user -> fallback still works for another. ---
    uA, uB = 10012, 10013
    grant_paid(uA)
    grant_paid(uB)
    set_model(uA, "tester", "fast", "gptoss")
    set_model(uB, "tester", "fast", "gptoss")
    for _ in range(FALLBACK_USER_DAILY_CAP):
        db.try_consume_fallback(uA, FALLBACK_DAILY_CAP, FALLBACK_USER_DAILY_CAP)
    check("fallback per-user cap: user A's cap now exhausted", not db.try_consume_fallback(uA, FALLBACK_DAILY_CAP, FALLBACK_USER_DAILY_CAP))

    ai._client.chat.completions.create = fake_groq_429
    aitunnel_calls.clear()
    mA = FakeMessage(uA, text="вопрос от исчерпанного юзера")
    await handlers._answer_text_query(mA, None, "вопрос от исчерпанного юзера", uA, "tester")
    check("fallback per-user cap: exhausted user A gets NO fallback", aitunnel_calls == [])

    aitunnel_calls.clear()
    mB = FakeMessage(uB, text="вопрос от другого юзера")
    await handlers._answer_text_query(mB, None, "вопрос от другого юзера", uB, "tester")
    check("fallback per-user cap: user B (not exhausted) still gets the fallback", len(aitunnel_calls) == 1)

    # --- 13. FALLBACK_MODEL empty -> behaves exactly as before (no fallback attempt). ---
    original_fallback_model = handlers.FALLBACK_MODEL
    handlers.FALLBACK_MODEL = ""
    u13 = 10014
    grant_paid(u13)
    set_model(u13, "tester", "fast", "gptoss")
    ai._client.chat.completions.create = fake_groq_429
    aitunnel_calls.clear()
    m13 = FakeMessage(u13, text="вопрос без резерва")
    await handlers._answer_text_query(m13, None, "вопрос без резерва", u13, "tester")
    check("fallback disabled: aitunnel NOT called when FALLBACK_MODEL is empty", aitunnel_calls == [])
    check("fallback disabled: user sees the plain rate-limit error, as before this feature existed", any("слишком много запросов" in t.lower() for t in m13.sent))
    handlers.FALLBACK_MODEL = original_fallback_model
    ai._client.chat.completions.create = fake_groq_create

    # --- 14. A user with a saved qwen37 choice gets a WORKING bot after the option's removal. ---
    u14 = 10015
    grant_paid(u14)
    db.set_model_pref(u14, "tester", "premium", "qwen37")  # simulates a pre-removal stored choice
    groq_calls.clear()
    m14 = FakeMessage(u14, text="привет после удаления qwen37")
    await handlers._answer_text_query(m14, None, "привет после удаления qwen37", u14, "tester")
    check("qwen37 removal: stored qwen37 preference resolves to a working model (gptoss)", len(groq_calls) >= 1)
    check("qwen37 removal: user got a real answer, not an error", not any("Не удалось" in t for t in m14.sent))

    # --- 15. Fallback usage counter is visible in admin stats. ---
    stats_text = handlers._admin_stats_text()
    check("admin stats: mentions fallback usage", "резерв" in stats_text.lower())
    check("admin stats: fallback usage count reflects today's real counter", str(db.get_fallback_usage_today()) in stats_text)

    # ==================================================================
    # Cost tracking (Правка 2)
    # ==================================================================
    ai._aitunnel_client.chat.completions.create = fake_aitunnel_create
    u_cost = 10018
    grant_paid(u_cost)
    set_model(u_cost, "tester", "premium", "gemini")
    cost_before_success = db.get_today_cost()
    m_cost = FakeMessage(u_cost, text="платный вопрос")
    await handlers._answer_text_query(m_cost, None, "платный вопрос", u_cost, "tester")
    expected_gain = MODEL_COST_PER_REQUEST.get(GEMINI, 0.0)
    check(
        f"cost: a successful gemini request adds {expected_gain}₽ to today's spend",
        abs(db.get_today_cost() - (cost_before_success + expected_gain)) < 1e-9,
    )

    summary = db.get_cost_summary()
    check("cost: get_cost_summary's today total matches get_today_cost", abs(summary["today"] - db.get_today_cost()) < 1e-9)
    check("cost: get_cost_summary's today_by_model includes gemini", any(m == GEMINI for m, _ in summary["today_by_model"]))
    check(
        "cost: 7d/30d totals are >= today's total (today falls inside both windows)",
        summary["7d"] >= summary["today"] and summary["30d"] >= summary["today"],
    )

    # A Groq/free model never costs anything.
    cost_before_groq = db.get_today_cost()
    set_model(u_cost, "tester", "fast", "gptoss")
    ai._client.chat.completions.create = fake_groq_create
    m_free_model = FakeMessage(u_cost, text="бесплатная модель")
    await handlers._answer_text_query(m_free_model, None, "бесплатная модель", u_cost, "tester")
    check("cost: a Groq/free-model request adds nothing to spend", db.get_today_cost() == cost_before_groq)

    stats_text2 = handlers._admin_stats_text()
    check("admin stats: shows today's estimated spend", "Сегодня:" in stats_text2)
    check("admin stats: mentions 7 days and 30 days spend", "7 дней" in stats_text2 and "30 дней" in stats_text2)
    check("admin stats: labels the spend numbers as an estimate, not a real invoice", "оценка" in stats_text2.lower())

    # ==================================================================
    # Daily cost cap (Правка 3)
    # ==================================================================
    original_cap = handlers.DAILY_COST_CAP

    # A tiny cap that's already exceeded by the spend accumulated above.
    handlers.DAILY_COST_CAP = 0.0001
    with db._lock:
        db._conn.execute("DELETE FROM cost_cap_alerts")
        db._conn.commit()

    u_free_cap = 10019
    db._ensure_player(u_free_cap, "tester")  # free tier
    set_model(u_free_cap, "tester", "premium", "gemini")
    aitunnel_calls.clear()
    m_cap_free = FakeMessage(u_free_cap, text="под потолком, бесплатный")
    await handlers._answer_text_query(m_cap_free, None, "под потолком, бесплатный", u_free_cap, "tester")
    check("cost cap: free user denied when the cap is exceeded", aitunnel_calls == [] and any(m_cap_free.sent))
    check(
        "cost cap: the denial does NOT consume a trial use (it's the shared budget, not their own limit)",
        db.get_model_trial_usage(u_free_cap, GEMINI) == 0,
    )
    check(
        "cost cap: admin notified exactly once (this request's FakeBot) when the cap first blocks someone",
        len(m_cap_free.bot.admin_messages) == len(handlers.ADMIN_IDS) and len(handlers.ADMIN_IDS) > 0,
    )

    u_free_cap2 = 10021
    db._ensure_player(u_free_cap2, "tester")
    set_model(u_free_cap2, "tester", "premium", "gemini")
    m_cap_free2 = FakeMessage(u_free_cap2, text="ещё один под потолком")
    await handlers._answer_text_query(m_cap_free2, None, "ещё один под потолком", u_free_cap2, "tester")
    check("cost cap: a second denial the same day does NOT notify admins again", m_cap_free2.bot.admin_messages == [])

    u_paid_cap = 10020
    grant_paid(u_paid_cap)
    set_model(u_paid_cap, "tester", "premium", "gemini")
    aitunnel_calls.clear()
    m_cap_paid = FakeMessage(u_paid_cap, text="под потолком, платный")
    await handlers._answer_text_query(m_cap_paid, None, "под потолком, платный", u_paid_cap, "tester")
    check("cost cap: a PAYING user is never blocked by the cap", len(aitunnel_calls) == 1)

    # DAILY_COST_CAP = 0 disables the whole mechanism.
    handlers.DAILY_COST_CAP = 0
    u_cap_disabled = 10022
    db._ensure_player(u_cap_disabled, "tester")
    set_model(u_cap_disabled, "tester", "premium", "gemini")
    aitunnel_calls.clear()
    m_cap_off = FakeMessage(u_cap_disabled, text="потолок отключён")
    await handlers._answer_text_query(m_cap_off, None, "потолок отключён", u_cap_disabled, "tester")
    check("cost cap disabled (DAILY_COST_CAP=0): free user NOT blocked by spend at all", len(aitunnel_calls) == 1)

    handlers.DAILY_COST_CAP = original_cap


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
