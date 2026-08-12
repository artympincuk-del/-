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
)

from bot import ai, db
from bot.config import (
    DAILY_FREE_MESSAGES,
    FAST_MODEL,
    PREMIUM_CREDIT_COST,
    PREMIUM_MODEL,
    VISION_MODEL,
)
from bot.payments import PACKAGES, packages_keyboard

router = Router()

MODEL_NAMES = {"fast": "⚡ Быстрая (Llama 3.1 8B)", "premium": "💎 Премиум (Llama 3.3 70B)"}


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
            "Недостаточно докупленных сообщений для премиум-модели. "
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
        f"Бесплатно: <b>{DAILY_FREE_MESSAGES}</b> сообщений в день (сброс в 00:00).\n"
        "Дальше — докупка сообщений за Telegram Stars.\n\n"
        "Команды:\n"
        "/model — выбрать модель (быстрая / премиум)\n"
        "/balance — остаток лимита\n"
        "/buy — купить сообщения за Stars\n"
        "/reset — начать диалог заново\n"
        "/help — подробнее\n\n"
        "Просто напиши мне вопрос или пришли фото — и я отвечу."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🤖 <b>Как это работает</b>\n\n"
        f"• Быстрая модель (Llama 3.1 8B): {DAILY_FREE_MESSAGES} бесплатных сообщений в день, "
        "дальше — из докупленного пакета.\n"
        f"• Премиум модель (Llama 3.3 70B): точнее и умнее, но без бесплатного лимита — "
        f"каждое сообщение списывает {PREMIUM_CREDIT_COST} сообщений из докупленного пакета.\n"
        "• Фото: пришли картинку (можно с подписью-вопросом) — распознаю содержимое. "
        "Списывается как обычное сообщение.\n\n"
        "Переключить модель: /model\n"
        "Купить сообщения: /buy\n"
        "Сбросить историю диалога: /reset"
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    model_name = MODEL_NAMES[status["model_pref"]]
    await message.answer(
        f"📊 <b>Баланс</b>\n\n"
        f"Модель: {model_name}\n"
        f"Бесплатных сообщений сегодня использовано: {status['used_today']}/{DAILY_FREE_MESSAGES}\n"
        f"Докупленные сообщения: <b>{status['bonus_credits']}</b>"
    )


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer(
        "Выберите модель:",
        reply_markup=model_keyboard(status["model_pref"]),
    )


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


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        "💎 <b>Купить сообщения за Telegram Stars</b>\n\nВыберите пакет:",
        reply_markup=packages_keyboard(),
    )


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


@router.message(F.text & ~F.text.startswith("/"))
async def handle_chat_message(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    username = message.from_user.username

    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    model = PREMIUM_MODEL if status["model_pref"] == "premium" else FAST_MODEL

    data = await state.get_data()
    history = data.get("history", [])

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        reply_text = await ai.ask_ai(history, message.text, model)
    except ai.AIError:
        await message.answer("Не удалось получить ответ. Попробуйте ещё раз.")
        return

    history = history + [
        {"role": "user", "content": message.text},
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
        user_id, username, DAILY_FREE_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    caption = message.caption or "Опиши, что на этом фото."

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
        reply_text = await ai.ask_ai(history, user_content, VISION_MODEL)
    except ai.AIError:
        await message.answer("Не удалось обработать изображение. Попробуйте ещё раз.")
        return

    history = history + [
        {"role": "user", "content": f"[фото] {caption}"},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    await message.answer(reply_text, parse_mode=None)
