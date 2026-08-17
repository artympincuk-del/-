import asyncio
import os
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "groq_retry_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
os.environ["AITUNNEL_API_KEY"] = "test-aitunnel-key"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

import httpx  # noqa: E402
import groq  # noqa: E402

from bot import ai, handlers  # noqa: E402
from bot.config import GROQ_MAX_RETRIES  # noqa: E402

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


async def run():
    # ------------------------------------------------------------------
    # 1. The Groq client is actually constructed with GROQ_MAX_RETRIES,
    #    not left at the SDK default (2).
    # ------------------------------------------------------------------
    check(f"config: GROQ_MAX_RETRIES is a small, sane default (got {GROQ_MAX_RETRIES})", 0 < GROQ_MAX_RETRIES <= 2)
    check(
        "ai: the real Groq client's max_retries matches config (not the SDK's own default of 2)",
        ai._client.max_retries == GROQ_MAX_RETRIES,
    )

    # ------------------------------------------------------------------
    # 2. End-to-end against the REAL groq SDK (not mocked at the .create()
    #    level, so the SDK's own internal retry/backoff is actually
    #    exercised) — a permanent 429 must stop after 1 + GROQ_MAX_RETRIES
    #    total HTTP calls, not the SDK's default of 3.
    # ------------------------------------------------------------------
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "0"}
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    test_client = groq.AsyncGroq(api_key="test", max_retries=GROQ_MAX_RETRIES, http_client=http_client)

    original_client = ai._client
    ai._client = test_client
    t0 = time.monotonic()
    try:
        await ai.ask_ai([], "тестовый вопрос", "openai/gpt-oss-20b")
        check("real-SDK 429: should have raised, did not", False)
    except ai.AIError as e:
        elapsed = time.monotonic() - t0
        check(f"real-SDK 429: capped at 1+GROQ_MAX_RETRIES={1 + GROQ_MAX_RETRIES} calls (got {call_count})", call_count == 1 + GROQ_MAX_RETRIES)
        check("real-SDK 429: raised as a limit error (so the fallback would trigger)", e.is_limit_error and e.status_code == 429)
        check(f"real-SDK 429: noticeably faster than the SDK default of 2 retries (got {elapsed:.2f}s)", elapsed < 1.5)
    finally:
        ai._client = original_client
        await http_client.aclose()

    # For contrast: confirm the SDK's own DEFAULT (2 retries = 3 calls)
    # really would have taken longer — proves this is a real fix, not just
    # a config value nobody reads.
    call_count_default = 0

    def handler_default(request: httpx.Request) -> httpx.Response:
        nonlocal call_count_default
        call_count_default += 1
        return httpx.Response(
            429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "0"}
        )

    transport2 = httpx.MockTransport(handler_default)
    http_client2 = httpx.AsyncClient(transport=transport2)
    default_client = groq.AsyncGroq(api_key="test", http_client=http_client2)  # SDK default max_retries
    try:
        await default_client.chat.completions.create(
            model="x", messages=[{"role": "user", "content": "hi"}], max_tokens=10
        )
    except Exception:
        pass
    finally:
        await http_client2.aclose()
    check(
        f"contrast: the SDK's own default makes MORE calls than our reduced setting ({call_count_default} > {1 + GROQ_MAX_RETRIES})",
        call_count_default > 1 + GROQ_MAX_RETRIES,
    )

    # ------------------------------------------------------------------
    # 3. Unchanged fallback behavior: mocked at the .create() level (as
    #    the rest of the suite does), a 429 still routes to AITUNNEL with
    #    the same daily-cap/footer logic as before — this change doesn't
    #    touch that.
    # ------------------------------------------------------------------
    def make_groq_429():
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        response = httpx.Response(429, request=request, json={"error": {"message": "rate limited"}})
        return groq.APIStatusError("429", response=response, body=response.json())

    aitunnel_calls = []

    async def fake_groq_429(**kwargs):
        raise make_groq_429()

    async def fake_aitunnel_create(**kwargs):
        aitunnel_calls.append(kwargs)
        return FakeResponse("резервный ответ")

    ai._client.chat.completions.create = fake_groq_429
    ai._aitunnel_client.chat.completions.create = fake_aitunnel_create

    text, sources, answered_model = await handlers._ask_ai_with_fallback(
        [], "вопрос", "openai/gpt-oss-20b", user_id=99001, is_paid=True,
    )
    check("fallback: still routes to AITUNNEL after a 429 (unchanged)", len(aitunnel_calls) == 1)
    check("fallback: still reports the real answering model (unchanged)", answered_model == handlers.FALLBACK_MODEL)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
