from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Bump this whenever PACKAGES/TIME_PACKAGES prices or contents change. It's
# baked into every invoice's payload, so an invoice created under the old
# prices (e.g. still open in a user's chat during a redeploy) gets rejected
# at pre-checkout instead of crediting the wrong amount. This lives here
# rather than in config.py/.env on purpose: it must move in lockstep with
# the package tables right below it, and a same-file constant can't drift
# out of sync with them the way a separately-set env var could.
PRICE_VERSION = "v1"

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


def resolve_package(kind: str, version: str, idx: int) -> dict | None:
    """Looks up a package by the (kind, version, idx) triple encoded in an
    invoice payload. Returns None if the version is stale or the index is
    out of range, so pre-checkout/successful_payment can reject the payment
    instead of trusting whatever a possibly-outdated invoice claims."""
    if version != PRICE_VERSION:
        return None
    if kind == "messages":
        table = PACKAGES
    elif kind == "unlimited":
        table = TIME_PACKAGES
    else:
        return None
    if not (0 <= idx < len(table)):
        return None
    return table[idx]


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
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
