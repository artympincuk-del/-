import asyncio
import html as html_module
import os
import re
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "freshness_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from aiogram.exceptions import TelegramBadRequest  # noqa: E402

from bot import ai  # noqa: E402
from bot import handlers  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ======================================================================
# Telegram-HTML-like validator (extended from earlier tests to also accept
# <a href="...">, since sources links use it) — proves the sanitized output
# would really be accepted by Telegram, not just "look plausible".
# ======================================================================
_TG_SUPPORTED_TAGS = {"b", "i", "u", "s", "code", "pre", "a"}
_TAG_RE = re.compile(r'<(/)?([a-zA-Z][a-zA-Z0-9]*)((?:\s+href="[^"<>]*")?)>')


def _mock_telegram_html_check(text: str) -> None:
    stack = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "<":
            m = _TAG_RE.match(text, i)
            if not m or m.group(2).lower() not in _TG_SUPPORTED_TAGS:
                raise TelegramBadRequest(method=None, message="Bad Request: can't parse entities: unsupported tag")
            is_close, name = bool(m.group(1)), m.group(2).lower()
            if is_close:
                if not stack or stack[-1] != name:
                    raise TelegramBadRequest(method=None, message="Bad Request: unexpected close tag")
                stack.pop()
            else:
                stack.append(name)
            i = m.end()
            continue
        if c == ">":
            raise TelegramBadRequest(method=None, message="Bad Request: unexpected >")
        i += 1
    if stack:
        raise TelegramBadRequest(method=None, message="Bad Request: unclosed tag")


_UNSET = object()


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, parse_mode=_UNSET):
        if parse_mode is None:
            self.sent.append({"text": text, "html": False})
            return
        _mock_telegram_html_check(text)
        self.sent.append({"text": text, "html": True})


class FakeChoiceMessage:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = FakeChoiceMessage(content)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]


async def run():
    # ------------------------------------------------------------------
    # Freshness marker detection.
    # ------------------------------------------------------------------
    check("'кто выиграл турнир' is a freshness marker", ai._has_freshness_marker("кто выиграл турнир"))
    check("'реши уравнение x^2 = 4' is NOT a freshness marker", not ai._has_freshness_marker("реши уравнение x^2 = 4"))
    check("a current/future year mention is a freshness marker", ai._has_freshness_marker("Олимпиада 2026"))

    # ------------------------------------------------------------------
    # Today's date is present in the system prompt.
    # ------------------------------------------------------------------
    prompt = ai._build_system_prompt(None)
    check("today's date string is present in the system prompt", ai._current_date_str() in prompt)
    check("staleness instruction is present", "устарели" in prompt)

    # ------------------------------------------------------------------
    # Forced search: a freshness-marked question triggers search_web BEFORE
    # the model is asked, even though the fake model itself never requests
    # a tool call.
    # ------------------------------------------------------------------
    search_calls = []

    async def fake_search_structured(query, max_results=5):
        search_calls.append(query)
        return [
            {"title": "Чемпионат мира 2026 — итоги", "href": "https://news.example.com/wc2026", "body": "Победитель: сборная X."},
            {"title": "Финал ЧМ-2026", "href": "https://sport.example.com/final", "body": "Счёт 2:1."},
        ]

    groq_calls = []

    async def fake_create_ok(**kwargs):
        groq_calls.append(kwargs)
        return FakeResponse("Победила сборная X со счётом 2:1.")

    ai._search_web_structured = fake_search_structured
    ai._client.chat.completions.create = fake_create_ok

    search_calls.clear()
    groq_calls.clear()
    text, sources = await ai.ask_ai([], "кто выиграл чемпионат мира 2026?", "openai/gpt-oss-20b", enable_search=True)
    check("forced search: search_web WAS called for a freshness-marked question", len(search_calls) == 1)
    check("forced search: model never got a tool_calls round trip (search ran up front)", groq_calls[0].get("tools") is not None)  # tool stays available too
    system_msg = groq_calls[-1]["messages"][0]["content"]
    check("forced search: system prompt includes the search-results block", "Данные поиска в интернете на" in system_msg)
    check("forced search: system prompt includes today's date in that block", ai._current_date_str() in system_msg)
    check("forced search: returned sources match the search results", sources == [
        ("Чемпионат мира 2026 — итоги", "https://news.example.com/wc2026"),
        ("Финал ЧМ-2026", "https://sport.example.com/final"),
    ])
    check("forced search: answer text passed through untouched", text == "Победила сборная X со счётом 2:1.")

    # ------------------------------------------------------------------
    # A non-freshness question does NOT trigger search.
    # ------------------------------------------------------------------
    search_calls.clear()
    groq_calls.clear()
    text2, sources2 = await ai.ask_ai([], "реши уравнение x^2 = 4", "openai/gpt-oss-20b", enable_search=True)
    check("no marker: search_web NOT called", search_calls == [])
    check("no marker: sources empty", sources2 == [])
    system_msg2 = groq_calls[-1]["messages"][0]["content"]
    check("no marker: system prompt has no search-results block", "Данные поиска в интернете на" not in system_msg2)

    # ------------------------------------------------------------------
    # Empty search results -> the model is told honestly, no fabricated
    # sources are cited.
    # ------------------------------------------------------------------
    async def fake_search_empty(query, max_results=5):
        search_calls.append(query)
        return []

    async def fake_create_honest(**kwargs):
        groq_calls.append(kwargs)
        return FakeResponse("Свежих данных по этому вопросу сейчас нет.")

    ai._search_web_structured = fake_search_empty
    ai._client.chat.completions.create = fake_create_honest
    search_calls.clear()
    groq_calls.clear()
    text3, sources3 = await ai.ask_ai([], "какие последние новости о курсе доллара сегодня?", "openai/gpt-oss-20b", enable_search=True)
    check("empty results: search_web was attempted", len(search_calls) == 1)
    check("empty results: no sources cited (nothing to cite)", sources3 == [])
    check("empty results: answer honestly says there's no fresh data", "нет" in text3.lower())
    check(
        "empty results: answer does NOT get a search-results block appended to the system prompt",
        "Данные поиска в интернете на" not in groq_calls[-1]["messages"][0]["content"],
    )

    # ------------------------------------------------------------------
    # Sources produce real clickable links; no-sources produces none.
    # ------------------------------------------------------------------
    sources_block = handlers._build_sources_html([
        ("Title A", "https://example.com/a"),
        ("Title B", "https://example.com/b"),
    ])
    check("_build_sources_html: non-empty for real sources", sources_block != "")
    check("_build_sources_html: contains a well-formed <a href> link", '<a href="https://example.com/a">' in sources_block)

    msg_with_sources = FakeMessage()
    await handlers._send_long(msg_with_sources, "Вот ответ по существу.", trusted_suffix=sources_block)
    check("with sources: sent without raising (valid Telegram HTML)", len(msg_with_sources.sent) == 1 and msg_with_sources.sent[0]["html"])
    sent_text = msg_with_sources.sent[0]["text"]
    check("with sources: clickable <a href> link present in the sent message", '<a href="https://example.com/a">Title A</a>' in sent_text)
    check("with sources: 'Источники' label present", "Источники" in sent_text)

    check("_build_sources_html: empty for no sources", handlers._build_sources_html([]) == "")

    msg_no_sources = FakeMessage()
    await handlers._send_long(msg_no_sources, "Ответ без поиска.", trusted_suffix=handlers._build_sources_html([]))
    check("without sources: no 'Источники' block in the sent message", "Источники" not in msg_no_sources.sent[0]["text"])
    check("without sources: no <a> tag at all", "<a " not in msg_no_sources.sent[0]["text"])

    # ------------------------------------------------------------------
    # Markdown **bold**/*italic* survives as real Telegram formatting, no
    # literal asterisks reach the user.
    # ------------------------------------------------------------------
    msg_md = FakeMessage()
    await handlers._send_long(msg_md, "Это **жирный** текст и *курсив* тоже, а тут `print(x)`.")
    check("markdown: sent without raising", len(msg_md.sent) == 1 and msg_md.sent[0]["html"])
    md_text = msg_md.sent[0]["text"]
    check("markdown: **bold** became a real <b> tag", "<b>жирный</b>" in md_text)
    check("markdown: *italic* became a real <i> tag", "<i>курсив</i>" in md_text)
    check("markdown: `real code` became a real <code> tag", "<code>print(x)</code>" in md_text)
    check("markdown: no literal asterisks reach the user", "*" not in html_module.unescape(md_text))
    check("markdown: no literal backticks reach the user", "`" not in html_module.unescape(md_text))

    # A backticked ORDINARY RUSSIAN WORD is deliberately not code — the tags
    # come back off in _unwrap_plain_code, so the user reads a sentence
    # instead of monospace speckle. (Covered in depth in run_code_tags_test.)
    msg_word = FakeMessage()
    await handlers._send_long(msg_word, "Нажми `кнопку` ещё раз.")
    word_text = msg_word.sent[0]["text"]
    check("markdown: a backticked plain Russian word ends up as plain text", word_text == "Нажми кнопку ещё раз.")


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
