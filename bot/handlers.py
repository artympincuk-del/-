import base64
import io
import logging
import re
import time

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
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
    REFERRAL_BONUS_MESSAGES,
    VISION_MODEL,
)
from bot.payments import PACKAGES, PRICE_VERSION, TIME_PACKAGES, packages_keyboard, resolve_package

logger = logging.getLogger(__name__)

router = Router()


class Form(StatesGroup):
    waiting_for_image_prompt = State()


# (tier, choice) -> which actual Groq model + reasoning_effort to use. `tier`
# ('fast'/'premium') decides which quota bucket a message is billed against;
# `choice` decides which specific engine runs within that tier — the two
# GPT-OSS models are the default/recommended pick, the original Llama models
# are offered alongside them as an alternative "flavor" within the same tier.
MODEL_OPTIONS = {
    ("fast", "gptoss"): {
        "model": FAST_MODEL,
        "reasoning": FAST_REASONING_EFFORT,
        "label": "⚡ GPT-OSS 20B",
    },
    ("fast", "llama"): {
        "model": "llama-3.1-8b-instant",
        "reasoning": None,
        "label": "⚡🦙 Llama 3.1 8B",
    },
    ("premium", "gptoss"): {
        "model": PREMIUM_MODEL,
        "reasoning": PREMIUM_REASONING_EFFORT,
        "label": "💎 GPT-OSS 120B (глубокий анализ)",
    },
    ("premium", "llama"): {
        "model": "llama-3.3-70b-versatile",
        "reasoning": None,
        "label": "💎🦙 Llama 3.3 70B",
    },
}


def _model_option(status: dict) -> dict:
    key = (status["model_pref"], status.get("model_choice") or "gptoss")
    return MODEL_OPTIONS.get(key, MODEL_OPTIONS[(status["model_pref"], "gptoss")])

BTN_BALANCE = "💰 Баланс / Пополнить"
BTN_BUY = "💎 Пополнить"
BTN_MODEL = "🧠 Модель"
BTN_NOTES = "📝 Заметки"
BTN_IMAGE = "🎨 Картинка"
BTN_INVITE = "🎁 Пригласить друга"
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
                InlineKeyboardButton(text=BTN_INVITE, callback_data="menu:invite"),
                InlineKeyboardButton(text=BTN_RESET, callback_data="menu:reset"),
            ],
            [
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


_bot_username_cache: str | None = None


async def _get_bot_username(bot) -> str:
    global _bot_username_cache
    if _bot_username_cache is None:
        me = await bot.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


async def _should_respond_in_group(message: Message) -> bool:
    """Private chats: always respond. Groups: only when the bot is directly
    addressed (replied to, or @mentioned) — Telegram delivers every group
    message to the bot once privacy mode is off, and answering all of them
    would spam the whole chat instead of just the person who asked."""
    if message.chat.type == "private":
        return True
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == message.bot.id
    ):
        return True
    text = message.text or message.caption or ""
    username = await _get_bot_username(message.bot)
    return f"@{username}".lower() in text.lower()


async def _strip_mention(message: Message, text: str) -> str:
    if message.chat.type == "private" or not text:
        return text
    username = await _get_bot_username(message.bot)
    return re.sub(rf"@{re.escape(username)}", "", text, flags=re.IGNORECASE).strip()


@router.my_chat_member()
async def on_bot_membership_changed(event: ChatMemberUpdated) -> None:
    """Introduce the bot to the whole group the moment it's added, instead of
    staying silent until someone happens to @mention it — one add exposes the
    entire class/chat at once instead of relying on word of mouth."""
    if event.chat.type not in ("group", "supergroup"):
        return
    was_in = event.old_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    )
    is_in = event.new_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    )
    if was_in or not is_in:
        return

    username = await _get_bot_username(event.bot)
    await event.bot.send_message(
        event.chat.id,
        "👋 <b>Привет! Я AI-ассистент.</b>\n\n"
        "Помогаю с домашкой и вопросами: понимаю текст, фото, голосовые и PDF.\n\n"
        f"В этом чате отвечаю, только если меня <b>упомянуть</b> (@{username}) или "
        "<b>ответить</b> на моё сообщение — не буду встревать в каждый разговор.\n\n"
        "Написать в личку и посмотреть все возможности — /menu там.",
    )


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


async def _send_long(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Telegram rejects messages over ~4096 chars outright; split instead of crashing.
    Sends with the bot's default HTML parse mode so the model's <b>/<i>/<code>
    formatting renders; if the model ever emits malformed markup, fall back to
    plain text for that chunk instead of losing the reply entirely. `reply_markup`
    (if given) is attached only to the last chunk."""
    chunks = [
        text[i : i + TELEGRAM_MAX_MESSAGE_LEN]
        for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LEN)
    ] or [""]
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            await message.answer(chunk, reply_markup=markup)
        except TelegramBadRequest:
            await message.answer(chunk, parse_mode=None, reply_markup=markup)


QUICK_ACTIONS = {
    "detail": "Дай больше деталей и разверни предыдущий ответ подробнее.",
    "simpler": "Объясни то же самое проще, другими словами, как для новичка.",
    "example": "Приведи ещё один похожий пример с решением.",
}


def quick_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Подробнее", callback_data="qa:detail"),
                InlineKeyboardButton(text="💡 Проще", callback_data="qa:simpler"),
                InlineKeyboardButton(text="📝 Пример", callback_data="qa:example"),
            ],
            [InlineKeyboardButton(text="📤 Поделиться", callback_data="qa:share")],
        ]
    )


def model_keyboard(current_pref: str, current_choice: str) -> InlineKeyboardMarkup:
    def label(tier: str, choice: str) -> str:
        text = MODEL_OPTIONS[(tier, choice)]["label"]
        return f"✅ {text}" if (tier, choice) == (current_pref, current_choice) else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("fast", "llama"), callback_data="model:fast:llama")],
            [InlineKeyboardButton(text=label("fast", "gptoss"), callback_data="model:fast:gptoss")],
            [InlineKeyboardButton(text=label("premium", "llama"), callback_data="model:premium:llama")],
            [InlineKeyboardButton(text=label("premium", "gptoss"), callback_data="model:premium:gptoss")],
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


async def _apply_referral(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("ref_"):
        return
    referrer_str = parts[1][len("ref_") :]
    if not referrer_str.isdigit():
        return
    referrer_id = int(referrer_str)
    user_id = message.from_user.id
    db.get_status(user_id, message.from_user.username)  # ensure the row exists first
    if not db.set_referrer(user_id, referrer_id):
        return
    db.add_bonus_credits(user_id, message.from_user.username, REFERRAL_BONUS_MESSAGES)
    new_balance = db.add_bonus_credits(referrer_id, None, REFERRAL_BONUS_MESSAGES)
    try:
        await message.bot.send_message(
            referrer_id,
            f"🎉 По твоей ссылке присоединился новый пользователь — начислено "
            f"<b>{REFERRAL_BONUS_MESSAGES}</b> сообщений! Баланс: {new_balance}.",
        )
    except Exception:
        pass  # referrer may have blocked the bot — not worth failing /start over


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _apply_referral(message)
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
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
) -> None:
    """Edits the button's own message in place instead of sending a new one,
    so navigating the menu doesn't spam the chat with a fresh message every
    tap. Falls back to sending a new message if editing isn't possible
    (e.g. re-selecting the same option leaves text/markup unchanged, which
    Telegram rejects as "message is not modified"). Pass parse_mode=None for
    screens that embed raw user content, so stray '<'/'&' can't crash the
    edit the way they used to for the old HTML-only menus."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


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
    "или день — удобно, если нужно решить много задач подряд и не считать сообщения.\n"
    "• Под каждым ответом есть кнопки «Подробнее» / «Проще» / «Пример» — не нужно "
    "переписывать вопрос, чтобы уточнить ответ.\n"
    f"• Пригласи друга (кнопка в меню) — когда он запустит бота по твоей ссылке, вы оба "
    f"получите по {REFERRAL_BONUS_MESSAGES} сообщений.\n"
    "• В группах бот отвечает, только если его упомянуть (@username) или ответить на "
    "его сообщение — чтобы не отвечать на каждое сообщение в чате.\n\n"
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
    model_name = _model_option(status)["label"]
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


MODEL_MENU_TEXT = (
    "Выберите модель:\n\n"
    "⚡ — быстрый тариф, 💎 — премиум (глубже анализ, свой дневной лимит)\n"
    "🦙 — оригинальные модели Llama (альтернатива GPT-OSS)"
)


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer(
        MODEL_MENU_TEXT, reply_markup=model_keyboard(status["model_pref"], status["model_choice"])
    )


@router.callback_query(F.data == "menu:model")
async def cb_menu_model(callback: CallbackQuery) -> None:
    await callback.answer()
    status = db.get_status(callback.from_user.id, callback.from_user.username)
    await _edit_or_send(
        callback, MODEL_MENU_TEXT, model_keyboard(status["model_pref"], status["model_choice"])
    )


@router.message(F.text == BTN_MODEL)
async def btn_model_text(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer(
        MODEL_MENU_TEXT, reply_markup=model_keyboard(status["model_pref"], status["model_choice"])
    )


@router.callback_query(F.data.startswith("model:"))
async def cb_model(callback: CallbackQuery) -> None:
    _, tier, choice = callback.data.split(":")
    db.set_model_pref(callback.from_user.id, callback.from_user.username, tier, choice)
    label = MODEL_OPTIONS[(tier, choice)]["label"]
    await callback.answer(f"Модель: {label}")
    await _edit_or_send(callback, MODEL_MENU_TEXT, model_keyboard(tier, choice))


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


async def _invite_text(bot, user_id: int) -> str:
    username = await _get_bot_username(bot)
    link = f"https://t.me/{username}?start=ref_{user_id}"
    return (
        f"🎁 <b>Пригласи друга</b>\n\n"
        f"Отправь эту ссылку другу — как только он запустит бота по ней, вы "
        f"<b>оба</b> получите по {REFERRAL_BONUS_MESSAGES} бесплатных сообщений.\n\n"
        f"<code>{link}</code>"
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    await message.answer(await _invite_text(message.bot, message.from_user.id))


@router.callback_query(F.data == "menu:invite")
async def cb_menu_invite(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(
        callback, await _invite_text(callback.bot, callback.from_user.id), back_keyboard()
    )


@router.message(F.text == BTN_INVITE)
async def btn_invite_text(message: Message) -> None:
    await message.answer(await _invite_text(message.bot, message.from_user.id))


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
        # payload carries the price version + index (not the raw amount), so
        # pre-checkout can look the package up in the *current* PACKAGES and
        # reject the invoice if prices changed since it was created.
        payload=f"messages:{PRICE_VERSION}:{idx}",
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
        payload=f"unlimited:{PRICE_VERSION}:{idx}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Безлимит {pkg['label']}", amount=pkg["stars"])],
        provider_token="",
    )


def _parse_payload(payload: str) -> tuple[str, str, int] | None:
    parts = payload.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    kind, version, idx_str = parts
    return kind, version, int(idx_str)


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    parsed = _parse_payload(pre_checkout_query.invoice_payload)
    if parsed is None:
        await pre_checkout_query.answer(
            ok=False, error_message="Некорректный платёж, оформите покупку заново."
        )
        return
    kind, version, idx = parsed
    pkg = resolve_package(kind, version, idx)
    if pkg is None:
        await pre_checkout_query.answer(
            ok=False,
            error_message="Этот пакет уже недействителен (изменились цены) — оформите покупку заново.",
        )
        return
    if pre_checkout_query.total_amount != pkg["stars"]:
        await pre_checkout_query.answer(
            ok=False, error_message="Цена не совпадает, оформите покупку заново."
        )
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    user_id = message.from_user.id
    username = message.from_user.username
    stars = sp.total_amount
    charge_id = sp.telegram_payment_charge_id

    # pre_checkout already validated the payload against current PACKAGES, so
    # this should always resolve — but re-validate defensively rather than
    # trust a payload blindly this far into the money-already-moved path.
    parsed = _parse_payload(sp.invoice_payload)
    pkg = resolve_package(*parsed) if parsed else None
    if pkg is None:
        logger.warning(
            "successful_payment with unresolvable payload=%r charge_id=%s user_id=%s",
            sp.invoice_payload, charge_id, user_id,
        )
        await message.answer(
            "✅ Оплата получена, но пакет не удалось определить автоматически. "
            f"Сообщи администратору этот номер платежа: <code>{charge_id}</code>"
        )
        return

    kind = parsed[0]
    amount = pkg["messages"] if kind == "messages" else pkg["minutes"]
    outcome, result = db.record_payment_and_credit(user_id, username, kind, stars, charge_id, amount)

    if outcome == "duplicate":
        logger.warning(
            "Duplicate successful_payment ignored: charge_id=%s user_id=%s", charge_id, user_id
        )
        await message.answer("Этот платёж уже был обработан ранее — повторного начисления не будет.")
        return

    if kind == "unlimited":
        until_local = result.replace("T", " ")
        await message.answer(
            f"✅ Оплата получена! Безлимит активирован до <b>{until_local}</b> (UTC).\n"
            f"Все сообщения без ограничений, пока безлимит активен."
        )
        return

    await message.answer(
        f"✅ Оплата получена! Начислено <b>{amount}</b> сообщений.\n"
        f"Доступно докупленных сообщений: <b>{result}</b>."
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


ADMIN_PAGE_SIZE = 8

ADMIN_MENU_TEXT = (
    "🔑 <b>Админ-панель</b>\n\n"
    "Команды по-прежнему работают: /grant, /users, /chatlog "
    "&lt;user_id или @username&gt;, /refund &lt;telegram_payment_charge_id&gt;."
)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0")],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(ADMIN_MENU_TEXT, reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await _edit_or_send(callback, ADMIN_MENU_TEXT, admin_menu_keyboard())


def _admin_stats_text() -> str:
    s = db.get_admin_stats()
    return (
        "📊 <b>Статистика</b>\n\n"
        f"Всего пользователей: <b>{s['total_users']}</b>\n"
        f"Активны сегодня: <b>{s['active_today']}</b>\n"
        f"Докупленных сообщений на руках: <b>{s['bonus_outstanding']}</b>\n\n"
        f"⭐ Доход сегодня: <b>{s['revenue_today']}</b> ({s['payments_today']} плат.)\n"
        f"⭐ Доход за 7 дней: <b>{s['revenue_7d']}</b> ({s['payments_7d']} плат.)\n"
        f"⭐ Доход всего: <b>{s['revenue_all']}</b> ({s['payments_all']} плат.)"
    )


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")]]
    )
    await _edit_or_send(callback, _admin_stats_text(), kb)


def admin_users_keyboard(page: int, total: int, users: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for uid, uname, used, premium_used, bonus, pref, last_active in users:
        label = f"@{uname}" if uname else str(uid)
        rows.append(
            [InlineKeyboardButton(text=f"{label} · {bonus}💬", callback_data=f"admin:user:{uid}:{page}")]
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:users:{page - 1}"))
    if (page + 1) * ADMIN_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:users:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin:users:"))
async def cb_admin_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await callback.answer()
    total = db.count_users()
    if total == 0:
        await _edit_or_send(callback, "Пока нет пользователей.", admin_menu_keyboard())
        return
    users = db.list_users(limit=ADMIN_PAGE_SIZE, offset=page * ADMIN_PAGE_SIZE)
    text = f"👥 <b>Пользователи</b> ({total}) — стр. {page + 1}"
    await _edit_or_send(callback, text, admin_users_keyboard(page, total, users))


ADMIN_RECENT_PAYMENTS = 5


def _admin_user_text(p: dict, payments: list[tuple]) -> str:
    name = f"@{p['username']}" if p["username"] else str(p["user_id"])
    lines = [
        f"👤 <b>{name}</b> (id <code>{p['user_id']}</code>)\n",
        f"Тариф: {p['model_pref']} / {p['model_choice']}",
        f"Бесплатно сегодня: {p['used_today']}/{DAILY_FREE_MESSAGES} + "
        f"{p['premium_used_today']}/{DAILY_FREE_PREMIUM_MESSAGES} премиум",
        f"Докупленных сообщений: <b>{p['bonus_credits']}</b>",
    ]
    if p["unlimited_until"]:
        lines.append(f"Безлимит до: {p['unlimited_until'].replace('T', ' ')} (UTC)")
    lines.append(f"Последняя активность: {p['last_active_at']}")

    if payments:
        lines.append("\n💳 <b>Последние платежи:</b>")
        for pid, kind, stars, credited, charge_id, status, created_at in payments:
            mark = "✅" if status == "paid" else "↩️"
            cid = charge_id or "нет charge_id (платёж до миграции — возврат недоступен)"
            lines.append(f"#{pid} {mark} {kind} · {stars}⭐ · {created_at}\n   {cid}")
    return "\n".join(lines)


def admin_user_keyboard(uid: int, page: int, payments: list[tuple]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="+10", callback_data=f"admin:grant:{uid}:10:{page}"),
            InlineKeyboardButton(text="+50", callback_data=f"admin:grant:{uid}:50:{page}"),
            InlineKeyboardButton(text="-10", callback_data=f"admin:grant:{uid}:-10:{page}"),
        ],
        [InlineKeyboardButton(text="💬 Чатлог", callback_data=f"admin:chatlog:{uid}:{page}")],
    ]
    for pid, kind, stars, credited, charge_id, status, created_at in payments:
        if status == "paid" and charge_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"↩️ Возврат #{pid} ({stars}⭐)",
                        callback_data=f"admin:refund:{pid}:{page}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data=f"admin:users:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin:user:"))
async def cb_admin_user(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid_str, page_str = callback.data.split(":")
    uid, page = int(uid_str), int(page_str)
    await callback.answer()
    p = db.get_player(uid)
    if p is None:
        await _edit_or_send(callback, "Пользователь не найден.", admin_menu_keyboard())
        return
    payments = db.list_recent_payments(uid, ADMIN_RECENT_PAYMENTS)
    await _edit_or_send(callback, _admin_user_text(p, payments), admin_user_keyboard(uid, page, payments))


@router.callback_query(F.data.startswith("admin:grant:"))
async def cb_admin_grant(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid_str, amount_str, page_str = callback.data.split(":")
    uid, amount, page = int(uid_str), int(amount_str), int(page_str)
    new_balance = db.admin_add_bonus_credits(uid, amount)
    if new_balance is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    await callback.answer(f"Готово: {new_balance} сообщений")
    p = db.get_player(uid)
    payments = db.list_recent_payments(uid, ADMIN_RECENT_PAYMENTS)
    await _edit_or_send(callback, _admin_user_text(p, payments), admin_user_keyboard(uid, page, payments))


async def _do_refund(bot, charge_id: str) -> str:
    """Shared by /refund and the admin-card refund button. Calls Telegram's
    Stars refund API first — only if that actually succeeds do we reverse
    the credit locally, so a Telegram-side failure can't leave the user
    stripped of messages they never got refunded for."""
    payment = db.get_payment_by_charge_id(charge_id)
    if payment is None:
        return f"Платёж с charge_id <code>{charge_id}</code> не найден."
    if payment["status"] != "paid":
        return f"Платёж #{payment['id']} уже в статусе «{payment['status']}» — повторный возврат не нужен."

    try:
        ok = await bot.refund_star_payment(
            user_id=payment["user_id"], telegram_payment_charge_id=charge_id
        )
    except TelegramBadRequest as e:
        return f"Telegram отклонил возврат: {e}"
    if not ok:
        return "Telegram вернул отказ по возврату (ok=False, без текста ошибки)."

    result = db.refund_payment(charge_id)
    if result is None:
        return (
            "⚠️ Возврат прошёл в Telegram, но локально платёж уже был помечен "
            "как возвращённый — сверь баланс пользователя вручную."
        )
    unit = "сообщений" if result["kind"] == "messages" else "мин. безлимита"
    return (
        f"✅ Возврат выполнен: {payment['amount_stars']}⭐, "
        f"списано {result['credited_amount']} {unit} у пользователя {result['user_id']}."
    )


@router.callback_query(F.data.startswith("admin:refund:"))
async def cb_admin_refund(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, pid_str, page_str = callback.data.split(":")
    pid, page = int(pid_str), int(page_str)
    await callback.answer("Выполняю возврат…")
    payment = db.get_payment(pid)
    if payment is None or not payment["charge_id"]:
        await callback.message.answer("Платёж не найден или у него нет charge_id.")
        return
    result_text = await _do_refund(callback.bot, payment["charge_id"])
    await callback.message.answer(result_text)

    uid = payment["user_id"]
    p = db.get_player(uid)
    if p is not None:
        payments = db.list_recent_payments(uid, ADMIN_RECENT_PAYMENTS)
        await _edit_or_send(
            callback, _admin_user_text(p, payments), admin_user_keyboard(uid, page, payments)
        )


@router.message(Command("refund"))
async def cmd_refund(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /refund &lt;telegram_payment_charge_id&gt;")
        return
    await message.answer(await _do_refund(message.bot, parts[1]))


def _chatlog_text(target_label: str, rows: list[tuple]) -> str:
    lines = [f"Чат с {target_label} (последние {len(rows)}):\n"]
    for role, content, created_at in rows:
        who = "[Я]" if role == "user" else "[Бот]"
        text = content if len(content) <= 300 else content[:300] + "…"
        lines.append(f"{who} [{created_at}] {text}")
    full_text = "\n".join(lines)
    if len(full_text) > 3500:
        full_text = full_text[-3500:]
    return full_text


@router.callback_query(F.data.startswith("admin:chatlog:"))
async def cb_admin_chatlog(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid_str, page_str = callback.data.split(":")
    uid, page = int(uid_str), int(page_str)
    await callback.answer()
    rows = db.get_recent_chat(uid, 20)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:{uid}:{page}")]]
    )
    if not rows:
        await _edit_or_send(callback, "Нет сообщений для этого пользователя.", kb)
        return
    # Chat content is raw user/model text and can contain stray '<'/'&' that
    # would crash HTML parsing (this bit the old menus before), so this
    # screen is sent as plain text rather than risking that.
    await _edit_or_send(callback, _chatlog_text(str(uid), rows), kb, parse_mode=None)


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
    new_balance = db.admin_add_bonus_credits(target_id, amount)
    if new_balance is None:
        await message.answer(f"Пользователь {parts[1]} не найден (он должен хотя бы раз написать боту).")
        return
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
    await message.answer(_chatlog_text(parts[1], rows), parse_mode=None)


REMEMBER_PREFIXES = ("запомни:", "запомни,", "запомни ")


async def _answer_text_query(
    message: Message, state: FSMContext, text: str, user_id: int, username: str | None
) -> None:
    """Core pipeline for anything that resolves to a text question: typed
    messages, transcribed voice, and quick-action follow-ups alike. Callers
    have already handled remember/image-prefix detection."""
    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    opt = _model_option(status)
    model, reasoning_effort = opt["model"], opt["reasoning"]
    notes = [content for _id, content in db.list_notes(user_id)]

    data = await state.get_data()
    history = data.get("history", [])

    await message.bot.send_chat_action(message.chat.id, "typing")

    t0 = time.monotonic()
    try:
        reply_text = await ai.ask_ai(
            history, text, model, notes=notes, reasoning_effort=reasoning_effort, enable_search=True
        )
    except ai.AIError as e:
        await message.answer(e.user_message)
        return
    elapsed = time.monotonic() - t0

    db.log_message(user_id, username, "user", text)
    db.log_message(user_id, username, "assistant", reply_text)

    history = history + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    footer = f"\n\n⚡ <i>Ответ за {elapsed:.1f} сек · {opt['label']}</i>"
    await _send_long(message, reply_text + footer, reply_markup=quick_actions_keyboard())


async def _process_text_query(message: Message, state: FSMContext, text: str) -> None:
    """Public entry point: checks remember/image prefixes first, then falls
    through to the shared answering pipeline."""
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

    await _answer_text_query(message, state, text, message.from_user.id, message.from_user.username)


@router.callback_query(F.data == "qa:share")
async def cb_share(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    history = data.get("history", [])
    if not history or history[-1]["role"] != "assistant":
        await callback.message.answer("Нечего делиться — сначала задай вопрос.")
        return

    username = await _get_bot_username(callback.bot)
    link = f"https://t.me/{username}?start=ref_{callback.from_user.id}"
    share_text = (
        f"{history[-1]['content']}\n\n"
        f"—\n"
        f"🤖 Решено с помощью @{username} — бесплатный AI-ассистент в Telegram: "
        f"текст, фото, голос, PDF.\n"
        f"Попробуй: {link}"
    )
    await _send_long(callback.message, share_text)


@router.callback_query(F.data.startswith("qa:"))
async def cb_quick_action(callback: CallbackQuery, state: FSMContext) -> None:
    instruction = QUICK_ACTIONS.get(callback.data.split(":", 1)[1])
    await callback.answer()
    if not instruction:
        return
    await _answer_text_query(
        callback.message, state, instruction, callback.from_user.id, callback.from_user.username
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_chat_message(message: Message, state: FSMContext) -> None:
    if not await _should_respond_in_group(message):
        return
    text = await _strip_mention(message, message.text)
    if not text:
        return
    await _process_text_query(message, state, text)


@router.message(F.voice)
async def handle_voice_message(message: Message, state: FSMContext) -> None:
    if not await _should_respond_in_group(message):
        return

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
    if not await _should_respond_in_group(message):
        return

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

    caption = await _strip_mention(message, message.caption) or "Кратко перескажи документ и выдели главное."
    prompt = (
        f"Пользователь прислал PDF «{filename}» ({len(reader.pages)} стр."
        f"{', показана только часть текста' if truncated else ''}). Содержимое документа:\n\n"
        f"{doc_text}\n\n---\nЗадача: {caption}"
    )

    opt = _model_option(status)
    model, reasoning_effort = opt["model"], opt["reasoning"]
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

    # Keep a capped excerpt (not the full doc — history gets replayed on
    # every future turn) so follow-up questions about the same PDF still
    # have some content to work with without re-uploading it.
    MAX_DOC_HISTORY_CHARS = 2000
    history_entry = f"[Документ «{filename}»] {caption}\n\n{doc_text[:MAX_DOC_HISTORY_CHARS]}"
    history = history + [
        {"role": "user", "content": history_entry},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    await _send_long(message, reply_text, reply_markup=quick_actions_keyboard())


@router.message(F.photo)
async def handle_photo_message(message: Message, state: FSMContext) -> None:
    if not await _should_respond_in_group(message):
        return

    user_id = message.from_user.id
    username = message.from_user.username

    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    caption = await _strip_mention(message, message.caption) or (
        "Реши задание на фото. Если это не задание — опиши, что на фото."
    )
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
    status_msg = await message.answer("🔍 <i>Распознаю задание...</i>")
    t0 = time.monotonic()

    # Stage 1 — pure perception: get an accurate, literal description of
    # what's on the photo (text/problem AND general visual content — the
    # PREMIUM_MODEL doing the solving in stage 2 has no vision at all, so
    # whatever isn't captured here is permanently lost to it). Kept separate
    # from solving so the vision model (weaker at multi-step reasoning)
    # isn't also responsible for getting the actual answer right — it only
    # has to accurately see. Keeping the instruction short and explicitly
    # "no analysis" measurably cut down on the model over-thinking and
    # running into the reasoning-token budget (verified via repeated
    # testing — an elaborate "think carefully and fully" instruction was
    # the actual cause of intermittent truncation).
    transcription_request = [
        {
            "type": "text",
            "text": (
                "Describe exactly what is in this image. If it contains text/a problem "
                "(numbers, formulas, questions), transcribe it verbatim. If it's a photo "
                "or scene with no text, describe what's visually shown instead (objects, "
                "people, animals, colors, setting) in enough detail to answer questions "
                "about it. Be direct and concise — no analysis, no commentary, no extra "
                "reasoning, just the transcription/description."
            ),
        },
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    try:
        vision_text = await ai.ask_ai([], transcription_request, VISION_MODEL, max_tokens=3000)
    except ai.AIError as e:
        await status_msg.edit_text(e.user_message)
        return

    try:
        await status_msg.edit_text("🧠 <i>Решаю...</i>")
    except TelegramBadRequest:
        pass

    # Stage 2 — actual solving/answering, always handed to the strongest
    # reasoning model regardless of the user's fast/premium chat preference:
    # accuracy on homework is the core promise of this bot, worth the extra
    # seconds. It never sees the photo itself, only stage 1's description.
    solve_prompt = (
        f"Вот описание фото, которое прислал пользователь:\n\n{vision_text}\n\n---\n"
        f"Запрос пользователя: {caption}\n\n"
        f"Ответь точно и по существу. Если это учебное задание — реши по шагам, а в конце "
        f"добавь короткий раздел «✅ Проверка:» с быстрой самопроверкой результата (например, "
        f"подстановкой ответа обратно в условие или другим способом решения). Если это не "
        f"вычислительная/логическая задача, а просто вопрос о содержимом фото — раздел "
        f"«Проверка» не нужен."
    )
    try:
        reply_text = await ai.ask_ai(
            history, solve_prompt, PREMIUM_MODEL, notes=notes, reasoning_effort="high", max_tokens=6144
        )
    except ai.AIError as e:
        await status_msg.edit_text(e.user_message)
        return

    try:
        await status_msg.delete()
    except TelegramBadRequest:
        pass

    db.log_message(user_id, username, "user", f"[фото] {caption}")
    db.log_message(user_id, username, "assistant", reply_text)

    # Store the actual description (not just the caption) so a follow-up
    # text question like "а второе задание?" still has something to work
    # with — vision_text is already short (stage 1 is tuned for brevity),
    # so it's safe to keep in full rather than just a placeholder.
    history_entry = f"[Фото] {caption}\n\nСодержимое фото: {vision_text}"
    history = history + [
        {"role": "user", "content": history_entry},
        {"role": "assistant", "content": reply_text},
    ]
    history = history[-(2 * 10):]
    await state.update_data(history=history)

    elapsed = time.monotonic() - t0
    has_check = "✅ Проверка" in reply_text or "Проверка:" in reply_text
    stage_label = "распознано → решено → проверено" if has_check else "распознано → решено"
    footer = f"\n\n⚡ <i>Ответ за {elapsed:.1f} сек · 🔬 {stage_label}</i>"
    await _send_long(message, reply_text + footer, reply_markup=quick_actions_keyboard())
