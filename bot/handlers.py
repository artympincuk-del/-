import base64
import io

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from PIL import Image
from pypdf import PdfReader

from bot import ai, db
from bot.config import (
    ADMIN_IDS,
    DAILY_FREE_MESSAGES,
    DAILY_FREE_PREMIUM_MESSAGES,
    FAST_MODEL,
    FAST_REASONING_EFFORT,
    IMAGE_CREDIT_COST,
    PREMIUM_CREDIT_COST,
    PREMIUM_MODEL,
    PREMIUM_REASONING_EFFORT,
    VISION_MODEL,
)
from bot.payments import PACKAGES, TIME_PACKAGES, packages_keyboard

router = Router()


class Form(StatesGroup):
    waiting_for_image_prompt = State()


MODEL_NAMES = {"fast": "⚡ Быстрая (GPT-OSS 20B)", "premium": "💎 Премиум (GPT-OSS 120B, глубокий анализ)"}

BTN_BALANCE = "💰 Баланс / Пополнить"
BTN_BUY = "💎 Пополнить"
BTN_MODEL = "🧠 Модель"
BTN_NOTES = "📝 Заметки"
BTN_IMAGE = "🎨 Картинка"
BTN_RESET = "🔄 Сбросить диалог"
BTN_HELP = "❓ Помощь"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_BALANCE, callback_data="menu:balance"),
                InlineKeyboardButton(text=BTN_MODEL, callback_data="menu:model"),
            ],
            [
                InlineKeyboardButton(text=BTN_NOTES, callback_data="menu:notes"),
                InlineKeyboardButton(text=BTN_IMAGE, callback_data="menu:image"),
            ],
            [
                InlineKeyboardButton(text=BTN_RESET, callback_data="menu:reset"),
                InlineKeyboardButton(text=BTN_HELP, callback_data="menu:help"),
            ],
        ]
    )


PERSISTENT_MENU_BTN = "📋 Меню"


def persistent_keyboard() -> ReplyKeyboardMarkup:
    """Small always-visible bottom keyboard so the menu is reachable even
    after the inline menu card has scrolled out of view — unlike inline
    keyboards, this stays pinned regardless of how much the chat scrolls."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PERSISTENT_MENU_BTN)]],
        resize_keyboard=True,
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


MAX_IMAGE_DIM = 1600


def _prepare_image(raw: bytes) -> bytes:
    """Downscale/recompress a photo so it stays well under the vision API's
    request-size limit (large phone-camera photos otherwise trigger 413s)."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if max(img.size) > MAX_IMAGE_DIM:
        img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


TELEGRAM_MAX_MESSAGE_LEN = 4000


async def _send_long(message: Message, text: str) -> None:
    """Telegram rejects messages over ~4096 chars outright; split instead of crashing.
    Sends with the bot's default HTML parse mode so the model's <b>/<i>/<code>
    formatting renders; if the model ever emits malformed markup, fall back to
    plain text for that chunk instead of losing the reply entirely."""
    for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LEN):
        chunk = text[i : i + TELEGRAM_MAX_MESSAGE_LEN]
        try:
            await message.answer(chunk)
        except TelegramBadRequest:
            await message.answer(chunk, parse_mode=None)


def model_keyboard(current: str) -> InlineKeyboardMarkup:
    def label(key: str, text: str) -> str:
        return f"✅ {text}" if key == current else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("fast", MODEL_NAMES["fast"]), callback_data="model:fast")],
            [InlineKeyboardButton(text=label("premium", MODEL_NAMES["premium"]), callback_data="model:premium")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
        ]
    )


def quota_denied_text(status: dict) -> str:
    if status["model_pref"] == "premium":
        return (
            f"Бесплатные премиум-запросы на сегодня закончились "
            f"({status['premium_used_today']}/{DAILY_FREE_PREMIUM_MESSAGES}), "
            "а докупленных сообщений не хватает. "
            "Пополните баланс: /buy, либо переключитесь на быструю модель: /model"
        )
    return (
        f"Бесплатный лимит на сегодня исчерпан ({DAILY_FREE_MESSAGES} сообщений). "
        "Докупите сообщения: /buy — или дождитесь сброса в полночь."
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🤖 <b>Привет! Я AI-ассистент.</b>\n"
        "Пиши текстом, голосом, присылай фото или PDF — отвечу на всё.\n\n"
        f"Бесплатно: <b>{DAILY_FREE_MESSAGES}</b> сообщений в день "
        f"(+{DAILY_FREE_PREMIUM_MESSAGES} премиум), сброс в 00:00.\n\n"
        "Кнопка «📋 Меню» внизу — всегда под рукой. Подробнее — «Помощь».",
        reply_markup=persistent_keyboard(),
    )
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)  # cancel any pending "waiting for image prompt" etc.
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


@router.message(F.text == PERSISTENT_MENU_BTN)
async def btn_persistent_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")]]
    )


async def _edit_or_send(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Edits the button's own message in place instead of sending a new one,
    so navigating the menu doesn't spam the chat with a fresh message every
    tap. Falls back to sending a new message if editing isn't possible
    (e.g. re-selecting the same option leaves text/markup unchanged, which
    Telegram rejects as "message is not modified")."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await callback.message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "menu:back")
async def cb_menu_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(None)
    await _edit_or_send(callback, "📋 <b>Меню</b>", main_menu_keyboard())


HELP_TEXT = (
    "🤖 <b>Как это работает</b>\n\n"
    f"• Быстрая модель (GPT-OSS 20B): {DAILY_FREE_MESSAGES} бесплатных сообщений в день, "
    "дальше — из докупленного пакета.\n"
    f"• Премиум модель (GPT-OSS 120B): думает глубже и точнее отвечает на сложные "
    f"вопросы — {DAILY_FREE_PREMIUM_MESSAGES} бесплатных сообщений в день, дальше каждое "
    f"списывает {PREMIUM_CREDIT_COST} сообщений из докупленного пакета.\n"
    "• Фото: пришли картинку (можно с подписью-вопросом) — распознаю содержимое. "
    "Списывается как обычное сообщение.\n"
    "• Голосовые: пришли голосовое — распознаю речь и отвечу как на обычное сообщение.\n"
    "• PDF-документы: пришли файл — прочитаю и отвечу по содержимому.\n"
    f"• Картинки: кнопка «Картинка» в меню (или напиши «нарисуй ...») — опиши, что нарисовать, "
    f"без всяких команд. Стоит {IMAGE_CREDIT_COST} докупленных сообщений за картинку.\n"
    "• Поиск в интернете: для вопросов про свежие новости, курсы, актуальные факты — сам "
    "решаю, когда стоит поискать в сети, и использую это в ответе.\n"
    "• Заметки: напиши «Запомни: ...» — я буду учитывать это в каждом ответе, даже "
    "после перезапуска. Список — кнопка «Заметки» в меню, удалить — /forget &lt;номер&gt;.\n"
    "• ⏱ Безлимит на время: в разделе «Баланс» можно купить безлимит на 30 минут, час "
    "или день — удобно, если нужно решить много задач подряд и не считать сообщения.\n\n"
    "Открыть меню в любой момент — /menu."
)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.callback_query(F.data == "menu:help")
async def cb_menu_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(callback, HELP_TEXT, back_keyboard())


@router.message(F.text == BTN_HELP)
async def btn_help_text(message: Message) -> None:
    # Safety net: users who haven't re-opened /start since the reply-keyboard
    # menu was removed may still have the old buttons cached client-side.
    await message.answer(HELP_TEXT)


def _balance_text(user_id: int, username: str | None) -> str:
    status = db.get_status(user_id, username)
    model_name = MODEL_NAMES[status["model_pref"]]
    unlimited_line = ""
    if status["unlimited_until"]:
        until_local = status["unlimited_until"].replace("T", " ")
        unlimited_line = f"⏱ <b>Безлимит активен до {until_local} (UTC)</b> — лимиты ниже не расходуются.\n\n"
    return (
        f"📊 <b>Баланс</b>\n\n"
        f"{unlimited_line}"
        f"Модель: {model_name}\n"
        f"Быстрая, бесплатных сегодня: {status['used_today']}/{DAILY_FREE_MESSAGES}\n"
        f"Премиум, бесплатных сегодня: {status['premium_used_today']}/{DAILY_FREE_PREMIUM_MESSAGES}\n"
        f"Докупленные сообщения: <b>{status['bonus_credits']}</b>\n\n"
        f"Пополнить прямо здесь — выбери пакет ниже:"
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    await message.answer(
        _balance_text(message.from_user.id, message.from_user.username),
        reply_markup=packages_keyboard(),
    )


@router.callback_query(F.data == "menu:balance")
async def cb_menu_balance(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(
        callback,
        _balance_text(callback.from_user.id, callback.from_user.username),
        packages_keyboard(),
    )


@router.message(F.text == BTN_BALANCE)
async def btn_balance_text(message: Message) -> None:
    await message.answer(
        _balance_text(message.from_user.id, message.from_user.username),
        reply_markup=packages_keyboard(),
    )


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer("Выберите модель:", reply_markup=model_keyboard(status["model_pref"]))


@router.callback_query(F.data == "menu:model")
async def cb_menu_model(callback: CallbackQuery) -> None:
    await callback.answer()
    status = db.get_status(callback.from_user.id, callback.from_user.username)
    await _edit_or_send(callback, "Выберите модель:", model_keyboard(status["model_pref"]))


@router.message(F.text == BTN_MODEL)
async def btn_model_text(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer("Выберите модель:", reply_markup=model_keyboard(status["model_pref"]))


@router.callback_query(F.data.startswith("model:"))
async def cb_model(callback: CallbackQuery) -> None:
    choice = callback.data.split(":")[1]
    db.set_model_pref(callback.from_user.id, callback.from_user.username, choice)
    await callback.answer(f"Модель: {MODEL_NAMES[choice]}")
    await _edit_or_send(callback, "Выберите модель:", model_keyboard(choice))


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог сброшен. Начнём заново.")


@router.callback_query(F.data == "menu:reset")
async def cb_menu_reset(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Диалог сброшен")
    await _edit_or_send(callback, "✅ Диалог сброшен. Начнём заново.", back_keyboard())


@router.message(F.text == BTN_RESET)
async def btn_reset_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог сброшен. Начнём заново.")


BUY_TEXT = "💎 <b>Купить сообщения за Telegram Stars</b>\n\nВыберите пакет:"


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(BUY_TEXT, reply_markup=packages_keyboard())


@router.callback_query(F.data == "menu:buy")
async def cb_menu_buy(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(callback, BUY_TEXT, packages_keyboard())


@router.message(F.text == BTN_BUY)
async def btn_buy_text(message: Message) -> None:
    await message.answer(BUY_TEXT, reply_markup=packages_keyboard())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery) -> None:
    idx = int(callback.data.split(":")[1])
    pkg = PACKAGES[idx]
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"{pkg['messages']} сообщений",
        description="Пополнение лимита сообщений AI-ассистента.",
        payload=f"messages:{pkg['messages']}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{pkg['messages']} сообщений", amount=pkg["stars"])],
        provider_token="",
    )


@router.callback_query(F.data.startswith("buytime:"))
async def cb_buy_time(callback: CallbackQuery) -> None:
    idx = int(callback.data.split(":")[1])
    pkg = TIME_PACKAGES[idx]
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"Безлимит на {pkg['label']}",
        description="Без ограничения по количеству сообщений на выбранное время.",
        payload=f"unlimited:{pkg['minutes']}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Безлимит {pkg['label']}", amount=pkg["stars"])],
        provider_token="",
    )


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    payload = message.successful_payment.invoice_payload
    kind, _, value_str = payload.partition(":")
    user_id = message.from_user.id
    username = message.from_user.username

    if kind == "unlimited":
        minutes = int(value_str)
        expiry = db.activate_unlimited(user_id, username, minutes)
        until_local = expiry.replace("T", " ")
        await message.answer(
            f"✅ Оплата получена! Безлимит активирован до <b>{until_local}</b> (UTC).\n"
            f"Все сообщения без ограничений, пока безлимит активен."
        )
        return

    count = int(value_str)
    new_balance = db.add_bonus_credits(user_id, username, count)
    await message.answer(
        f"✅ Оплата получена! Начислено <b>{count}</b> сообщений.\n"
        f"Доступно докупленных сообщений: <b>{new_balance}</b>."
    )


def _notes_text(user_id: int) -> str:
    notes = db.list_notes(user_id)
    if not notes:
        return "Пока нет заметок. Просто напиши «Запомни: ...» или команду /remember &lt;текст&gt;."
    lines = ["📝 <b>Заметки</b>\n"]
    for note_id, content in notes:
        lines.append(f"{note_id}. {content}")
    lines.append("\nУдалить: /forget &lt;номер&gt;")
    return "\n".join(lines)


@router.message(Command("notes"))
async def cmd_notes(message: Message) -> None:
    await message.answer(_notes_text(message.from_user.id))


@router.callback_query(F.data == "menu:notes")
async def cb_menu_notes(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(callback, _notes_text(callback.from_user.id), back_keyboard())


@router.message(F.text == BTN_NOTES)
async def btn_notes_text(message: Message) -> None:
    await message.answer(_notes_text(message.from_user.id))


IMAGE_INTRO_TEXT = (
    f"🎨 <b>Генерация картинок</b>\n\n"
    f"Опиши следующим сообщением, что нарисовать (например: «закат над горами в стиле "
    f"акварели») — не нужна команда, просто напиши и отправь.\n\n"
    f"Стоимость: {IMAGE_CREDIT_COST} докупленных сообщений за картинку (из бесплатного "
    f"дневного лимита не списывается). Пополнить — кнопка «Баланс»."
)

IMAGE_PREFIXES = ("нарисуй:", "нарисуй,", "нарисуй ", "сгенерируй картинку", "сгенерируй изображение")


async def _process_image_request(message: Message, prompt: str) -> None:
    prompt = prompt.strip()
    if not prompt:
        await message.answer(IMAGE_INTRO_TEXT)
        return

    user_id = message.from_user.id
    username = message.from_user.username

    allowed, bonus = db.try_consume_bonus_credits(user_id, username, IMAGE_CREDIT_COST)
    if not allowed:
        await message.answer(
            f"Генерация картинки стоит {IMAGE_CREDIT_COST} докупленных сообщений, а на "
            f"балансе только {bonus}. Пополните баланс: /buy"
        )
        return

    await message.bot.send_chat_action(message.chat.id, "upload_photo")

    try:
        image_bytes = await ai.generate_image(prompt)
    except ai.AIError as e:
        db.add_bonus_credits(user_id, username, IMAGE_CREDIT_COST)  # refund on failure
        await message.answer(e.user_message)
        return

    db.log_message(user_id, username, "user", f"[генерация картинки] {prompt}")
    db.log_message(user_id, username, "assistant", "[изображение отправлено]")

    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="image.jpg"),
        caption=f"🎨 {prompt}",
    )


@router.message(Command("image"))
async def cmd_image(message: Message, state: FSMContext) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        await _process_image_request(message, parts[1])
        return
    await state.set_state(Form.waiting_for_image_prompt)
    await message.answer(IMAGE_INTRO_TEXT)


@router.callback_query(F.data == "menu:image")
async def cb_menu_image(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(Form.waiting_for_image_prompt)
    await _edit_or_send(callback, IMAGE_INTRO_TEXT, back_keyboard())


@router.message(F.text == BTN_IMAGE)
async def btn_image_text(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_for_image_prompt)
    await message.answer(IMAGE_INTRO_TEXT)


@router.message(Form.waiting_for_image_prompt, F.text & ~F.text.startswith("/"))
async def handle_image_prompt_state(message: Message, state: FSMContext) -> None:
    await state.set_state(None)  # clear pending state only, keep conversation history
    await _process_image_request(message, message.text)


@router.message(Command("remember"))
async def cmd_remember(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажи, что запомнить: /remember &lt;текст&gt;")
        return
    note_id = db.add_note(message.from_user.id, parts[1].strip())
    await message.answer(f"✅ Запомнил (заметка №{note_id}).")


@router.message(Command("forget"))
async def cmd_forget(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Укажи номер заметки: /forget &lt;номер&gt; (список — кнопка «Заметки»)")
        return
    note_id = int(parts[1].strip())
    if db.delete_note(message.from_user.id, note_id):
        await message.answer(f"Заметка №{note_id} удалена.")
    else:
        await message.answer("Заметка с таким номером не найдена.")


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


def _resolve_target(target: str) -> int | None:
    """Accepts either a numeric Telegram ID or @username (username lookup
    only works for users who have messaged the bot at least once)."""
    if target.startswith("@"):
        return db.find_user_id_by_username(target[1:])
    if target.isdigit():
        return int(target)
    return None


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🔑 <b>Админ-панель</b>\n\n"
        "/grant &lt;user_id или @username&gt; &lt;amount&gt; — выдать (или списать, если "
        "amount отрицательный) сообщения пользователю\n"
        "/users — список пользователей и их лимитов (юзернеймы видны там же)\n"
        "/chatlog &lt;user_id или @username&gt; [N] — последние N сообщений переписки "
        "(по умолчанию 20)\n\n"
        "По @username находит только тех, кто хотя бы раз писал боту — иначе бот не знает "
        "его username."
    )


@router.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
        await message.answer("Использование: /grant &lt;user_id или @username&gt; &lt;amount&gt;")
        return
    target_id = _resolve_target(parts[1])
    if target_id is None:
        await message.answer(f"Пользователь {parts[1]} не найден (он должен хотя бы раз написать боту).")
        return
    amount = int(parts[2])
    new_balance = db.add_bonus_credits(target_id, None, amount)
    await message.answer(f"Готово. Баланс пользователя {parts[1]}: {new_balance} сообщений.")


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    users = db.list_users()
    if not users:
        await message.answer("Пока нет пользователей.")
        return
    lines = ["👥 <b>Пользователи</b>\n"]
    for uid, uname, used, premium_used, bonus, pref, last_active in users:
        name = f"@{uname}" if uname else str(uid)
        lines.append(
            f"{name} (id {uid}) — {pref}, "
            f"free {used}/{DAILY_FREE_MESSAGES}+{premium_used}/{DAILY_FREE_PREMIUM_MESSAGES}, "
            f"bonus {bonus}, активен {last_active}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("chatlog"))
async def cmd_chatlog(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /chatlog &lt;user_id или @username&gt; [N]")
        return
    target_id = _resolve_target(parts[1])
    if target_id is None:
        await message.answer(f"Пользователь {parts[1]} не найден (он должен хотя бы раз написать боту).")
        return
    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 20
    rows = db.get_recent_chat(target_id, limit)
    if not rows:
        await message.answer("Нет сообщений для этого пользователя.")
        return
    lines = [f"💬 Чат с {parts[1]} (последние {len(rows)}):\n"]
    for role, content, created_at in rows:
        who = "👤" if role == "user" else "🤖"
        text = content if len(content) <= 300 else content[:300] + "…"
        lines.append(f"{who} [{created_at}] {text}")
    full_text = "\n".join(lines)
    if len(full_text) > 3800:
        full_text = full_text[-3800:]
    await message.answer(full_text, parse_mode=None)


REMEMBER_PREFIXES = ("запомни:", "запомни,", "запомни ")


async def _process_text_query(message: Message, state: FSMContext, text: str) -> None:
    """Shared pipeline for anything that resolves to a text question — typed
    messages and transcribed voice messages alike."""
    lowered = text.strip().lower()

    for prefix in REMEMBER_PREFIXES:
        if lowered.startswith(prefix):
            note_text = text.strip()[len(prefix):].strip()
            if note_text:
                note_id = db.add_note(message.from_user.id, note_text)
                await message.answer(f"✅ Запомнил (заметка №{note_id}).")
                return
            break

    for prefix in IMAGE_PREFIXES:
        if lowered.startswith(prefix):
            prompt = text.strip()[len(prefix):].strip()
            await _process_image_request(message, prompt)
            return

    user_id = message.from_user.id
    username = message.from_user.username

    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    if status["model_pref"] == "premium":
        model, reasoning_effort = PREMIUM_MODEL, PREMIUM_REASONING_EFFORT
    else:
        model, reasoning_effort = FAST_MODEL, FAST_REASONING_EFFORT
    notes = [content for _id, content in db.list_notes(user_id)]

    data = await state.get_data()
    history = data.get("history", [])

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        reply_text = await ai.ask_ai(
            history, text, model, notes=notes, reasoning_effort=reasoning_effort, enable_search=True
        )
    except ai.AIError as e:
        await message.answer(e.user_message)
        return

    db.log_message(user_id, username, "user", text)
    db.log_message(user_id, username, "assistant", reply_text)

    history = history + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    await _send_long(message, reply_text)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_chat_message(message: Message, state: FSMContext) -> None:
    await _process_text_query(message, state, message.text)


@router.message(F.voice)
async def handle_voice_message(message: Message, state: FSMContext) -> None:
    file_buf = await message.bot.download(message.voice.file_id)
    try:
        text = await ai.transcribe_audio(file_buf.read())
    except ai.AIError as e:
        await message.answer(e.user_message)
        return

    text = text.strip()
    if not text:
        await message.answer("Не удалось разобрать голосовое сообщение. Попробуйте ещё раз.")
        return

    await message.answer(f"🎙 <i>Распознано:</i> {text}")
    await _process_text_query(message, state, text)


MAX_PDF_CHARS = 20000


@router.message(F.document)
async def handle_document_message(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    username = message.from_user.username

    doc = message.document
    filename = doc.file_name or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        await message.answer("Пока умею читать только PDF. Пришлите документ в формате .pdf.")
        return

    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    file_buf = await message.bot.download(doc.file_id)
    try:
        reader = PdfReader(file_buf)
        doc_text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception:
        await message.answer("Не удалось прочитать PDF. Возможно, файл повреждён.")
        return

    if not doc_text:
        await message.answer(
            "В этом PDF нет текстового слоя (похоже на скан без OCR). "
            "Пришлите страницы как фото — так я смогу распознать текст."
        )
        return

    truncated = len(doc_text) > MAX_PDF_CHARS
    doc_text = doc_text[:MAX_PDF_CHARS]

    caption = message.caption or "Кратко перескажи документ и выдели главное."
    prompt = (
        f"Пользователь прислал PDF «{filename}» ({len(reader.pages)} стр."
        f"{', показана только часть текста' if truncated else ''}). Содержимое документа:\n\n"
        f"{doc_text}\n\n---\nЗадача: {caption}"
    )

    if status["model_pref"] == "premium":
        model, reasoning_effort = PREMIUM_MODEL, PREMIUM_REASONING_EFFORT
    else:
        model, reasoning_effort = FAST_MODEL, FAST_REASONING_EFFORT
    notes = [content for _id, content in db.list_notes(user_id)]

    data = await state.get_data()
    history = data.get("history", [])

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        reply_text = await ai.ask_ai(
            history, prompt, model, notes=notes, reasoning_effort=reasoning_effort
        )
    except ai.AIError as e:
        await message.answer(e.user_message)
        return

    short_ref = f"[документ «{filename}»] {caption}"
    db.log_message(user_id, username, "user", short_ref)
    db.log_message(user_id, username, "assistant", reply_text)

    history = history + [
        {"role": "user", "content": short_ref},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    await _send_long(message, reply_text)


@router.message(F.photo)
async def handle_photo_message(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    username = message.from_user.username

    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    caption = message.caption or "Реши задание на фото. Если это не задание — опиши, что на фото."
    notes = [content for _id, content in db.list_notes(user_id)]

    file_buf = await message.bot.download(message.photo[-1].file_id)
    try:
        image_bytes = _prepare_image(file_buf.read())
    except Exception:
        await message.answer("Не удалось обработать изображение. Попробуйте другое фото.")
        return
    image_b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:image/jpeg;base64,{image_b64}"

    data = await state.get_data()
    history = data.get("history", [])

    await message.bot.send_chat_action(message.chat.id, "typing")

    # Stage 1 — pure perception: get an accurate, literal transcription of
    # what's on the photo. Kept separate from solving so the vision model
    # (weaker at multi-step reasoning) isn't also responsible for getting
    # the actual answer right — it only has to accurately see. Keeping the
    # instruction short and explicitly "no analysis" measurably cut down on
    # the model over-thinking and running into the reasoning-token budget
    # (verified via repeated testing — an elaborate "think carefully and
    # fully" instruction was the actual cause of intermittent truncation).
    transcription_request = [
        {
            "type": "text",
            "text": (
                "Transcribe exactly what is written in this image (problem text, numbers, "
                "formulas, diagram contents). Be direct and concise — output only the "
                "transcription, no analysis, no commentary, no extra reasoning."
            ),
        },
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    try:
        vision_text = await ai.ask_ai([], transcription_request, VISION_MODEL, max_tokens=3000)
    except ai.AIError as e:
        await message.answer(e.user_message)
        return

    # Stage 2 — actual solving, always handed to the strongest reasoning
    # model regardless of the user's fast/premium chat preference: accuracy
    # on homework is the core promise of this bot, worth the extra seconds.
    solve_prompt = (
        f"Вот что распознано на фото пользователя:\n\n{vision_text}\n\n---\n"
        f"Запрос пользователя: {caption}\n\n"
        f"Реши точно и по шагам, объясни ключевые моменты решения."
    )
    try:
        reply_text = await ai.ask_ai(
            history, solve_prompt, PREMIUM_MODEL, notes=notes, reasoning_effort="high", max_tokens=6144
        )
    except ai.AIError as e:
        await message.answer(e.user_message)
        return

    db.log_message(user_id, username, "user", f"[фото] {caption}")
    db.log_message(user_id, username, "assistant", reply_text)

    history = history + [
        {"role": "user", "content": f"[фото] {caption}"},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    await _send_long(message, reply_text)
