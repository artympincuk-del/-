import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "code_tags_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import ai  # noqa: E402
from bot import handlers  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeChat:
    type = "private"
    id = 1


class FakeMessage:
    """Captures what _send_long would really hand to Telegram — the whole
    point is to test the full pipeline (LaTeX → Markdown → unwrap → escape),
    not _unwrap_plain_code in isolation."""

    def __init__(self):
        self.chat = FakeChat()
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


async def send(text: str) -> str:
    msg = FakeMessage()
    await handlers._send_long(msg, text)
    return msg.sent[0]


async def run():
    # ==================================================================
    # Правка 1: <code> comes off ordinary Russian words
    # ==================================================================
    out = await send("выключи <code>кнопку</code> или <code>выключатель</code>")
    check("plain Russian words: <code> removed", "<code>" not in out)
    check("plain Russian words: the words themselves survive", "кнопку" in out and "выключатель" in out)
    check("plain Russian words: reaches the user as ordinary text", out == "выключи кнопку или выключатель")

    check(
        "a multi-word Russian phrase is unwrapped too",
        await send("нажми <code>красную кнопку</code>") == "нажми красную кнопку",
    )
    check(
        "a hyphenated Russian word is unwrapped",
        await send("это <code>из-за</code> тебя") == "это из-за тебя",
    )
    check(
        "Markdown backticks around a Russian word end up unwrapped as well",
        await send("выключи `кнопку`") == "выключи кнопку",
    )

    # ==================================================================
    # ...and stays on everything that is actually code or a formula
    # ==================================================================
    kept = {
        "formula with digits and operators": "<code>x^2 + 1</code>",
        "bot command": "<code>/start</code>",
        "function call": "<code>print(x)</code>",
        "latin identifier": "<code>user_id</code>",
        "single latin variable": "<code>x</code>",
        "digits only": "<code>42</code>",
        "russian word with a digit": "<code>шаг1</code>",
        "russian word with brackets": "<code>функция(х)</code>",
        "mixed russian and latin": "<code>переменная x</code>",
        "russian with an underscore": "<code>имя_поля</code>",
        "comparison": "<code>a &lt; b</code>",
    }
    for label, payload in kept.items():
        out = await send(f"смотри {payload}")
        check(f"kept in <code>: {label}", "<code>" in out and "</code>" in out)

    # ==================================================================
    # <pre> is never touched, including a <code> nested inside one
    # ==================================================================
    pre_block = "<pre>строка один\nстрока два\nстрока три</pre>"
    out = await send(pre_block)
    check("<pre> with multi-line Russian content is unchanged", out == pre_block)

    nested = "<pre><code>привет</code></pre>"
    out = await send(nested)
    check("<code> nested inside <pre> is left alone", out == nested)

    mixed = "текст <code>кнопку</code> и блок <pre>строка\nещё строка</pre> конец"
    out = await send(mixed)
    check("mixed: the loose word is unwrapped", "<code>кнопку</code>" not in out)
    check("mixed: the <pre> block still stands", "<pre>строка\nещё строка</pre>" in out)

    # A multi-line <code> (not inside <pre>) is left alone too — the
    # pattern deliberately has no newline in its character class.
    multiline_code = "<code>первая\nвторая</code>"
    out = await send(multiline_code)
    check("multi-line <code> content is not unwrapped", "<code>" in out)

    # Unwrapping must not break the escaping that runs after it.
    out = await send("сравни <code>кнопку</code> и 3 < 5")
    check("escaping still runs after unwrapping (stray '<' escaped)", "3 &lt; 5" in out)

    # ==================================================================
    # Правка 2: the system prompt asks a question instead of guessing
    # ==================================================================
    prompt = ai.SYSTEM_PROMPT
    check("system prompt: tells the model to ask when the request is unclear", "непонятен" in prompt)
    check("system prompt: mentions likely typos", "опечатку" in prompt)
    check("system prompt: mentions several different readings", "прочтени" in prompt)
    check("system prompt: asks for ONE question, not a list", "ОДИН" in prompt and "не список" in prompt.lower())
    check(
        "system prompt: guards against turning into a question-asker (answer straight when clear)",
        "сразу отвечай" in prompt and "настоящей неоднозначности" in prompt,
    )
    check(
        "system prompt: a photo/file follow-up with no attachment asks for the file",
        "ни фото, ни файла нет" in prompt and "попроси прислать" in prompt,
    )
    check(
        "system prompt: the existing contradiction rule is still there (not replaced)",
        "противоречив" in prompt and "уверенного неправильного" in prompt,
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
