import asyncio
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "safety_onboarding_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
os.environ["AITUNNEL_API_KEY"] = "test-aitunnel-key"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import ai, db  # noqa: E402
from bot import handlers  # noqa: E402
from bot.config import DAILY_FREE_IMAGE_MESSAGES, MODEL_TRIAL_TOTAL_FREE  # noqa: E402

GEMINI = "gemini-3.5-flash-lite"

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


class FakeUser:
    def __init__(self, uid, username="tester"):
        self.id = uid
        self.username = username


class FakeChat:
    type = "private"

    def __init__(self, chat_id):
        self.id = chat_id


class FakeBot:
    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        pass


class FakeMessage:
    def __init__(self, uid, text=None, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.chat = FakeChat(uid)
        self.bot = FakeBot()
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    async def delete(self):
        pass

    async def edit_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


class FakeCallbackMessage:
    def __init__(self, uid):
        self.chat = FakeChat(uid)
        self.bot = FakeBot()
        self.sent = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, uid, data, username="tester"):
        self.from_user = FakeUser(uid, username)
        self.data = data
        self.message = FakeCallbackMessage(uid)
        self.bot = FakeBot()
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


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

    async def clear(self):
        self.data = {}
        self.state = None


async def run():
    # ==================================================================
    # Правка 1: image content filter
    # ==================================================================
    blocked_prompts = [
        "нарисуй голую девушку",
        "обнажённая девушка на пляже",
        "draw a nude woman",
        "naked girl",
        "порно картинка",
        "эротика в стиле аниме",
        "девушка топлес",
        "нарисуй сиськи",
        "hentai girl",
        "нарисуй голую девочку",          # ребёнок + нагота
        "школьница в нижнем белье",        # по отдельности не блок, вместе — блок
        "sexy teen in bikini",             # то же самое по-английски
        "г о л а я  девушка",              # разрядка
        "п.о.р.н.о",                       # точки между буквами
        "гoлaя девушка",                   # латинские o и a внутри слова
        "расчленённое тело",               # графическое насилие
    ]
    for prompt in blocked_prompts:
        check(f"filter blocks: {prompt!r}", ai.image_prompt_is_blocked(prompt))

    allowed_prompts = [
        "нарисуй кота в шляпе",
        "закат над горами в стиле акварели",
        "a pug on a hoverboard",
        "Бородинское сражение",            # война/история — не блок
        "схема анализа крови",             # «анал» внутри «анализа»
        "члены семьи за столом",           # «член» внутри «члены семьи»
        "голова робота",                   # «гол» внутри «голова»
        "футболист забивает гол",          # «гол» как отдельное слово, но не «голый»
        "аналог старого логотипа",         # «anal» внутри «аналог»
        "девочка с воздушным шариком",     # ребёнок без сексуализации — не блок
        "нарисуй кота с надписью бл**ь",   # мат без содержания — не блок (Правка 1.5)
        "блядский кот в очках",            # то же: лексика, а не содержание
    ]
    for prompt in allowed_prompts:
        check(f"filter allows: {prompt!r}", not ai.image_prompt_is_blocked(prompt))

    # Блокировка происходит ДО списания лимита.
    u_img = 90001
    db._ensure_player(u_img, "imgtester")
    before = db.get_status(u_img, "imgtester")["images_used_today"]
    msg_blocked = FakeMessage(u_img, "нарисуй голую девушку")
    await handlers._process_image_request(
        msg_blocked, FakeState(), "нарисуй голую девушку", u_img, "imgtester"
    )
    after = db.get_status(u_img, "imgtester")["images_used_today"]
    check("blocked image request: quota NOT consumed", after == before)
    check("blocked image request: user gets a short refusal", any(msg_blocked.sent))
    refusal = msg_blocked.sent[0][0]
    check("blocked image request: refusal does not list what's forbidden", "порно" not in refusal.lower())
    check("blocked image request: refusal has no moralizing lecture", len(refusal) < 120)

    stats = db.get_blocked_image_stats()
    check("blocked image request: counted in admin stats (today)", stats["blocked_today"] == 1)
    check("blocked image request: counted as one distinct user", stats["blocked_users_today"] == 1)

    # Второй заблокированный запрос от того же человека: +1 к счётчику,
    # но не +1 к числу людей.
    msg_blocked2 = FakeMessage(u_img, "porn picture")
    await handlers._process_image_request(
        msg_blocked2, FakeState(), "porn picture", u_img, "imgtester"
    )
    stats2 = db.get_blocked_image_stats()
    check("second block from the same user: count grows", stats2["blocked_today"] == 2)
    check("second block from the same user: distinct-user count stays 1", stats2["blocked_users_today"] == 1)

    # Безобидный запрос с матом действительно доходит до генерации.
    generated = []

    async def fake_generate_image(prompt, **kwargs):
        generated.append(prompt)
        return b"\xff\xd8\xff-fake-jpeg"

    original_generate = ai.generate_image
    ai.generate_image = fake_generate_image

    class FakePhotoMessage(FakeMessage):
        async def answer_photo(self, photo, **kwargs):
            self.sent.append(("[photo]", kwargs))
            return self

    u_ok = 90002
    db._ensure_player(u_ok, "oktester")
    msg_ok = FakePhotoMessage(u_ok, "нарисуй кота с надписью бл**ь")
    await handlers._process_image_request(
        msg_ok, FakeState(), "нарисуй кота с надписью бл**ь", u_ok, "oktester"
    )
    check("harmless prompt with profanity actually reaches the generator", len(generated) == 1)
    check("harmless prompt with profanity: quota WAS consumed (normal path)", db.get_status(u_ok, "oktester")["images_used_today"] == 1)
    ai.generate_image = original_generate

    # Defense in depth: даже при прямом вызове ai.generate_image фильтр держит.
    raised = False
    try:
        await ai.generate_image("нарисуй голую девушку")
    except ai.AIError:
        raised = True
    check("ai.generate_image itself refuses a blocked prompt (defense in depth)", raised)

    # ==================================================================
    # Правка 2: английские отказы заменяются русскими
    # ==================================================================
    check(
        "whole-answer English refusal is replaced with Russian",
        ai._localize_refusal("I'm sorry, but I can't help with that.") == ai.REFUSAL_RU_TEXT,
    )
    check(
        "refusal with different casing/spacing is still caught",
        ai._localize_refusal("  I cannot help with that  ") == ai.REFUSAL_RU_TEXT,
    )
    check(
        "another phrasing from the list is caught",
        ai._localize_refusal("Sorry, I can't help with that.") == ai.REFUSAL_RU_TEXT,
    )
    meaningful = (
        "Вот решение задачи: x = 5. Кстати, если бы ты спросил про что-то "
        "запрещённое, я бы ответил «I can't help with that», но здесь всё в порядке."
    )
    check(
        "a refusal phrase INSIDE a meaningful answer is left untouched",
        ai._localize_refusal(meaningful) == meaningful,
    )
    normal = "Ответ: 42."
    check("an ordinary answer is untouched", ai._localize_refusal(normal) == normal)

    # ==================================================================
    # Правка 3: сложные задачи
    # ==================================================================
    check(
        "system prompt requires checking the condition for contradictions",
        "противоречив" in ai.SYSTEM_PROMPT and "не хватает" in ai.SYSTEM_PROMPT,
    )
    check(
        "system prompt says an honest 'contradictory' beats a confident wrong answer",
        "уверенного неправильного" in ai.SYSTEM_PROMPT,
    )

    check("_looks_like_task: digits", handlers._looks_like_task("сколько будет 2+2"))
    check("_looks_like_task: 'реши'", handlers._looks_like_task("реши уравнение"))
    check("_looks_like_task: 'докажи'", handlers._looks_like_task("докажи теорему"))
    check("_looks_like_task: ends with a question mark", handlers._looks_like_task("а почему небо голубое?"))
    check("_looks_like_task: plain statement is not a task", not handlers._looks_like_task("привет, как дела"))

    kb_task = handlers.quick_actions_keyboard(task_like=True, answered_model="openai/gpt-oss-20b")
    data_task = [b.callback_data for row in kb_task.inline_keyboard for b in row]
    check("task answer: has 'Перепроверить'", "qa:verify" in data_task)
    check("task answer (non-smart model): has 'Решить умной моделью'", "qa:smart" in data_task)

    kb_smart = handlers.quick_actions_keyboard(task_like=True, answered_model=GEMINI)
    data_smart = [b.callback_data for row in kb_smart.inline_keyboard for b in row]
    check("task already answered BY the smart model: 'Перепроверить' still offered", "qa:verify" in data_smart)
    check("task already answered BY the smart model: no pointless 'Решить умной моделью'", "qa:smart" not in data_smart)

    kb_plain = handlers.quick_actions_keyboard(task_like=False, answered_model="openai/gpt-oss-20b")
    data_plain = [b.callback_data for row in kb_plain.inline_keyboard for b in row]
    check("non-task answer: no task buttons", "qa:verify" not in data_plain and "qa:smart" not in data_plain)
    check("non-task answer: ordinary quick actions still there", "qa:detail" in data_plain and "qa:share" in data_plain)

    # "Перепроверить" реально ходит в модель и тратит обычный лимит.
    groq_calls = []
    aitunnel_calls = []

    async def fake_groq_create(**kwargs):
        groq_calls.append(kwargs)
        return FakeResponse("перепроверил: ответ верный")

    async def fake_aitunnel_create(**kwargs):
        aitunnel_calls.append(kwargs)
        return FakeResponse("умная модель: ответ 6")

    ai._client.chat.completions.create = fake_groq_create
    ai._aitunnel_client.chat.completions.create = fake_aitunnel_create

    u_task = 90003
    db._ensure_player(u_task, "tasker")
    db.append_dialog_turn(u_task, "реши 2x+3=7", "x=2", 10)
    used_before = db.get_status(u_task, "tasker")["used_today"]
    cb_verify = FakeCallback(u_task, "qa:verify")
    await handlers.cb_quick_action(cb_verify, FakeState())
    check("'Перепроверить': asked the model", len(groq_calls) >= 1)
    check(
        "'Перепроверить': spent one ordinary request",
        db.get_status(u_task, "tasker")["used_today"] == used_before + 1,
    )
    sent_instruction = groq_calls[-1]["messages"][-1]["content"]
    check("'Перепроверить': the instruction asks to FIND a mistake, not to confirm", "найди в нём ошибку" in sent_instruction)

    # "Решить умной моделью" уходит на платную модель и тратит её счётчик.
    u_smart = 90004
    db._ensure_player(u_smart, "smarter")
    db.append_dialog_turn(u_smart, "реши 3x=9", "x=3", 10)
    aitunnel_calls.clear()
    trial_before = db.get_model_trial_usage(u_smart, GEMINI)
    cb_smart = FakeCallback(u_smart, "qa:smart")
    await handlers.cb_quick_action_smart(cb_smart, FakeState())
    check("'Решить умной моделью': went to the paid model, not Groq", len(aitunnel_calls) == 1)
    check(
        "'Решить умной моделью': spent one of the smart model's trial uses",
        db.get_model_trial_usage(u_smart, GEMINI) == trial_before + 1,
    )
    check(
        "'Решить умной моделью': did NOT change the user's stored preference",
        db.get_status(u_smart, "smarter")["model_choice"] != "gemini",
    )

    # Когда пробные к умной модели кончились — честный остаток, а не тишина.
    u_out = 90005
    db._ensure_player(u_out, "outoftrial")
    db.append_dialog_turn(u_out, "реши 5x=25", "x=5", 10)
    for _ in range(MODEL_TRIAL_TOTAL_FREE):
        db.try_consume_model_trial(u_out, GEMINI, MODEL_TRIAL_TOTAL_FREE)
    aitunnel_calls.clear()
    cb_out = FakeCallback(u_out, "qa:smart")
    await handlers.cb_quick_action_smart(cb_out, FakeState())
    check("'Решить умной моделью' with no trial left: does NOT call the paid model", aitunnel_calls == [])
    denial = " ".join(t for t, _ in cb_out.message.sent)
    check("'Решить умной моделью' with no trial left: says what ran out", "пробн" in denial.lower())
    check(
        "'Решить умной моделью' with no trial left: shows the honest count",
        f"{MODEL_TRIAL_TOTAL_FREE}" in denial,
    )

    # ==================================================================
    # Правка 4: первый экран
    # ==================================================================
    welcome = handlers.WELCOME_TEXT
    check("welcome: opens with a direct call to action", welcome.strip().startswith("📸"))
    check("welcome: says what to send", "фото" in welcome and "вопрос" in welcome)
    check("welcome: KEEPS the conversation-storage notice", "переписка сохраняется" in welcome.lower())
    check("welcome: storage notice is one short sentence, not a paragraph", welcome.lower().count("переписка сохраняется") == 1)
    check("welcome: limits mentioned without a free/premium split", "премиум" not in welcome.lower())
    check("welcome: no model talk at all", "модел" not in welcome.lower())
    check("welcome: noticeably shorter than the old one (which ran ~600 chars)", len(welcome) < 420)
    check("welcome: points at the example button", "пример" in welcome.lower())

    u_new = 90006
    msg_start = FakeMessage(u_new, "/start")
    await handlers.cmd_start(msg_start, FakeState())
    all_markup = [kw.get("reply_markup") for _, kw in msg_start.sent]
    example_buttons = [
        b
        for markup in all_markup
        if getattr(markup, "inline_keyboard", None)
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data == "menu:example"
    ]
    check("/start: offers the 'Показать пример' button", len(example_buttons) == 1)

    cb_example = FakeCallback(u_new, "menu:example")
    await handlers.cb_menu_example(cb_example)
    example_text = cb_example.message.sent[0][0]
    check("'Показать пример': sends a worked example", "Решение" in example_text and "Ответ" in example_text)
    check("'Показать пример': ends by inviting the user to send their own", "пришли своё" in example_text.lower())
    check(
        "'Показать пример': costs the user nothing (no quota consumed)",
        db.get_status(u_new, "tester")["used_today"] == 0,
    )

    # ==================================================================
    # Правка 5: меню в два уровня + формат остатков
    # ==================================================================
    main_kb = handlers.main_menu_keyboard()
    main_buttons = [b for row in main_kb.inline_keyboard for b in row]
    main_data = [b.callback_data for b in main_buttons]
    check("main menu: four real options plus 'Ещё'", len(main_buttons) == 5)
    check("main menu: has Баланс", "menu:balance" in main_data)
    check("main menu: has Модель", "menu:model" in main_data)
    check("main menu: has Картинка", "menu:image" in main_data)
    check("main menu: keeps Помощь on the FIRST level", "menu:help" in main_data)
    check("main menu: has 'Ещё'", "menu:more" in main_data)
    for hidden in ("menu:notes", "menu:reminder", "menu:invite", "menu:reset"):
        check(f"main menu: {hidden} moved off the first screen", hidden not in main_data)

    more_kb = handlers.more_menu_keyboard()
    more_data = [b.callback_data for row in more_kb.inline_keyboard for b in row]
    for moved in ("menu:notes", "menu:reminder", "menu:invite", "menu:reset"):
        check(f"'Ещё' menu: {moved} is reachable there", moved in more_data)
    check("'Ещё' menu: has a way back", "menu:back" in more_data)

    cb_more = FakeCallback(90007, "menu:more")
    await handlers.cb_menu_more(cb_more, FakeState())
    check("menu:more callback renders the second level", len(cb_more.message.edits) == 1)

    # Формат остатков.
    check("_remaining formats as 'осталось X из Y'", handlers._remaining(3, 10) == "осталось 7 из 10")
    check("_remaining never goes negative", handlers._remaining(15, 10) == "осталось 0 из 10")

    u_bal = 90008
    db._ensure_player(u_bal, "balancer")
    balance_text = handlers._balance_text(u_bal, "balancer")
    check("balance screen uses 'осталось X из Y'", "осталось" in balance_text)
    check("balance screen no longer shows a bare 'N/M' fraction", f"0/{DAILY_FREE_IMAGE_MESSAGES}" not in balance_text)

    # Админская карточка формат НЕ меняет (Правка 5.4).
    admin_card = handlers._admin_user_text(db.get_player(u_bal), [])
    check("admin user card keeps the compact N/M format", "/" in admin_card and "осталось" not in admin_card)

    # ==================================================================
    # Метрика активации
    # ==================================================================
    with db._lock:
        db._conn.execute("DELETE FROM players")
        db._conn.execute("DELETE FROM chat_log")
        db._conn.commit()
    for uid in (91001, 91002, 91003):
        db._ensure_player(uid, f"newbie{uid}")
    db.log_message(91001, "newbie91001", "user", "привет")
    act = db.get_activation_stats()
    check("activation: 3 new users today", act["new_today"] == 3)
    check("activation: 1 of them wrote something", act["activated_today"] == 1)
    check("activation: 33%", act["activated_today_pct"] == 33)

    with db._lock:
        db._conn.execute("DELETE FROM players")
        db._conn.commit()
    empty = db.get_activation_stats()
    check("activation: empty day is 0%, not a crash", empty["new_today"] == 0 and empty["activated_today_pct"] == 0)

    stats_text = handlers._admin_stats_text()
    check("admin stats: shows the activation block", "Активация" in stats_text)
    check("admin stats: shows the blocked-images counter", "Заблокировано запросов на картинки" in stats_text)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
