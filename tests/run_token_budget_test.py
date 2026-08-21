import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "token_budget_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
# Start from an empty DB like every other test file here — this one reads no
# stored state, but inheriting a database from a previous run is exactly the
# kind of machine-state dependency that makes a suite pass locally and fail
# on a clean checkout.
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

import httpx  # noqa: E402
import groq  # noqa: E402

import bot.ai as ai  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def make_413(detail="Request too large for model, limit 8000, requested 9000"):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(413, request=request, json={"error": {"message": detail}})
    return groq.APIStatusError("413 error", response=response, body=response.json())


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = FakeMessage(content)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]


def history_turn(role, content):
    return {"role": role, "content": content}


# The system prompt is untouchable overhead inside REQUEST_TOKEN_BUDGET —
# only history is trimmed against it. So a synthetic "small budget" has to
# be expressed RELATIVE to the real prompt, not as a bare number: hardcoding
# 1200 silently stopped meaning "tight but workable" the moment the prompt
# itself grew past it (which is exactly what happened when the clarify-
# instead-of-guess rules were added), and every history turn got dropped.
_SYSTEM_PROMPT_TOKENS = ai._estimate_tokens(ai._build_system_prompt(None))
# Room for a couple of the 10 history turns, nowhere near all of them.
_TIGHT_BUDGET = _SYSTEM_PROMPT_TOKENS + 100


async def run():
    # ------------------------------------------------------------------
    # Scenario 1: history trimmed under budget, system + current message
    # always present.
    # ------------------------------------------------------------------
    ai.REQUEST_TOKEN_BUDGET = _TIGHT_BUDGET  # small budget on purpose, forces trimming
    long_history = [
        history_turn("user" if i % 2 == 0 else "assistant", "слово " * 20)
        for i in range(10)
    ]
    calls = []

    async def fake_create_ok(**kwargs):
        calls.append(kwargs)
        return FakeResponse("ответ 1")

    ai._client.chat.completions.create = fake_create_ok
    result, _ = await ai.ask_ai(long_history, "текущий вопрос", "openai/gpt-oss-20b")
    check("scenario1: returns the model's answer", result == "ответ 1")
    sent = calls[-1]["messages"]
    check("scenario1: system prompt is first message", sent[0]["role"] == "system")
    check("scenario1: current message is last message", sent[-1]["content"] == "текущий вопрос")
    check("scenario1: history was trimmed (fewer than all 10 turns kept)", len(sent) - 2 < len(long_history))
    check("scenario1: at least the newest turn was kept", sent[-2]["content"] == long_history[-1]["content"])

    # ------------------------------------------------------------------
    # Scenario 4: role alternation survives trimming.
    # ------------------------------------------------------------------
    kept = sent[1:-1]
    alternation_ok = all(kept[i]["role"] != kept[i + 1]["role"] for i in range(len(kept) - 1))
    check("scenario4: kept history still strictly alternates roles", alternation_ok)
    check("scenario4: some history actually survived (non-trivial trim)", len(kept) > 0)

    # ------------------------------------------------------------------
    # Scenario 5: a lone over-budget current message is sent with NO
    # history, and is not itself truncated on the first attempt.
    # ------------------------------------------------------------------
    # Same reasoning as _TIGHT_BUDGET: expressed relative to the real
    # prompt. Enough for the system prompt plus a little, nowhere near
    # enough for the huge message below — so history has to go.
    ai.REQUEST_TOKEN_BUDGET = _SYSTEM_PROMPT_TOKENS + 50
    huge_message = "очень " * 500  # the message itself is what blows the budget
    small_history = [history_turn("user", "привет"), history_turn("assistant", "привет!")]
    calls.clear()
    ai._client.chat.completions.create = fake_create_ok
    result, _ = await ai.ask_ai(small_history, huge_message, "openai/gpt-oss-20b")
    sent = calls[-1]["messages"]
    check("scenario5: history dropped entirely (only system + user)", len(sent) == 2)
    check("scenario5: the oversized current message is sent unmodified", sent[-1]["content"] == huge_message)

    # ------------------------------------------------------------------
    # Scenario 2 & partially 3: 1st 413 -> retry with no history,
    # 2nd 413 -> retry with halved+noted message, 3rd 413 -> refusal.
    # ------------------------------------------------------------------
    ai.REQUEST_TOKEN_BUDGET = 4000
    attempt_log = []

    async def fake_create_always_413(**kwargs):
        attempt_log.append(kwargs["messages"])
        raise make_413(f"too large, attempt {len(attempt_log)}")

    ai._client.chat.completions.create = fake_create_always_413
    history_for_413 = [history_turn("user", "старое сообщение"), history_turn("assistant", "старый ответ")]
    long_current_message = "abcdefghij" * 20  # 200 chars, deterministic halving
    try:
        await ai.ask_ai(history_for_413, long_current_message, "openai/gpt-oss-20b")
        check("scenario2: raises AIError after 3 failed 413 attempts", False)
    except ai.AIError as e:
        check("scenario2: raises AIError after 3 failed 413 attempts", True)
        no_jargon = "token" not in e.user_message.lower() and "токен" not in e.user_message.lower() and "413" not in e.user_message
        check("scenario2: refusal message has no token/limit jargon", no_jargon)
        check(
            "scenario2: refusal tells the user to send less at once",
            any(w in e.user_message.lower() for w in ("один", "меньше", "короче")),
        )

    check("scenario2: exactly 3 attempts were made", len(attempt_log) == 3)
    check("scenario2: attempt 1 included history", any(m["role"] != "system" and m.get("content") not in (long_current_message,) and m["role"] == "user" and m["content"] != long_current_message for m in attempt_log[0]) or len(attempt_log[0]) > 2)
    check("scenario2: attempt 2 dropped history (system+user only)", len(attempt_log[1]) == 2)
    check("scenario2: attempt 3 dropped history (system+user only)", len(attempt_log[2]) == 2)
    # commit 2bb9d13 ("refine the 413 retry sequence") deliberately flipped
    # which half survives: it now KEEPS THE BEGINNING (the actual recognized
    # problem, per callers like the photo-solving prompt) and cuts the tail,
    # appending a note at the END — the opposite of the original "keep the
    # end, note at the start" behavior this check used to assert.
    half_len = len(long_current_message) // 2
    check(
        "scenario2: attempt 3's message text is halved (keeps the BEGINNING) with a truncation note",
        attempt_log[2][-1]["content"].startswith(long_current_message[:half_len])
        and "обрезан" in attempt_log[2][-1]["content"],
    )
    check(
        "scenario2: attempt 1 message untouched (not pre-truncated)",
        attempt_log[0][-1]["content"] == long_current_message,
    )

    # ------------------------------------------------------------------
    # Scenario 3: a later-attempt success reaches the user as a normal
    # answer, with no mention of retries/truncation in the return value.
    # ------------------------------------------------------------------
    call_count = 0

    async def fake_create_fail_then_succeed(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise make_413("too large on first try")
        return FakeResponse("нормальный финальный ответ")

    ai._client.chat.completions.create = fake_create_fail_then_succeed
    result, _ = await ai.ask_ai(history_for_413, "нормальный вопрос", "openai/gpt-oss-20b")
    check("scenario3: final answer returned verbatim", result == "нормальный финальный ответ")
    check("scenario3: exactly 2 attempts were made (1 fail + 1 success)", call_count == 2)
    check(
        "scenario3: no retry/truncation wording leaked into the answer",
        "обрезан" not in result and "попробуй" not in result.lower(),
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
