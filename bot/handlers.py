import asyncio

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)

from bot import db
from bot.config import WEBAPP_URL
from bot.payments import PACKAGES, packages_keyboard
from bot.roulette import bet_label, color_emoji, color_of, payout_multiplier, spin

router = Router()


def webapp_keyboard() -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎰 Открыть рулетку", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


class BetStates(StatesGroup):
    waiting_amount = State()
    waiting_number = State()


def bet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔴 Красное", callback_data="bet:color:red"),
                InlineKeyboardButton(text="⚫ Чёрное", callback_data="bet:color:black"),
            ],
            [
                InlineKeyboardButton(text="Чёт", callback_data="bet:parity:even"),
                InlineKeyboardButton(text="Нечет", callback_data="bet:parity:odd"),
            ],
            [
                InlineKeyboardButton(text="1-18", callback_data="bet:range:low"),
                InlineKeyboardButton(text="19-36", callback_data="bet:range:high"),
            ],
            [
                InlineKeyboardButton(text="Дюжина 1", callback_data="bet:dozen:1"),
                InlineKeyboardButton(text="Дюжина 2", callback_data="bet:dozen:2"),
                InlineKeyboardButton(text="Дюжина 3", callback_data="bet:dozen:3"),
            ],
            [
                InlineKeyboardButton(text="🔢 Число (0-36)", callback_data="bet:number"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="bet:cancel"),
            ],
        ]
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    balance = db.get_or_create_player(message.from_user.id, message.from_user.username)
    await message.answer(
        "🎰 <b>Добро пожаловать в Рулетку!</b>\n\n"
        f"Ваш стартовый баланс: <b>{balance}</b> фишек.\n\n"
        "Команды:\n"
        "/play &lt;ставка&gt; — сделать ставку (текстом)\n"
        "/balance — узнать баланс\n"
        "/buy — купить фишки за Telegram Stars\n"
        "/top — таблица лидеров\n"
        "/help — правила игры",
        reply_markup=webapp_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🎲 <b>Правила европейской рулетки</b>\n\n"
        "Числа: 0-36. 0 — зелёное, остальные красные/чёрные.\n\n"
        "Типы ставок и выплаты (профит к ставке):\n"
        "• Число — 35:1\n"
        "• Дюжина (1-12 / 13-24 / 25-36) — 2:1\n"
        "• Цвет / Чёт-Нечет / 1-18-19-36 — 1:1\n\n"
        "Выпадение 0 — проигрыш всех внешних ставок (кроме ставки на число 0).\n\n"
        "Чтобы сыграть: /play 100"
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    balance = db.get_or_create_player(message.from_user.id, message.from_user.username)
    await message.answer(f"💰 Ваш баланс: <b>{balance}</b> фишек.")


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    rows = db.top_players()
    if not rows:
        await message.answer("Пока никто не играл.")
        return
    lines = ["🏆 <b>Топ игроков</b>"]
    for i, (username, balance) in enumerate(rows, start=1):
        name = f"@{username}" if username else "аноним"
        lines.append(f"{i}. {name} — {balance}")
    await message.answer("\n".join(lines))


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        "💎 <b>Купить фишки за Telegram Stars</b>\n\nВыберите пакет:",
        reply_markup=packages_keyboard(),
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery) -> None:
    idx = int(callback.data.split(":")[1])
    pkg = PACKAGES[idx]
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"{pkg['chips']} фишек рулетки",
        description="Пополнение игрового баланса. Фишки виртуальные, вывод недоступен.",
        payload=f"chips:{pkg['chips']}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{pkg['chips']} фишек", amount=pkg["stars"])],
        provider_token="",
    )


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    payload = message.successful_payment.invoice_payload
    _, _, chips_str = payload.partition(":")
    chips = int(chips_str)

    balance = db.get_or_create_player(message.from_user.id, message.from_user.username)
    balance += chips
    db.set_balance(message.from_user.id, balance)

    await message.answer(
        f"✅ Оплата получена! Начислено <b>{chips}</b> фишек.\n"
        f"Баланс: <b>{balance}</b> фишек."
    )


@router.message(Command("play"))
async def cmd_play(message: Message, state: FSMContext) -> None:
    balance = db.get_or_create_player(message.from_user.id, message.from_user.username)
    parts = message.text.split(maxsplit=1)

    amount_text = parts[1].strip() if len(parts) > 1 else None
    if amount_text is None:
        await state.set_state(BetStates.waiting_amount)
        await message.answer(
            f"Ваш баланс: {balance}. Введите размер ставки числом (или /cancel):"
        )
        return

    await _handle_amount(message, state, amount_text, balance)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(StateFilter(BetStates.waiting_amount))
async def process_amount(message: Message, state: FSMContext) -> None:
    balance = db.get_balance(message.from_user.id)
    await _handle_amount(message, state, message.text.strip(), balance)


async def _handle_amount(message: Message, state: FSMContext, amount_text: str, balance: int) -> None:
    if not amount_text.isdigit():
        await message.answer("Ставка должна быть положительным целым числом. Попробуйте снова:")
        return

    amount = int(amount_text)
    if amount <= 0:
        await message.answer("Ставка должна быть больше нуля. Попробуйте снова:")
        return
    if amount > balance:
        await message.answer(f"Недостаточно фишек (баланс: {balance}). Введите меньшую ставку:")
        return

    await state.update_data(amount=amount)
    await state.set_state(None)
    await message.answer(
        f"Ставка: {amount}. Выберите тип ставки:", reply_markup=bet_keyboard()
    )


@router.callback_query(F.data == "bet:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.callback_query(F.data == "bet:number")
async def cb_number(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if "amount" not in data:
        await callback.answer("Сначала начните ставку через /play", show_alert=True)
        return
    await state.set_state(BetStates.waiting_number)
    await callback.message.edit_text("Введите число от 0 до 36:")
    await callback.answer()


@router.message(StateFilter(BetStates.waiting_number))
async def process_number(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.isdigit() or not (0 <= int(text) <= 36):
        await message.answer("Введите целое число от 0 до 36:")
        return

    data = await state.get_data()
    amount = data["amount"]
    await state.clear()
    await resolve_bet(message.answer, message.from_user, "number", text, amount)


@router.callback_query(F.data.startswith("bet:"))
async def cb_bet(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if "amount" not in data:
        await callback.answer("Сначала начните ставку через /play", show_alert=True)
        return
    amount = data["amount"]
    await state.clear()

    _, bet_type, bet_value = callback.data.split(":")
    await callback.answer()
    await resolve_bet(callback.message.edit_text, callback.from_user, bet_type, bet_value, amount)


async def resolve_bet(send, user, bet_type: str, bet_value: str, amount: int) -> None:
    balance = db.get_balance(user.id)
    if amount > balance:
        await send(f"Недостаточно фишек (баланс: {balance}).")
        return

    balance -= amount
    db.set_balance(user.id, balance)

    msg = await send(f"🎰 Ставка «{bet_label(bet_type, bet_value)}» на {amount}. Крутим...")
    await asyncio.sleep(1.2)

    number = spin()
    color = color_of(number)
    multiplier = payout_multiplier(bet_type, bet_value, number)

    if multiplier > 0:
        profit = amount * multiplier
        balance += amount + profit
        db.set_balance(user.id, balance)
        result_text = f"🎉 Выигрыш! +{profit}"
    else:
        result_text = f"😔 Проигрыш. -{amount}"

    text = (
        f"{color_emoji(color)} Выпало число: <b>{number}</b>\n\n"
        f"{result_text}\n"
        f"Баланс: <b>{balance}</b> фишек.\n\n"
        "Сыграть ещё: /play"
    )

    if hasattr(msg, "edit_text"):
        await msg.edit_text(text)
    else:
        await send(text)
