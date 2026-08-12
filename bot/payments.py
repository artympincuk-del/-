from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Telegram Stars (XTR) packages. "stars" is charged to the user, "chips" is
# credited to their in-game balance on successful payment.
PACKAGES = [
    {"chips": 500, "stars": 50},
    {"chips": 1200, "stars": 100},
    {"chips": 3000, "stars": 200},
    {"chips": 8000, "stars": 400},
]


def packages_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i, pkg in enumerate(PACKAGES):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{pkg['chips']} фишек — {pkg['stars']} ⭐",
                    callback_data=f"buy:{i}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
