from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Telegram Stars (XTR) packages. "stars" is charged to the user, "messages" is
# credited to their bonus quota on successful payment.
PACKAGES = [
    {"messages": 50, "stars": 50},
    {"messages": 150, "stars": 120},
    {"messages": 400, "stars": 280},
    {"messages": 1000, "stars": 600},
]

# Unlimited-access time passes: no per-message limit at all while active,
# for the "срочно на уроке" use case where counting messages is the wrong
# mental model. Adjust prices freely — these are starting points.
TIME_PACKAGES = [
    {"minutes": 30, "stars": 40, "label": "30 минут"},
    {"minutes": 60, "stars": 70, "label": "1 час"},
    {"minutes": 1440, "stars": 250, "label": "1 день"},
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
    for i, pkg in enumerate(TIME_PACKAGES):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⏱ Безлимит {pkg['label']} — {pkg['stars']} ⭐",
                    callback_data=f"buytime:{i}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
