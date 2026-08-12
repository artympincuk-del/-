from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Telegram Stars (XTR) packages. "stars" is charged to the user, "messages" is
# credited to their bonus quota on successful payment.
PACKAGES = [
    {"messages": 50, "stars": 50},
    {"messages": 150, "stars": 120},
    {"messages": 400, "stars": 280},
    {"messages": 1000, "stars": 600},
]


def packages_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i, pkg in enumerate(PACKAGES):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{pkg['messages']} сообщений — {pkg['stars']} ⭐",
                    callback_data=f"buy:{i}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
