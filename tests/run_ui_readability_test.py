import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "ui_readability_test.db")
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
    def __init__(self, uid, text=None, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class FakeCallbackMessage:
    def __init__(self):
        self.edits = []
        self.sent = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))


class FakeCallback:
    def __init__(self, uid, data, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.data = data
        self.message = FakeCallbackMessage()
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


async def run():
    # ==================================================================
    # Admin chatlog readability
    # ==================================================================
    uid = 80001
    db._ensure_player(uid, "chatty")
    db.log_message(uid, "chatty", "user", "привет, реши 2 < 3 & true")  # raw '<'/'&'
    db.log_message(uid, "chatty", "assistant", "конечно! 2 < 3 действительно верно")
    rows = db.get_recent_chat(uid, 20)
    text = handlers._chatlog_text(str(uid), rows)

    check("chatlog: has a bold speaker label for the user turn", "<b>🙋 Пользователь</b>" in text)
    check("chatlog: has a bold speaker label for the bot turn", "<b>🤖 Бот</b>" in text)
    check("chatlog: turns are separated by a blank line, not run together", "\n\n" in text)
    check("chatlog: raw '<' in user content is escaped, not left as a literal tag", "&lt; 3" in text)
    check("chatlog: raw '&' in user content is escaped", "&amp;" in text)
    check("chatlog: no unescaped '< 3' survives (would break HTML parsing)", "< 3" not in text)

    # The whole thing must actually be valid enough to send as HTML — the
    # old version used parse_mode=None specifically to dodge this risk;
    # confirm the escaping makes that no longer necessary by round-tripping
    # through the same tag-validation _sanitize_model_html uses elsewhere
    # (any stray '<'/'>' that survived unescaped would show up as a
    # "tag" token here).
    import re as _re

    tag_like = _re.findall(r"<[^>]*>", text)
    allowed_tags = {"<b>", "</b>", "<i>", "</i>"}
    check(
        "chatlog: every '<...>' in the output is one of our own <b>/<i> tags, nothing from raw content",
        all(t in allowed_tags for t in tag_like),
    )

    # Admin-facing send sites no longer force parse_mode=None (HTML is safe
    # now that the content is escaped).
    cb = FakeCallback(9999, f"admin:chatlog:{uid}:0")
    await handlers.cb_admin_chatlog(cb)
    check("cb_admin_chatlog: sent via edit (HTML default, no parse_mode override)", len(cb.message.edits) == 1)
    # _edit_or_send always passes parse_mode explicitly (its own default is
    # "HTML") — the fix is that this call site no longer overrides it to
    # None, not that the kwarg is absent.
    check("cb_admin_chatlog: parse_mode is HTML, not forced to None anymore", cb.message.edits[0][1].get("parse_mode") == "HTML")

    msg = FakeMessage(9999, f"/chatlog {uid}")
    await handlers.cmd_chatlog(msg)
    check("cmd_chatlog: sent one reply", len(msg.sent) == 1)
    check("cmd_chatlog: parse_mode key is absent too (HTML default)", "parse_mode" not in msg.sent[0][1])

    # Empty chat still handled cleanly (no crash on an empty rows list).
    uid_empty = 80002
    db._ensure_player(uid_empty, "quiet")
    cb_empty = FakeCallback(9999, f"admin:chatlog:{uid_empty}:0")
    await handlers.cb_admin_chatlog(cb_empty)
    check("cb_admin_chatlog: empty history shows a friendly message, no crash", any("Нет сообщений" in t for t, _ in cb_empty.message.edits))

    # ==================================================================
    # Image generation: buttons after a result let the user continue
    # without hunting for the original menu message.
    # ==================================================================
    kb = handlers.image_actions_keyboard()
    all_buttons = [b for row in kb.inline_keyboard for b in row]
    callback_data = [b.callback_data for b in all_buttons]

    check("image keyboard: still has 'Ещё раз'", "img:retry" in callback_data)
    check("image keyboard: still has 'Изменить'", "img:edit" in callback_data)
    check("image keyboard: has a new 'Новая картинка' button", "img:new" in callback_data)
    check("image keyboard: has a 'Меню' button reusing the existing menu:back handler", "menu:back" in callback_data)

    # img:new starts a fresh prompt without disturbing the remembered
    # last_image_prompt/mode (so "Ещё раз"/"Изменить" under the ORIGINAL
    # photo still work if the user backs out).
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

    state = FakeState()
    await state.update_data(last_image_prompt="кот в шляпе", last_image_mode="generate")

    cb_new = FakeCallback(9001, "img:new")
    await handlers.cb_image_new(cb_new, state)
    check("img:new: answers the callback", cb_new.answered)
    check("img:new: sets the FSM state to waiting_for_image_prompt", state.state == handlers.Form.waiting_for_image_prompt)
    check("img:new: sends the image intro text (ready to type a fresh description)", any(handlers.IMAGE_INTRO_TEXT in t for t, _ in cb_new.message.sent))
    check(
        "img:new: does NOT clear the remembered last prompt (Ещё раз/Изменить under the old photo still work)",
        (await state.get_data()).get("last_image_prompt") == "кот в шляпе",
    )

    # HELP_TEXT mentions the new buttons, so the feature is discoverable
    # without having to find it by trial and error.
    check("HELP_TEXT mentions 'Новая картинка'", "Новая картинка" in handlers.HELP_TEXT)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
