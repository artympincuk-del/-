import base64

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
)

from bot import ai, db
from bot.config import (
    ADMIN_IDS,
    DAILY_FREE_MESSAGES,
    DAILY_FREE_PREMIUM_MESSAGES,
    FAST_MODEL,
    PREMIUM_CREDIT_COST,
    PREMIUM_MODEL,
    VISION_MODEL,
)
from bot.payments import PACKAGES, packages_keyboard

router = Router()

MODEL_NAMES = {"fast": "⚡ Быстрая (Llama 3.1 8B)", "premium": "💎 Премиум (Llama 3.3 70B)"}

BTN_BALANCE = "💰 Баланс"
BTN_BUY = "💎 Пополнить"
BTN_MODEL = "🧠 Модель"
BTN_NOTES = "📝 Заметки"
BTN_RESET = "🔄 Сбросить диалог"
BTN_HELP = "❓ Помощь"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_BALANCE, callback_data="menu:balance"),
                InlineKeyboardButton(text=BTN_BUY, callback_data="menu:buy"),
            ],
            [
                InlineKeyboardButton(text=BTN_MODEL, callback_data="menu:model"),
                InlineKeyboardButton(text=BTN_NOTES, callback_data="menu:notes"),
            ],
            [
                InlineKeyboardButton(text=BTN_RESET, callback_data="menu:reset"),
                InlineKeyboardButton(text=BTN_HELP, callback_data="menu:help"),
            ],
        ]
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def model_keyboard(current: str) -> InlineKeyboardMarkup:
    def label(key: str, text: str) -> str:
        return f"✅ {text}" if key == current else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("fast", MODEL_NAMES["fast"]), callback_data="model:fast")],
            [InlineKeyboardButton(text=label("premium", MODEL_NAMES["premium"]), callback_data="model:premium")],
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
        "🤖 <b>Привет! Я AI-ассистент на базе Llama (Groq).</b>\n\n"
        f"Бесплатно: <b>{DAILY_FREE_MESSAGES}</b> сообщений в день на быстрой модели "
        f"и <b>{DAILY_FREE_PREMIUM_MESSAGES}</b> на премиум (сброс в 00:00).\n"
        "Дальше — докупка сообщений за Telegram Stars.\n\n"
        "Просто напиши мне вопрос или пришли фото — и я отвечу.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


HELP_TEXT = (
    "🤖 <b>Как это работает</b>\n\n"
    f"• Быстрая модель (Llama 3.1 8B): {DAILY_FREE_MESSAGES} бесплатных сообщений в день, "
    "дальше — из докупленного пакета.\n"
    f"• Премиум модель (Llama 3.3 70B): точнее и умнее — {DAILY_FREE_PREMIUM_MESSAGES} "
    f"бесплатных сообщений в день, дальше каждое списывает {PREMIUM_CREDIT_COST} "
    "сообщений из докупленного пакета.\n"
    "• Фото: пришли картинку (можно с подписью-вопросом) — распознаю содержимое. "
    "Списывается как обычное сообщение.\n"
    "• Заметки: напиши «Запомни: ...» — я буду учитывать это в каждом ответе, даже "
    "после перезапуска. Список — кнопка «Заметки» в меню, удалить — /forget <номер>.\n\n"
    "Открыть меню в любой момент — /menu."
)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.callback_query(F.data == "menu:help")
async def cb_menu_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(HELP_TEXT)


def _balance_text(user_id: int, username: str | None) -> str:
    status = db.get_status(user_id, username)
    model_name = MODEL_NAMES[status["model_pref"]]
    return (
        f"📊 <b>Баланс</b>\n\n"
        f"Модель: {model_name}\n"
        f"Быстрая, бесплатных сегодня: {status['used_today']}/{DAILY_FREE_MESSAGES}\n"
        f"Премиум, бесплатных сегодня: {status['premium_used_today']}/{DAILY_FREE_PREMIUM_MESSAGES}\n"
        f"Докупленные сообщения: <b>{status['bonus_credits']}</b>"
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    await message.answer(_balance_text(message.from_user.id, message.from_user.username))


@router.callback_query(F.data == "menu:balance")
async def cb_menu_balance(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(_balance_text(callback.from_user.id, callback.from_user.username))


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer("Выберите модель:", reply_markup=model_keyboard(status["model_pref"]))


@router.callback_query(F.data == "menu:model")
async def cb_menu_model(callback: CallbackQuery) -> None:
    await callback.answer()
    status = db.get_status(callback.from_user.id, callback.from_user.username)
    await callback.message.answer("Выберите модель:", reply_markup=model_keyboard(status["model_pref"]))


@router.callback_query(F.data.startswith("model:"))
async def cb_model(callback: CallbackQuery) -> None:
    choice = callback.data.split(":")[1]
    db.set_model_pref(callback.from_user.id, callback.from_user.username, choice)
    await callback.answer(f"Модель: {MODEL_NAMES[choice]}")
    await callback.message.edit_text(
        "Выберите модель:", reply_markup=model_keyboard(choice)
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог сброшен. Начнём заново.")


@router.callback_query(F.data == "menu:reset")
async def cb_menu_reset(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Диалог сброшен")
    await callback.message.answer("Диалог сброшен. Начнём заново.")


BUY_TEXT = "💎 <b>Купить сообщения за Telegram Stars</b>\n\nВыберите пакет:"


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(BUY_TEXT, reply_markup=packages_keyboard())


@router.callback_query(F.data == "menu:buy")
async def cb_menu_buy(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(BUY_TEXT, reply_markup=packages_keyboard())


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


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    payload = message.successful_payment.invoice_payload
    _, _, count_str = payload.partition(":")
    count = int(count_str)

    new_balance = db.add_bonus_credits(message.from_user.id, message.from_user.username, count)

    await message.answer(
        f"✅ Оплата получена! Начислено <b>{count}</b> сообщений.\n"
        f"Доступно докупленных сообщений: <b>{new_balance}</b>."
    )


def _notes_text(user_id: int) -> str:
    notes = db.list_notes(user_id)
    if not notes:
        return "Пока нет заметок. Просто напиши «Запомни: ...» или команду /remember <текст>."
    lines = ["📝 <b>Заметки</b>\n"]
    for note_id, content in notes:
        lines.append(f"{note_id}. {content}")
    lines.append("\nУдалить: /forget <номер>")
    return "\n".join(lines)


@router.message(Command("notes"))
async def cmd_notes(message: Message) -> None:
    await message.answer(_notes_text(message.from_user.id))


@router.callback_query(F.data == "menu:notes")
async def cb_menu_notes(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(_notes_text(callback.from_user.id))


@router.message(Command("remember"))
async def cmd_remember(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажи, что запомнить: /remember <текст>")
        return
    note_id = db.add_note(message.from_user.id, parts[1].strip())
    await message.answer(f"✅ Запомнил (заметка №{note_id}).")


@router.message(Command("forget"))
async def cmd_forget(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Укажи номер заметки: /forget <номер> (список — кнопка «Заметки»)")
        return
    note_id = int(parts[1].strip())
    if db.delete_note(message.from_user.id, note_id):
        await message.answer(f"Заметка №{note_id} удалена.")
    else:
        await message.answer("Заметка с таким номером не найдена.")


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🔑 <b>Админ-панель</b>\n\n"
        "/grant &lt;user_id&gt; &lt;amount&gt; — выдать (или списать, если amount отрицательный) "
        "сообщения пользователю\n"
        "/users — список пользователей и их лимитов\n"
        "/chatlog &lt;user_id&gt; [N] — последние N сообщений переписки (по умолчанию 20)"
    )


@router.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Использование: /grant <user_id> <amount>")
        return
    target_id = int(parts[1])
    amount = int(parts[2])
    new_balance = db.add_bonus_credits(target_id, None, amount)
    await message.answer(f"Готово. Баланс пользователя {target_id}: {new_balance} сообщений.")


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
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /chatlog <user_id> [N]")
        return
    target_id = int(parts[1])
    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 20
    rows = db.get_recent_chat(target_id, limit)
    if not rows:
        await message.answer("Нет сообщений для этого пользователя.")
        return
    lines = [f"💬 Чат с {target_id} (последние {len(rows)}):\n"]
    for role, content, created_at in rows:
        who = "👤" if role == "user" else "🤖"
        text = content if len(content) <= 300 else content[:300] + "…"
        lines.append(f"{who} [{created_at}] {text}")
    full_text = "\n".join(lines)
    if len(full_text) > 3800:
        full_text = full_text[-3800:]
    await message.answer(full_text, parse_mode=None)


REMEMBER_PREFIXES = ("запомни:", "запомни,", "запомни ")


@router.message(F.text & ~F.text.startswith("/"))
async def handle_chat_message(message: Message, state: FSMContext) -> None:
    text = message.text
    lowered = text.strip().lower()

    for prefix in REMEMBER_PREFIXES:
        if lowered.startswith(prefix):
            note_text = text.strip()[len(prefix):].strip()
            if note_text:
                note_id = db.add_note(message.from_user.id, note_text)
                await message.answer(f"✅ Запомнил (заметка №{note_id}).")
                return
            break

    user_id = message.from_user.id
    username = message.from_user.username

    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    model = PREMIUM_MODEL if status["model_pref"] == "premium" else FAST_MODEL
    notes = [content for _id, content in db.list_notes(user_id)]

    data = await state.get_data()
    history = data.get("history", [])

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        reply_text = await ai.ask_ai(history, text, model, notes=notes)
    except ai.AIError:
        await message.answer("Не удалось получить ответ. Попробуйте ещё раз.")
        return

    db.log_message(user_id, username, "user", text)
    db.log_message(user_id, username, "assistant", reply_text)

    history = history + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    await message.answer(reply_text, parse_mode=None)


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

    caption = message.caption or "Опиши, что на этом фото."
    notes = [content for _id, content in db.list_notes(user_id)]

    file_buf = await message.bot.download(message.photo[-1].file_id)
    image_b64 = base64.b64encode(file_buf.read()).decode()
    data_url = f"data:image/jpeg;base64,{image_b64}"

    data = await state.get_data()
    history = data.get("history", [])

    await message.bot.send_chat_action(message.chat.id, "typing")

    user_content = [
        {"type": "text", "text": caption},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]

    try:
        reply_text = await ai.ask_ai(history, user_content, VISION_MODEL, notes=notes)
    except ai.AIError:
        await message.answer("Не удалось обработать изображение. Попробуйте ещё раз.")
        return

    db.log_message(user_id, username, "user", f"[фото] {caption}")
    db.log_message(user_id, username, "assistant", reply_text)

    history = history + [
        {"role": "user", "content": f"[фото] {caption}"},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    await message.answer(reply_text, parse_mode=None)
