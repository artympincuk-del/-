import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "budget2_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

import httpx  # noqa: E402
import groq  # noqa: E402

from bot import ai  # noqa: E402
from bot import db  # noqa: E402
from bot import handlers  # noqa: E402
from bot.config import MAX_HISTORY_TURNS, REQUEST_TOKEN_BUDGET  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def make_413(detail="Request too large, limit 8000, requested 9000"):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(413, request=request, json={"error": {"message": detail}})
    return groq.APIStatusError("413 error", response=response, body=response.json())


class FakeMessageObj:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = FakeMessageObj(content)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]


def history_turn(role, content):
    return {"role": role, "content": content}


async def run():
    # ------------------------------------------------------------------
    # 1. A long history is trimmed so the estimate stays under
    #    REQUEST_TOKEN_BUDGET, system prompt + current message always
    #    included.
    # ------------------------------------------------------------------
    long_history = [
        history_turn("user" if i % 2 == 0 else "assistant", "слово " * 80)
        for i in range(20)
    ]
    system_content = ai._build_system_prompt(None)
    trimmed = ai._trim_history_to_budget(long_history, system_content, "текущий вопрос", REQUEST_TOKEN_BUDGET)
    total = (
        ai._estimate_tokens(system_content)
        + sum(ai._estimate_message_tokens(t["content"]) for t in trimmed)
        + ai._estimate_message_tokens("текущий вопрос")
    )
    check("history trim: total estimate <= REQUEST_TOKEN_BUDGET", total <= REQUEST_TOKEN_BUDGET)
    check("history trim: fewer than all 20 turns kept (actually trimmed)", len(trimmed) < len(long_history))
    check("history trim: kept turns are a suffix of the original (newest kept)", trimmed == long_history[len(long_history) - len(trimmed):])

    # ------------------------------------------------------------------
    # 4. Role alternation survives trimming.
    # ------------------------------------------------------------------
    check(
        "role alternation: kept history still strictly alternates roles",
        all(trimmed[i]["role"] != trimmed[i + 1]["role"] for i in range(len(trimmed) - 1)),
    )

    # ------------------------------------------------------------------
    # 5. A lone over-budget current message is sent without history, and
    #    is NOT truncated by our own estimate (only Groq's real response
    #    decides that).
    # ------------------------------------------------------------------
    huge_message = "очень " * 2000
    calls = []

    async def fake_create_ok(**kwargs):
        calls.append(kwargs)
        return FakeResponse("ok")

    ai._client.chat.completions.create = fake_create_ok
    small_history = [history_turn("user", "привет"), history_turn("assistant", "привет!")]
    await ai.ask_ai(small_history, huge_message, "openai/gpt-oss-20b")
    sent_messages = calls[-1]["messages"]
    check("oversized message: history dropped (system + user only)", len(sent_messages) == 2)
    check("oversized message: sent unmodified, not truncated by our estimate", sent_messages[-1]["content"] == huge_message)

    # ------------------------------------------------------------------
    # 2 & 3. 413 retry sequence + truncation direction + a later success
    # reaching the user as a normal answer.
    # ------------------------------------------------------------------
    attempt_log = []

    async def fake_create_413_then_ok(**kwargs):
        attempt_log.append(kwargs)
        if len(attempt_log) < 2:
            raise make_413(f"too large, attempt {len(attempt_log)}")
        return FakeResponse("обычный финальный ответ")

    ai._client.chat.completions.create = fake_create_413_then_ok
    attempt_log.clear()
    history_for_413 = [history_turn("user", "старое"), history_turn("assistant", "старый ответ")]
    long_current_message = "начало сообщения " + ("x" * 300) + " конец сообщения"
    result, _ = await ai.ask_ai(history_for_413, long_current_message, "openai/gpt-oss-20b")
    check("2nd-attempt success: answer reaches caller verbatim", result == "обычный финальный ответ")
    check("2nd-attempt success: exactly 2 Groq calls made", len(attempt_log) == 2)
    check(
        "2nd-attempt success: no retry/truncation wording leaked into the answer",
        "обрезан" not in result and "попробуй" not in result.lower(),
    )
    check("attempt 1 included history", len(attempt_log[0]["messages"]) > 2)
    check("attempt 2 (after 1st 413) dropped history", len(attempt_log[1]["messages"]) == 2)
    check(
        "attempt 2 (after 1st 413) max_tokens cut to the reasonable minimum",
        attempt_log[1]["max_tokens"] == ai._MIN_RESPONSE_TOKENS,
    )
    check(
        "attempt 2 (after 1st 413) message text is untouched (not yet truncated)",
        attempt_log[1]["messages"][-1]["content"] == long_current_message,
    )

    # Now force 413 on every attempt to check the 2nd-413 truncation and
    # the final human-readable refusal.
    async def fake_create_always_413(**kwargs):
        attempt_log.append(kwargs)
        raise make_413(f"too large, attempt {len(attempt_log)}")

    ai._client.chat.completions.create = fake_create_always_413
    attempt_log.clear()
    try:
        await ai.ask_ai(history_for_413, long_current_message, "openai/gpt-oss-20b")
        check("3 failed 413s: raises AIError", False)
    except ai.AIError as e:
        check("3 failed 413s: raises AIError", True)
        msg = e.user_message.lower()
        check("refusal text mentions документ/фото being too large", "документ" in msg or "фото" in msg)
        check("refusal text suggests one page/question at a time", "страниц" in msg or "один вопрос" in msg)
        check("refusal text has no token/limit jargon", "token" not in msg and "413" not in msg and "лимит" not in msg)

    check("3 failed 413s: exactly 3 Groq calls made", len(attempt_log) == 3)
    check("attempt 3 (after 2nd 413) dropped history too", len(attempt_log[2]["messages"]) == 2)
    check(
        "attempt 3 (after 2nd 413) max_tokens still at the floor",
        attempt_log[2]["max_tokens"] == ai._MIN_RESPONSE_TOKENS,
    )
    truncated_text = attempt_log[2]["messages"][-1]["content"]
    half = len(long_current_message) // 2
    check(
        "attempt 3 (after 2nd 413): text cut to its first half (kept the beginning)",
        truncated_text.startswith(long_current_message[:half]),
    )
    check(
        "attempt 3 (after 2nd 413): a truncation note is appended at the end",
        truncated_text.endswith("]") and "обрезан" in truncated_text,
    )
    check(
        "attempt 3 (after 2nd 413): shorter than the original message",
        len(truncated_text) < len(long_current_message),
    )

    # ------------------------------------------------------------------
    # 6. A genuinely long conversation (15 long pairs) doesn't blow the
    #    16th, short follow-up question into a refusal.
    # ------------------------------------------------------------------
    long_convo_calls = []

    async def fake_create_convo(**kwargs):
        long_convo_calls.append(kwargs)
        return FakeResponse("короткий ответ")

    ai._client.chat.completions.create = fake_create_convo
    convo_history = []
    for i in range(15):
        convo_history.append(history_turn("user", f"Реши задачу номер {i}: " + ("подробное условие задачи. " * 60)))
        convo_history.append(history_turn("assistant", f"Решение задачи {i}: " + ("подробный разбор шагов решения. " * 60)))
    result, _ = await ai.ask_ai(convo_history[-MAX_HISTORY_TURNS * 2:], "А как насчёт этого?", "openai/gpt-oss-20b")
    check("15-pair conversation: 16th short question does NOT get refused", result == "короткий ответ")
    check("15-pair conversation: exactly 1 Groq call (no retries needed)", len(long_convo_calls) == 1)
    sent = long_convo_calls[0]["messages"]
    est = sum(ai._estimate_message_tokens(m["content"]) for m in sent)
    check(f"15-pair conversation: request stayed within budget (~{est} tokens)", est <= REQUEST_TOKEN_BUDGET + 50)

    # ------------------------------------------------------------------
    # 7. max_tokens for a low-ceiling model is smaller than for gpt-oss.
    # ------------------------------------------------------------------
    mt_calls = []

    async def fake_create_capture_mt(**kwargs):
        mt_calls.append(kwargs)
        return FakeResponse("ok")

    ai._client.chat.completions.create = fake_create_capture_mt
    mt_calls.clear()
    await ai.ask_ai([], "короткий вопрос", "openai/gpt-oss-20b")
    gptoss_max_tokens = mt_calls[-1]["max_tokens"]

    mt_calls.clear()
    await ai.ask_ai([], "короткий вопрос", "llama-3.1-8b-instant")
    llama_max_tokens = mt_calls[-1]["max_tokens"]

    print()
    print(f"max_tokens for openai/gpt-oss-20b (ceiling {ai.MODEL_TOKEN_CEILINGS['openai/gpt-oss-20b']}): {gptoss_max_tokens}")
    print(f"max_tokens for llama-3.1-8b-instant (ceiling {ai.MODEL_TOKEN_CEILINGS['llama-3.1-8b-instant']}): {llama_max_tokens}")
    check("max_tokens: low-ceiling model gets a smaller max_tokens than gpt-oss", llama_max_tokens < gptoss_max_tokens)

    # ------------------------------------------------------------------
    # 8. Llama 3.1 8B is gone from the menu; an existing user's stored
    #    ("fast", "llama") preference softly falls back to gptoss.
    # ------------------------------------------------------------------
    check("menu: ('fast', 'llama') no longer in MODEL_OPTIONS", ("fast", "llama") not in handlers.MODEL_OPTIONS)
    check("menu: llama-3.1-8b-instant not reachable via any MODEL_OPTIONS entry", all(
        opt["model"] != "llama-3.1-8b-instant" for opt in handlers.MODEL_OPTIONS.values()
    ))

    fallback_status = {"model_pref": "fast", "model_choice": "llama"}
    option = handlers._model_option(fallback_status)
    check("menu: a stored 'fast'+'llama' preference falls back to gptoss without crashing", option["model"] == handlers.FAST_MODEL)

    class FakeCallback:
        def __init__(self, data, user_id):
            self.data = data

            class _U:
                pass

            self.from_user = _U()
            self.from_user.id = user_id
            self.from_user.username = "tester"
            self.message = None

        async def answer(self, text=None, **kwargs):
            self.answered = text

    async def fake_edit_or_send(callback, text, kb, **kwargs):
        pass

    original_edit_or_send = handlers._edit_or_send
    handlers._edit_or_send = fake_edit_or_send
    try:
        cb = FakeCallback("model:fast:llama", 8001)
        try:
            await handlers.cb_model(cb)
            check("menu: tapping a stale fast/llama button doesn't crash", True)
        except Exception as e:
            check(f"menu: tapping a stale fast/llama button doesn't crash ({e!r})", False)
        stored = db.get_status(8001, "tester")
        check("menu: stale-button tap stored gptoss, not llama", stored["model_choice"] == "gptoss")
    finally:
        handlers._edit_or_send = original_edit_or_send


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
