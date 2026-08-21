import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "aitunnel_tuning_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
os.environ["AITUNNEL_API_KEY"] = "test-aitunnel-key"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import ai  # noqa: E402
from bot import handlers  # noqa: E402
from bot.config import AITUNNEL_MAX_CONCURRENT, GROQ_MAX_CONCURRENT, MODEL_RESPONSE_TOKENS  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeMsg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMsg(content)
        self.finish_reason = "stop"


class FakeResp:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


async def run():
    # ------------------------------------------------------------------
    # 1. requirements.txt pin — checked separately by pip install, not here.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 2. MODEL_RESPONSE_TOKENS has the new models at 4096, not the 2048
    #    default.
    # ------------------------------------------------------------------
    check("config: qwen3.7-flash response budget is 4096", MODEL_RESPONSE_TOKENS.get("qwen3.7-flash") == 4096)
    check("config: gemini-3.5-flash-lite response budget is 4096", MODEL_RESPONSE_TOKENS.get("gemini-3.5-flash-lite") == 4096)

    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return FakeResp("ok")

    ai._aitunnel_client.chat.completions.create = fake_create
    calls.clear()
    await ai.ask_ai([], "короткий вопрос", "qwen3.7-flash")
    check("ask_ai: qwen3.7-flash actually gets max_tokens=4096 (not 2048)", calls[-1]["max_tokens"] == 4096)

    # ------------------------------------------------------------------
    # 3. Separate AITUNNEL semaphore, Groq semaphore untouched.
    # ------------------------------------------------------------------
    check("config: AITUNNEL_MAX_CONCURRENT default is 3", AITUNNEL_MAX_CONCURRENT == 3)
    check("ai: _aitunnel_semaphore exists and is a distinct object from _groq_semaphore", ai._aitunnel_semaphore is not ai._groq_semaphore)
    check("ai: _groq_semaphore still sized from GROQ_MAX_CONCURRENT", ai._groq_semaphore._value == GROQ_MAX_CONCURRENT)
    check("ai: _aitunnel_semaphore sized from AITUNNEL_MAX_CONCURRENT", ai._aitunnel_semaphore._value == AITUNNEL_MAX_CONCURRENT)

    # Real concurrency test: fire more AITUNNEL requests at once than the
    # semaphore allows, and verify the actual in-flight count never exceeds
    # AITUNNEL_MAX_CONCURRENT.
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def slow_create(**kwargs):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return FakeResp("ok")

    ai._aitunnel_client.chat.completions.create = slow_create
    await asyncio.gather(*[ai.ask_ai([], f"вопрос {i}", "qwen3.7-flash") for i in range(8)])
    check(
        f"concurrency: at most AITUNNEL_MAX_CONCURRENT requests in flight at once (saw {max_in_flight})",
        max_in_flight <= AITUNNEL_MAX_CONCURRENT,
    )
    check(f"concurrency: the semaphore was actually the bottleneck (saw {max_in_flight} == limit)", max_in_flight == AITUNNEL_MAX_CONCURRENT)

    # Groq path is untouched: still bounded by GROQ_MAX_CONCURRENT, using a
    # completely separate semaphore instance (no cross-contamination).
    groq_in_flight = 0
    groq_max_in_flight = 0
    groq_lock = asyncio.Lock()

    async def slow_groq_create(**kwargs):
        nonlocal groq_in_flight, groq_max_in_flight
        async with groq_lock:
            groq_in_flight += 1
            groq_max_in_flight = max(groq_max_in_flight, groq_in_flight)
        await asyncio.sleep(0.05)
        async with groq_lock:
            groq_in_flight -= 1
        return FakeResp("ok")

    ai._client.chat.completions.create = slow_groq_create
    await asyncio.gather(*[ai.ask_ai([], f"вопрос {i}", "openai/gpt-oss-20b") for i in range(8)])
    check(
        f"concurrency: Groq path still bounded by GROQ_MAX_CONCURRENT (saw {groq_max_in_flight}, limit {GROQ_MAX_CONCURRENT})",
        groq_max_in_flight <= GROQ_MAX_CONCURRENT,
    )

    # ------------------------------------------------------------------
    # 4. HELP_TEXT explains the model choice, using constants — /start
    #    deliberately does NOT (Правка 4.4: a brand-new user shouldn't be
    #    handed a choice between models before seeing a single answer;
    #    two thirds of arrivals were leaving without sending anything).
    # ------------------------------------------------------------------
    check("help/start: fast model label constant matches MODEL_OPTIONS", handlers._FAST_MODEL_LABEL == handlers.MODEL_OPTIONS[("fast", "gptoss")]["label"])
    check("help/start: premium model label constant matches MODEL_OPTIONS", handlers._PREMIUM_MODEL_LABEL == handlers.MODEL_OPTIONS[("premium", "gptoss")]["label"])
    check("HELP_TEXT: mentions the Модель button", handlers.BTN_MODEL in handlers.HELP_TEXT)
    check("HELP_TEXT: mentions the fast model label", handlers._FAST_MODEL_LABEL in handlers.HELP_TEXT)
    check("HELP_TEXT: mentions the premium model label", handlers._PREMIUM_MODEL_LABEL in handlers.HELP_TEXT)

    class FakeUser:
        id = 1234
        username = "tester"

    class FakeChat:
        type = "private"
        id = 1234

    class FakeState:
        async def clear(self):
            pass

    sent_texts = []

    class FakeStartMessage:
        from_user = FakeUser()
        chat = FakeChat()
        text = "/start"

        async def answer(self, text, **kwargs):
            sent_texts.append(text)

    await handlers.cmd_start(FakeStartMessage(), FakeState())
    welcome_text = sent_texts[0]
    check("/start: does NOT push the model choice at a brand-new user", handlers.BTN_MODEL not in welcome_text)
    check("/start: does not name the fast model", handlers._FAST_MODEL_LABEL not in welcome_text)
    check("/start: does not name the premium model", handlers._PREMIUM_MODEL_LABEL not in welcome_text)
    check(
        "/start: the model choice is still reachable — it's in the menu that follows",
        any("Меню" in t for t in sent_texts),
    )

    # ------------------------------------------------------------------
    # Model menu: Qwen 3.7 Flash removed (Правка 4 of the follow-up task
    # made it fallback-only), Gemini 3.5 Flash Lite still offered — this is
    # what a real /model tap in the deployed bot renders. Superseded in
    # detail by run_model_fallback_test.py; kept here as a light smoke check.
    # ------------------------------------------------------------------
    menu_status = {"model_pref": "fast", "model_choice": "gptoss", "subscription_until": None, "unlimited_until": None, "bonus_credits": 0}
    kb = handlers.model_keyboard(menu_status, 8001)
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    check("model menu: 'Qwen 3.7 Flash' button NOT present (moved to fallback-only)", not any("Qwen 3.7 Flash" in t for t in button_texts))
    check("model menu: 'Gemini' button present", any("Gemini" in t for t in button_texts))
    check(
        "model menu: no callback_data for qwen37 remains",
        not any(btn.callback_data == "model:premium:qwen37" for row in kb.inline_keyboard for btn in row),
    )
    check(
        "model menu: gemini callback_data is wired correctly",
        any(btn.callback_data == "model:premium:gemini" for row in kb.inline_keyboard for btn in row),
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
