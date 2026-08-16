from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import SUBSCRIPTION_DAILY_MESSAGES, SUBSCRIPTION_PRICE_STARS

# Bump this whenever PACKAGES/TIME_PACKAGES/SUBSCRIPTION prices or contents
# change. It's baked into every invoice's payload, so an invoice created
# under the old prices (e.g. still open in a user's chat during a redeploy)
# gets rejected at pre-checkout instead of crediting the wrong amount. This
# lives here rather than in config.py/.env on purpose: it must move in
# lockstep with the package tables right below it, and a same-file constant
# can't drift out of sync with them the way a separately-set env var could.
# (SUBSCRIPTION_PRICE_STARS itself is the one exception, kept in config.py
# because it was explicitly asked for as an env-tunable setting — still
# bump this version in the same deploy whenever that changes.)
PRICE_VERSION = "v4"

# Telegram Stars (XTR) packages. "stars" is charged to the user, "messages" is
# credited to their bonus quota on successful payment.
PACKAGES = [
    {"messages": 25, "stars": 25},
    {"messages": 100, "stars": 75},
]

# Unlimited-access time passes: no per-message limit at all while active,
# for the "срочно на уроке" use case where counting messages is the wrong
# mental model. Adjust prices freely — these are starting points.
TIME_PACKAGES = [
    {"minutes": 60, "stars": 45, "label": "1 час"},
]

# Monthly Stars subscription — auto-renewing, but NOT unlimited (see
# SUBSCRIPTION_DAILY_MESSAGES/SUBSCRIPTION_DAILY_PREMIUM_MESSAGES in
# config.py and db.try_consume_message). Telegram currently only allows
# exactly 2592000 seconds (30 days) for subscription_period; there's no
# other option to pick here. Max allowed subscription price is 10000 Stars.
SUBSCRIPTION_PERIOD_SECONDS = 2592000
SUBSCRIPTION = {"days": 30, "stars": SUBSCRIPTION_PRICE_STARS}


def _discount_pct(base_rate: float, rate: float) -> int:
    """% cheaper per message `rate` is than `base_rate` (0 if not cheaper)
    — used so the purchase menu doesn't let the cheapest, smallest package
    look like the obvious pick when bigger ones are better value."""
    if base_rate <= 0 or rate >= base_rate:
        return 0
    return round((1 - rate / base_rate) * 100)


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
    elif kind == "subscription":
        table = [SUBSCRIPTION]
    else:
        return None
    if not (0 <= idx < len(table)):
        return None
    return table[idx]


def packages_keyboard() -> InlineKeyboardMarkup:
    rows = []
    # Per-message rate of the cheapest/smallest package is the baseline
    # every other option's "выгоднее на N%" is measured against.
    base_rate = PACKAGES[0]["stars"] / PACKAGES[0]["messages"]
    for i, pkg in enumerate(PACKAGES):
        rate = pkg["stars"] / pkg["messages"]
        discount = _discount_pct(base_rate, rate)
        suffix = f" (выгоднее на {discount}%)" if discount else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{pkg['messages']} сообщений — {pkg['stars']} ⭐{suffix}",
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
    # Best-case value: up to SUBSCRIPTION_DAILY_MESSAGES fast messages/day
    # for the whole period, compared at the same per-message basis as PACKAGES.
    sub_rate = SUBSCRIPTION["stars"] / (SUBSCRIPTION_DAILY_MESSAGES * SUBSCRIPTION["days"])
    sub_discount = _discount_pct(base_rate, sub_rate)
    sub_suffix = f", выгоднее на {sub_discount}%" if sub_discount else ""
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    f"⭐ Подписка — {SUBSCRIPTION['stars']} ⭐/мес, "
                    f"до {SUBSCRIPTION_DAILY_MESSAGES} запросов/день{sub_suffix}"
                ),
                callback_data="buysub:0",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
