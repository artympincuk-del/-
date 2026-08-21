import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "admin_help_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import handlers  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    id = 9999
    type = "private"


class FakeMessage:
    def __init__(self, uid, text):
        self.from_user = FakeUser(uid)
        self.chat = FakeChat()
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))


class FakeTgMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, uid, data):
        self.from_user = FakeUser(uid)
        self.data = data
        self.message = FakeTgMessage()
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


async def run():
    # ------------------------------------------------------------------
    # Main /admin menu now has a 4th button linking to the full command
    # reference, and its text no longer dumps every command inline.
    # ------------------------------------------------------------------
    msg = FakeMessage(9999, "/admin")
    await handlers.cmd_admin(msg)
    check("/admin: sent a message", len(msg.sent) == 1)
    kb = msg.sent[0][1]["reply_markup"]
    button_texts = [b.text for row in kb.inline_keyboard for b in row]
    button_data = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("/admin menu: has 'Все команды' button", any("команды" in t.lower() for t in button_texts))
    check("/admin menu: help button routes to admin:help", "admin:help" in button_data)
    check("/admin menu: still has Статистика/Воронка/Пользователи buttons", "admin:stats" in button_data and "admin:funnel" in button_data and "admin:users:0" in button_data)

    # Non-admin gets nothing.
    msg2 = FakeMessage(1, "/admin")
    await handlers.cmd_admin(msg2)
    check("/admin: non-admin gets no reply", len(msg2.sent) == 0)

    # ------------------------------------------------------------------
    # Help index screen: one button per category, back to menu.
    # ------------------------------------------------------------------
    cb = FakeCallback(9999, "admin:help")
    await handlers.cb_admin_help(cb)
    check("admin:help: answered the callback", cb.answered)
    check("admin:help: edited the message", len(cb.message.edits) == 1)
    help_kb = cb.message.edits[0][1]["reply_markup"]
    help_data = [b.callback_data for row in help_kb.inline_keyboard for b in row]
    expected_categories = {f"admin:help:{k}" for k in handlers.ADMIN_HELP_CATEGORIES}
    check("admin:help: one button per category", expected_categories.issubset(set(help_data)))
    check("admin:help: has a way back to the main menu", "admin:menu" in help_data)

    # Non-admin gets no edit.
    cb_bad = FakeCallback(1, "admin:help")
    await handlers.cb_admin_help(cb_bad)
    check("admin:help: non-admin gets no edit", len(cb_bad.message.edits) == 0)

    # ------------------------------------------------------------------
    # Every category screen renders, mentions its commands, and every
    # admin-only command mentioned actually exists as a registered handler.
    # ------------------------------------------------------------------
    all_admin_commands = {
        "users", "grant", "chatlog", "notes_of", "refund", "payers",
        "promo_add", "promo_off", "promo_list", "promo_stat", "promo_users", "promo_owner",
        "promo_token", "backup",
    }
    mentioned = set()
    for key, category in handlers.ADMIN_HELP_CATEGORIES.items():
        cb = FakeCallback(9999, f"admin:help:{key}")
        await handlers.cb_admin_help_category(cb)
        check(f"admin:help:{key}: edited the message", len(cb.message.edits) == 1)
        body = cb.message.edits[0][0]
        check(f"admin:help:{key}: title present", category["label"].split(" ", 1)[1] in body or category["label"] in body)
        cat_kb = cb.message.edits[0][1]["reply_markup"]
        cat_data = [b.callback_data for row in cat_kb.inline_keyboard for b in row]
        check(f"admin:help:{key}: back button returns to help index", "admin:help" in cat_data)
        import re

        for cmd in re.findall(r"/([a-z_]+)", body):
            mentioned.add(cmd)

    check("help text: every admin command is documented somewhere", all_admin_commands.issubset(mentioned))
    non_command_artifacts = {"b", "code", "premium"}  # HTML closing tags + "free/premium" prose, not commands
    check(
        "help text: doesn't document nonexistent commands",
        (mentioned - non_command_artifacts).issubset(all_admin_commands | {"mypromo", "promo", "remember"}),
    )

    # Unknown category key doesn't crash.
    cb_unknown = FakeCallback(9999, "admin:help:doesnotexist")
    await handlers.cb_admin_help_category(cb_unknown)
    check("admin:help:<bad key>: no crash, no edit", len(cb_unknown.message.edits) == 0)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
