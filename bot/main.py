import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonDefault

from bot.config import BOT_TOKEN
from bot.handlers import router

BOT_COMMANDS = [
    BotCommand(command="start", description="Начало работы"),
    BotCommand(command="balance", description="Баланс сообщений"),
    BotCommand(command="buy", description="Купить сообщения за Stars"),
    BotCommand(command="model", description="Выбрать модель"),
    BotCommand(command="remember", description="Запомнить заметку"),
    BotCommand(command="notes", description="Список заметок"),
    BotCommand(command="forget", description="Удалить заметку"),
    BotCommand(command="reset", description="Сбросить диалог"),
    BotCommand(command="whoami", description="Мой Telegram ID"),
    BotCommand(command="help", description="Помощь"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
