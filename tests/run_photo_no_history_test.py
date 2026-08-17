import asyncio
import io
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "photo_history_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from PIL import Image  # noqa: E402

from bot import db  # noqa: E402
from bot import handlers  # noqa: E402
from bot.config import MAX_HISTORY_TURNS, VISION_MODEL  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- a real tiny JPEG, since _prepare_image actually opens it with PIL ---
_buf = io.BytesIO()
Image.new("RGB", (40, 40), color=(200, 50, 50)).save(_buf, format="JPEG")
FAKE_PHOTO_BYTES = _buf.getvalue()


class FakeUser:
    def __init__(self, user_id, username="student"):
        self.id = user_id
        self.username = username


class FakeChat:
    type = "private"
    id = 5001


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeDownload:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeBot:
    async def send_chat_action(self, chat_id, action):
        pass

    async def download(self, file_id):
        return FakeDownload(FAKE_PHOTO_BYTES)


class FakeSentMessage:
    def __init__(self, text):
        self.text = text

    async def edit_text(self, text, **kwargs):
        self.text = text

    async def delete(self):
        pass


class FakeMessage:
    def __init__(self, user_id, caption):
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat()
        self.caption = caption
        self.photo = [FakePhotoSize("file-abc")]
        self.bot = FakeBot()
        self.sent_texts = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.sent_texts.append(text)
        return FakeSentMessage(text)


ask_ai_calls = []


async def fake_ask_ai(
    history, content, model, notes=None, reasoning_effort=None, max_tokens=4096, enable_search=False
):
    ask_ai_calls.append({"history": history, "content": content, "model": model})
    if model == VISION_MODEL:
        return "2x + 3 = 7, найти x", []
    return "x = 2\n\n✅ Проверка: 2*2+3=7", []


handlers.ai.ask_ai = fake_ask_ai


# 10 turns — matches MAX_HISTORY_TURNS, i.e. what a genuinely active chat
# would have riding along on every request before this fix.
SEED_TURNS = [
    ("Привет, помоги с домашкой по алгебре", "Конечно, присылай задание, разберём по шагам!"),
    ("Что такое квадратное уравнение?", "Уравнение вида ax^2+bx+c=0, где a≠0. Решается через дискриминант D=b^2-4ac, корни x=(-b±√D)/(2a)."),
    ("А если дискриминант отрицательный?", "Тогда действительных корней нет — уравнение решений в множестве R не имеет, только комплексные."),
    ("Спасибо, теперь понятно. А что такое дискриминант геометрически?", "Он показывает, сколько раз парабола y=ax^2+bx+c пересекает ось X: 2 раза, 1 раз (касание) или 0 раз."),
    ("Ясно. Давай теперь про системы уравнений", "Хорошо, какой метод интересует — подстановка, сложение или метод Крамера?"),
    ("Расскажи про метод подстановки", "Выражаешь одну переменную через другую в одном уравнении, подставляешь во второе — получаешь уравнение с одной переменной."),
    ("А метод сложения?", "Умножаешь уравнения на коэффициенты так, чтобы при сложении одна переменная сократилась, затем решаешь оставшееся уравнение."),
    ("Какой метод быстрее для больших систем?", "Для систем с двумя-тремя переменными оба сопоставимы, для больших — матричные методы вроде метода Крамера или Гаусса."),
    ("Хорошо, а теперь про производные объясни", "Производная — это скорость изменения функции. Для степенной функции x^n производная равна n*x^(n-1)."),
    ("Спасибо, теперь понятно", "Пожалуйста! Обращайся, если будут ещё вопросы по алгебре и анализу."),
]


def seed_history(user_id):
    for user_text, assistant_text in SEED_TURNS:
        db.append_dialog_turn(user_id, user_text, assistant_text, MAX_HISTORY_TURNS)


async def run():
    user_id = 5001
    seed_history(user_id)

    # ------------------------------------------------------------------
    # 1. Photo with no caption at all -> no history on either stage.
    # ------------------------------------------------------------------
    ask_ai_calls.clear()
    msg = FakeMessage(user_id, caption=None)
    await handlers._handle_photo_message_locked(msg, None)
    check("no-caption photo: exactly 2 ask_ai calls (vision + solve)", len(ask_ai_calls) == 2)
    check("no-caption photo: vision stage got no history", ask_ai_calls[0]["history"] == [])
    check("no-caption photo: solve stage got no history", ask_ai_calls[1]["history"] == [])

    # Size of what actually goes out now for the common no-reference case:
    # history contributes 0 chars.
    size_without_history = len(ask_ai_calls[1]["content"])

    # ------------------------------------------------------------------
    # 2. Photo captioned "реши" (a plain task instruction, no reference
    #    to earlier conversation) -> still no history.
    # ------------------------------------------------------------------
    ask_ai_calls.clear()
    msg = FakeMessage(user_id, caption="реши")
    await handlers._handle_photo_message_locked(msg, None)
    check("'реши' caption: vision stage got no history", ask_ai_calls[0]["history"] == [])
    check("'реши' caption: solve stage got no history", ask_ai_calls[1]["history"] == [])

    # ------------------------------------------------------------------
    # 3. Photo captioned "а тут так же?" (explicit reference to earlier
    #    conversation) -> solve stage gets real history. Uses a separate,
    #    identically-seeded user so the "before this fix, history always
    #    rode along" comparison in step 6 is apples-to-apples with step 1.
    # ------------------------------------------------------------------
    user_id_3 = 5002
    seed_history(user_id_3)
    expected_history = db.get_dialog_history(user_id_3, MAX_HISTORY_TURNS)

    ask_ai_calls.clear()
    msg = FakeMessage(user_id_3, caption="а тут так же?")
    await handlers._handle_photo_message_locked(msg, None)
    check("'а тут так же?' caption: vision stage still gets no history", ask_ai_calls[0]["history"] == [])
    check("'а тут так же?' caption: solve stage DOES get history", len(ask_ai_calls[1]["history"]) > 0)
    check(
        "'а тут так же?' caption: solve stage history matches db.get_dialog_history",
        ask_ai_calls[1]["history"] == expected_history,
    )

    size_with_history = sum(len(t["content"]) for t in ask_ai_calls[1]["history"]) + len(
        ask_ai_calls[1]["content"]
    )

    # ------------------------------------------------------------------
    # 4. After photo processing, a new dialog_history entry about the
    #    photo exists.
    # ------------------------------------------------------------------
    history_after = db.get_dialog_history(user_id, MAX_HISTORY_TURNS)
    photo_entries = [t for t in history_after if t["role"] == "user" and t["content"].startswith("[Фото]")]
    check("dialog_history now contains a [Фото] entry", len(photo_entries) > 0)

    # ------------------------------------------------------------------
    # 5. The next TEXT question still gets full history, including the
    #    photo entry (text pipeline is untouched).
    # ------------------------------------------------------------------
    ask_ai_calls.clear()

    class FakeTextMessage(FakeMessage):
        def __init__(self, user_id, text):
            super().__init__(user_id, caption=None)
            self.text = text

    text_msg = FakeTextMessage(user_id, "а почему именно так?")
    await handlers._answer_text_query(text_msg, None, "а почему именно так?", user_id, "student")
    check("follow-up text question: exactly 1 ask_ai call", len(ask_ai_calls) == 1)
    followup_history = ask_ai_calls[0]["history"]
    check("follow-up text question: got non-empty history", len(followup_history) > 0)
    check(
        "follow-up text question: history includes the photo entry",
        any(t["content"].startswith("[Фото]") for t in followup_history),
    )

    # ------------------------------------------------------------------
    # 6. Request-size comparison: default (no-reference) photo request
    #    vs. what used to be sent unconditionally before this fix
    #    (equivalent to today's "references history" branch).
    # ------------------------------------------------------------------
    reduction_pct = 100 * (1 - size_without_history / size_with_history)
    print()
    print(f"Request size WITHOUT history (after fix, default case): {size_without_history} chars")
    print(f"Request size WITH full history (before fix, unconditional): {size_with_history} chars")
    print(f"Reduction: {reduction_pct:.0f}%")
    check(
        "request size dropped noticeably with the fix (>40% smaller)",
        size_without_history < size_with_history * 0.6,
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
