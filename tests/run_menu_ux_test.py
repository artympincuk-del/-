import asyncio
import os
import re
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "menu_ux_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from aiogram.types import ReplyKeyboardRemove  # noqa: E402

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


class FakeChat:
    type = "private"

    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self, uid, text=None, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.chat = FakeChat(uid)
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class FakeState:
    def __init__(self):
        self.cleared = False
        self.state = "SOME_STATE"

    async def clear(self):
        self.cleared = True
        self.state = None

    async def set_state(self, state):
        self.state = state


async def run():
    # ==================================================================
    # Admin help commands are bold, not <code> — Telegram doesn't offer
    # tap-to-fill for text inside <code>/<pre>.
    # ==================================================================
    for key, category in handlers.ADMIN_HELP_CATEGORIES.items():
        body = category["text"]
        commands_in_body = set(re.findall(r"/([a-z_]+)", body))
        for cmd in commands_in_body:
            check(
                f"admin help [{key}]: /{cmd} is not wrapped in <code> (stays tappable)",
                f"<code>/{cmd}" not in body,
            )
        check(f"admin help [{key}]: has at least one bold command", "<b>/" in body)

    # ==================================================================
    # /start no longer pins a persistent reply-keyboard duplicating /menu —
    # it clears any pre-existing one instead.
    # ==================================================================
    msg = FakeMessage(70001, "/start")
    state = FakeState()
    await handlers.cmd_start(msg, state)
    # Three messages now: the shortened welcome, the menu, and the
    # "Показать пример" prompt (Правка 4.5). The reply-keyboard removal has
    # to ride on its own message — a message carries one reply_markup, so
    # it can't both clear a reply keyboard and show an inline button.
    check("/start: sends welcome + menu + example prompt", len(msg.sent) == 3)
    welcome_kwargs = msg.sent[0][1]
    check(
        "/start: welcome message clears any old reply-keyboard (ReplyKeyboardRemove)",
        isinstance(welcome_kwargs.get("reply_markup"), ReplyKeyboardRemove),
    )
    check("/start: welcome text no longer references the removed 'Кнопка «📋 Меню» внизу'", "внизу" not in msg.sent[0][0])
    menu_kwargs = msg.sent[1][1]
    check("/start: the menu message itself still uses the normal inline main menu", menu_kwargs.get("reply_markup") is not None)

    # ==================================================================
    # The legacy button handler still exists as a compatibility shim, so
    # anyone who still has the old reply-keyboard pinned doesn't get their
    # tap misread as a chat question.
    # ==================================================================
    msg_legacy = FakeMessage(70002, handlers.PERSISTENT_MENU_BTN)
    state_legacy = FakeState()
    await handlers.btn_legacy_persistent_menu(msg_legacy, state_legacy)
    check("legacy '📋 Меню' tap: still opens the menu instead of being treated as a chat message", len(msg_legacy.sent) == 1 and "Меню" in msg_legacy.sent[0][0])

    # No new persistent-keyboard API is exposed anymore.
    check("persistent_keyboard() no longer exists", not hasattr(handlers, "persistent_keyboard"))


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
