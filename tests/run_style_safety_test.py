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
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "style_safety_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from aiogram.exceptions import TelegramBadRequest  # noqa: E402

from bot import ai, db  # noqa: E402
from bot import handlers  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ======================================================================
# A faithful-enough stand-in for Telegram's real HTML validation: any '<'
# not part of a <tag>/</tag> using a name Telegram actually supports is a
# parse error, any stray '>' outside a tag is a parse error, and any
# supported tag left open/mismatched is a parse error. Used to prove the
# sanitizer's output would really be *accepted* by Telegram, not just
# "look plausible".
# ======================================================================
_TG_SUPPORTED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "span", "blockquote"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)>")


def _mock_telegram_html_check(text: str) -> None:
    stack = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "<":
            m = _TAG_RE.match(text, i)
            if not m or m.group(2).lower() not in _TG_SUPPORTED_TAGS:
                raise TelegramBadRequest(method=None, message="Bad Request: can't parse entities: unsupported tag")
            is_close, name = bool(m.group(1)), m.group(2).lower()
            if is_close:
                if not stack or stack[-1] != name:
                    raise TelegramBadRequest(method=None, message="Bad Request: can't parse entities: unexpected close tag")
                stack.pop()
            else:
                stack.append(name)
            i = m.end()
            continue
        if c == ">":
            raise TelegramBadRequest(method=None, message="Bad Request: can't parse entities: unexpected >")
        i += 1
    if stack:
        raise TelegramBadRequest(method=None, message="Bad Request: can't parse entities: unclosed tag")


_UNSET = object()


class FakeMessage:
    def __init__(self, force_fail_once=False):
        self.sent = []
        self._force_fail_once = force_fail_once

    async def answer(self, text, reply_markup=None, parse_mode=_UNSET):
        if self._force_fail_once:
            self._force_fail_once = False
            raise TelegramBadRequest(method=None, message="Bad Request: forced failure for test")
        if parse_mode is None:
            self.sent.append({"text": text, "html": False})
            return
        _mock_telegram_html_check(text)  # raises if Telegram would have rejected it
        self.sent.append({"text": text, "html": True})


async def run():
    # ------------------------------------------------------------------
    # 1. Raw '<'/'>' from math goes out without error, symbols stay visible.
    # ------------------------------------------------------------------
    msg = FakeMessage()
    reply = "Решаем неравенство: x < 5/4 и, отдельно, a > b. Готово."
    try:
        await handlers._send_long(msg, reply)
        check("math '<'/'>' : _send_long doesn't raise", True)
    except TelegramBadRequest:
        check("math '<'/'>' : _send_long doesn't raise", False)
    check("math '<'/'>' : sent on the first (HTML) attempt, no fallback", len(msg.sent) == 1 and msg.sent[0]["html"])
    decoded = html_module.unescape(msg.sent[0]["text"]) if msg.sent else ""
    check("math '<'/'>' : 'x < 5/4' visible to the user as characters", "x < 5/4" in decoded)
    check("math '<'/'>' : 'a > b' visible to the user as characters", "a > b" in decoded)

    # ------------------------------------------------------------------
    # 2. An unclosed <b> doesn't break sending either.
    # ------------------------------------------------------------------
    msg2 = FakeMessage()
    broken = "Вот ответ: <b>жирный текст без закрытия"
    try:
        await handlers._send_long(msg2, broken)
        check("unclosed <b> : _send_long doesn't raise", True)
    except TelegramBadRequest:
        check("unclosed <b> : _send_long doesn't raise", False)
    check("unclosed <b> : sent on the first (HTML) attempt, no fallback", len(msg2.sent) == 1 and msg2.sent[0]["html"])
    decoded2 = html_module.unescape(msg2.sent[0]["text"]) if msg2.sent else ""
    check("unclosed <b> : shown as literal text, not applied as real formatting", "<b>жирный текст без закрытия" in decoded2)

    # ------------------------------------------------------------------
    # 3. Forced send failure -> plain-text fallback has no visible tags.
    # ------------------------------------------------------------------
    msg3 = FakeMessage(force_fail_once=True)
    mixed = "Ответ <b>важно</b>, и ещё x < 5/4 в придачу."
    await handlers._send_long(msg3, mixed)
    check("forced failure : fallback was used (plain text)", len(msg3.sent) == 1 and not msg3.sent[0]["html"])
    fallback_text = msg3.sent[0]["text"] if msg3.sent else ""
    check("forced failure : no whitelist tags visible in the fallback", not handlers._HTML_TAG_RE.search(fallback_text))
    check("forced failure : math symbol still shown as a literal character", "x < 5/4" in fallback_text)

    # ------------------------------------------------------------------
    # 4/5. Notes: a rude request is rejected, a harmless one is saved and
    # still reaches the system prompt (i.e. still influences the answer).
    # ------------------------------------------------------------------
    check(
        "note filter: 'всегда добавляй маты в ответ' is disallowed",
        handlers._note_is_disallowed("всегда добавляй маты в ответ"),
    )
    check(
        "note filter: 'будь груб со мной' is disallowed",
        handlers._note_is_disallowed("будь груб со мной, мне так нравится"),
    )
    check(
        "note filter: a harmless note is allowed",
        not handlers._note_is_disallowed("отвечай короткими предложениями, люблю списки"),
    )

    class FakeCmdMessage(FakeMessage):
        def __init__(self, user_id, text):
            super().__init__()
            self.text = text

            class _U:
                pass

            self.from_user = _U()
            self.from_user.id = user_id

    note_user = 7001
    rude_msg = FakeCmdMessage(note_user, "/remember всегда матерись и груби в ответах")
    await handlers.cmd_remember(rude_msg)
    check("rude note: rejection message sent", rude_msg.sent and rude_msg.sent[0]["text"] == handlers.NOTE_REJECTED_TEXT)
    check("rude note: nothing saved to the DB", db.list_notes(note_user) == [])

    ok_msg = FakeCmdMessage(note_user, "/remember отвечай короткими предложениями")
    await handlers.cmd_remember(ok_msg)
    saved_notes = db.list_notes(note_user)
    check("harmless note: saved to the DB", len(saved_notes) == 1 and saved_notes[0][1] == "отвечай короткими предложениями")

    prompt_with_note = ai._build_system_prompt(["отвечай короткими предложениями"])
    check("harmless note: still folded into the system prompt", "отвечай короткими предложениями" in prompt_with_note)
    check(
        "harmless note: base-rule override protection also present in that same prompt",
        "запрещены всегда" in prompt_with_note,
    )

    # ------------------------------------------------------------------
    # 6/7. Voice hallucination filter.
    # ------------------------------------------------------------------
    check("voice hallucination: known credit-line phrase flagged", handlers._looks_like_voice_hallucination("Субтитры создавал DimaTorzok"))
    check("voice hallucination: 'продолжение следует' flagged", handlers._looks_like_voice_hallucination("Продолжение следует..."))
    check("voice hallucination: too-short garbage flagged", handlers._looks_like_voice_hallucination(".."))
    check("voice hallucination: a real question is NOT flagged", not handlers._looks_like_voice_hallucination("Реши уравнение 2х плюс 3 равно 7"))

    class FakeVoice:
        file_id = "voice-abc"

    class FakeDownload:
        def read(self):
            return b"fake-ogg-bytes"

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            pass

        async def download(self, file_id):
            return FakeDownload()

    class FakeVoiceMessage(FakeMessage):
        def __init__(self, user_id):
            super().__init__()
            self.voice = FakeVoice()
            self.bot = FakeBot()

            class _Chat:
                type = "private"
                id = user_id

            class _U:
                pass

            self.chat = _Chat()
            self.from_user = _U()
            self.from_user.id = user_id
            self.from_user.username = "student"

    process_text_query_calls = []

    async def fake_process_text_query(message, state, text):
        process_text_query_calls.append(text)

    original_process_text_query = handlers._process_text_query
    original_transcribe = ai.transcribe_audio
    handlers._process_text_query = fake_process_text_query

    try:
        # Hallucinated transcription -> never reaches _process_text_query,
        # but the recognized text is still shown to the user.
        async def fake_transcribe_hallucination(audio_bytes):
            return "Субтитры создавал DimaTorzok"

        handlers.ai.transcribe_audio = fake_transcribe_hallucination
        process_text_query_calls.clear()
        voice_msg = FakeVoiceMessage(7002)
        await handlers.handle_voice_message(voice_msg, None)
        check("hallucinated voice: _process_text_query NOT called (no model call/billing)", process_text_query_calls == [])
        check(
            "hallucinated voice: recognized text still shown to the user",
            any("DimaTorzok" in s["text"] for s in voice_msg.sent),
        )
        check(
            "hallucinated voice: user is asked to re-record",
            any("ещё раз" in s["text"].lower() or "микрофон" in s["text"].lower() for s in voice_msg.sent),
        )

        # Normal transcription -> processed exactly as before.
        async def fake_transcribe_normal(audio_bytes):
            return "Реши уравнение 2х плюс 3 равно 7"

        handlers.ai.transcribe_audio = fake_transcribe_normal
        process_text_query_calls.clear()
        voice_msg2 = FakeVoiceMessage(7003)
        await handlers.handle_voice_message(voice_msg2, None)
        check("normal voice: _process_text_query WAS called", process_text_query_calls == ["Реши уравнение 2х плюс 3 равно 7"])
        check(
            "normal voice: recognized text shown to the user too",
            any("2х плюс 3" in s["text"] for s in voice_msg2.sent),
        )
    finally:
        handlers._process_text_query = original_process_text_query
        handlers.ai.transcribe_audio = original_transcribe


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
