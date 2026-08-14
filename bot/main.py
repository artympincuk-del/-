import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonDefault

from bot.config import BOT_TOKEN
from bot.handlers import router

# Kept short on purpose — everything here is also one tap away in the inline
# menu (/menu). Less-common commands (buy, image, remember, notes, forget,
# whoami, admin...) still work when typed, just aren't suggested by Telegram's
# "/" autocomplete, to keep that list from feeling cluttered on mobile.
BOT_COMMANDS = [
    BotCommand(command="start", description="Начало работы"),
    BotCommand(command="menu", description="Открыть меню"),
    BotCommand(command="reset", description="Сбросить диалог"),
    BotCommand(command="help", description="Помощь"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())

    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
