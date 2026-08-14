import asyncio
import datetime
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, MenuButtonDefault

from bot import ai, db
from bot.config import BOT_TOKEN, CHATLOG_RETENTION_DAYS, QUOTA_TZ
from bot.handlers import REMINDER_MESSAGE_TEXT, router

logger = logging.getLogger(__name__)

_QUOTA_TZINFO = ZoneInfo(QUOTA_TZ)

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

CHATLOG_RETENTION_INTERVAL_SECONDS = 24 * 60 * 60


def _run_chatlog_retention() -> None:
    deleted = db.delete_old_chat_log(CHATLOG_RETENTION_DAYS)
    if deleted:
        logging.info(
            "Chat log retention: deleted %d row(s) older than %d days",
            deleted,
            CHATLOG_RETENTION_DAYS,
        )


async def _chatlog_retention_loop() -> None:
    """Runs once a day, on top of the startup run in main() — otherwise
    chat_log (the admin support/audit trail) has no cap and grows forever
    on Railway's disk."""
    while True:
        await asyncio.sleep(CHATLOG_RETENTION_INTERVAL_SECONDS)
        try:
            _run_chatlog_retention()
        except Exception:
            logging.exception("Chat log retention cleanup failed")


REMINDER_CHECK_INTERVAL_SECONDS = 5 * 60


async def _reminder_loop(bot: Bot) -> None:
    """Strictly opt-in — see handlers.py's reminder menu (BTN_REMINDER);
    this loop never enables it for anyone, it only delivers what a user
    already turned on for themselves. Checks every 5 minutes for users
    whose chosen local hour just started and who haven't had today's
    reminder yet."""
    while True:
        await asyncio.sleep(REMINDER_CHECK_INTERVAL_SECONDS)
        try:
            now_local = datetime.datetime.now(_QUOTA_TZINFO)
            today = now_local.date().isoformat()
            for user_id in db.get_users_due_for_reminder(now_local.hour, today):
                try:
                    await bot.send_message(user_id, REMINDER_MESSAGE_TEXT)
                except Exception:
                    pass  # user blocked the bot, deleted the chat, etc.
                db.mark_reminder_sent(user_id, today)
        except Exception:
            logging.exception("Reminder loop failed")


ERROR_USER_MESSAGE = (
    "⚠️ Что-то пошло не так, попробуй ещё раз. Если повторяется, напиши администратору."
)


async def on_error(event: ErrorEvent) -> None:
    """Global safety net: without this, any exception that escapes a
    handler (a bug, a transient failure — anything not already caught
    where it happened) just vanishes into the log and the user gets no
    reply at all. ai.AIError is never expected to reach here — every call
    site that can raise it already catches it locally and shows its own
    user_message, so this only ever sees genuinely unhandled exceptions.
    Wrapped so this handler itself can never raise and take the bot down."""
    try:
        update = event.update
        try:
            update_type = update.event_type
        except Exception:
            update_type = "unknown"

        message = update.message
        callback = update.callback_query
        user = message.from_user if message is not None else None
        if user is None and callback is not None:
            user = callback.from_user

        logger.exception(
            "Unhandled exception processing update_type=%s user_id=%s",
            update_type,
            user.id if user else None,
            exc_info=event.exception,
        )

        try:
            if message is not None:
                await message.answer(ERROR_USER_MESSAGE)
            elif callback is not None:
                await callback.answer(ERROR_USER_MESSAGE, show_alert=True)
        except Exception:
            pass  # best-effort — the user just won't get the notice this time
    except Exception:
        logger.exception("Error handler itself failed")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.errors.register(on_error)

    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    await ai.check_configured_models()
    _run_chatlog_retention()
    retention_task = asyncio.create_task(_chatlog_retention_loop())
    reminder_task = asyncio.create_task(_reminder_loop(bot))

    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(bot)
    finally:
        # Graceful shutdown: stop the background tasks, release the Telegram
        # HTTP session, and flush/close the SQLite connection instead of
        # relying on process teardown.
        for task in (retention_task, reminder_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
