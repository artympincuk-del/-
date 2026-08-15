import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

FAST_MODEL = os.environ.get("FAST_MODEL", "openai/gpt-oss-20b")
PREMIUM_MODEL = os.environ.get("PREMIUM_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen/qwen3.6-27b")
STT_MODEL = os.environ.get("STT_MODEL", "whisper-large-v3-turbo")
FAST_REASONING_EFFORT = os.environ.get("FAST_REASONING_EFFORT", "low")
PREMIUM_REASONING_EFFORT = os.environ.get("PREMIUM_REASONING_EFFORT", "high")
PREMIUM_CREDIT_COST = int(os.environ.get("PREMIUM_CREDIT_COST", "3"))
IMAGE_CREDIT_COST = int(os.environ.get("IMAGE_CREDIT_COST", "5"))

DAILY_FREE_MESSAGES = int(os.environ.get("DAILY_FREE_MESSAGES", "10"))
DAILY_FREE_PREMIUM_MESSAGES = int(os.environ.get("DAILY_FREE_PREMIUM_MESSAGES", "5"))
REFERRAL_BONUS_MESSAGES = int(os.environ.get("REFERRAL_BONUS_MESSAGES", "20"))
# Small immediate welcome gift for whoever clicks a referral link — paid
# right away, unlike REFERRAL_BONUS_MESSAGES below. Not worth gating behind
# anti-abuse checks: a fake account getting a few free messages on its own
# throwaway balance isn't the exploit (the referrer's payout is).
REFERRAL_SIGNUP_BONUS = int(os.environ.get("REFERRAL_SIGNUP_BONUS", "5"))
# Anti-abuse: the *referrer's* bonus isn't paid at /start anymore, only once
# the referred account has sent this many real messages (proves it's not
# just a fake account created to farm the referrer bonus).
REFERRAL_MIN_MESSAGES = int(os.environ.get("REFERRAL_MIN_MESSAGES", "3"))
# ...and even then, at most this many referrals get PAID per referrer per
# day — caps how fast a farm of fake accounts can cash out regardless of how
# many of them clear REFERRAL_MIN_MESSAGES.
REFERRAL_DAILY_CAP = int(os.environ.get("REFERRAL_DAILY_CAP", "5"))

DB_PATH = os.environ.get("DB_PATH", "assistant.db")
MAX_HISTORY_TURNS = 10

# How long chat_log (the admin support/audit trail — see /chatlog) keeps
# messages before they're purged. Unlike dialog_history, chat_log has no
# other cap, so without this it grows forever on Railway's disk.
CHATLOG_RETENTION_DAYS = int(os.environ.get("CHATLOG_RETENTION_DAYS", "30"))

# Caps how many Groq requests (chat/vision/STT/image-prompt-translation — all
# of them, from every user) can be in flight at once, so one user firing off
# ten messages in a row can't burn through the whole free-tier rate limit by
# itself. Free tier is 30 RPM; a handful of concurrent requests is plenty for
# normal multi-user load without needlessly serializing everything.
GROQ_MAX_CONCURRENT = int(os.environ.get("GROQ_MAX_CONCURRENT", "5"))

# Timezone the daily free-message quota resets in, and unlimited-until times
# are displayed in — IANA name for zoneinfo. UTC would reset at 3am Moscow
# time, which is confusing for a bot whose users are mostly there.
QUOTA_TZ = os.environ.get("QUOTA_TZ", "Europe/Moscow")

# Monthly Stars subscription price. Unlike PACKAGES/TIME_PACKAGES (kept in
# payments.py on purpose — see PRICE_VERSION there), this one lives in
# config because it was explicitly asked for as an env-tunable setting.
# Changing it still requires bumping PRICE_VERSION in payments.py in the
# same deploy, same as any other price change, so an invoice created under
# the old price gets rejected at pre-checkout rather than under-charging.
SUBSCRIPTION_PRICE_STARS = int(os.environ.get("SUBSCRIPTION_PRICE_STARS", "199"))

# An active subscription is NOT unlimited — it draws from its own daily
# quota (bigger than the free tier, but still a cap), so one heavy user on
# a flat monthly price can't eat the whole margin. Once this is exhausted
# for the day, a subscriber falls back to spending bonus_credits like
# anyone else, same as the free tier does.
SUBSCRIPTION_DAILY_MESSAGES = int(os.environ.get("SUBSCRIPTION_DAILY_MESSAGES", "30"))
SUBSCRIPTION_DAILY_PREMIUM_MESSAGES = int(
    os.environ.get("SUBSCRIPTION_DAILY_PREMIUM_MESSAGES", "15")
)

# The promo-code time bonus (see db.activate_promo_bonus) is real time-boxed
# access, but — like the subscription above — not truly unlimited within
# that window. A promo link is public and Telegram accounts are free, so
# without a daily cap here it would be an unmetered API-spend faucet for
# anyone who finds the link, not just the partner's real subscribers.
PROMO_BONUS_DAILY_MESSAGES = int(os.environ.get("PROMO_BONUS_DAILY_MESSAGES", "30"))
PROMO_BONUS_DAILY_PREMIUM_MESSAGES = int(
    os.environ.get("PROMO_BONUS_DAILY_PREMIUM_MESSAGES", "5")
)

ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()
}
