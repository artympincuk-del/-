import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

# Optional — only needed for photo editing (kontext model via Pollinations'
# authenticated POST /v1/images/edits). Text-to-image generation works fine
# without it (anonymous flux, unlimited and free). Get a key at
# https://enter.pollinations.ai/keys — without one, photo editing degrades
# to a friendly "unavailable" message instead of crashing.
POLLINATIONS_API_KEY = os.environ.get("POLLINATIONS_API_KEY", "")

FAST_MODEL = os.environ.get("FAST_MODEL", "openai/gpt-oss-20b")
PREMIUM_MODEL = os.environ.get("PREMIUM_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen/qwen3.6-27b")
STT_MODEL = os.environ.get("STT_MODEL", "whisper-large-v3-turbo")
FAST_REASONING_EFFORT = os.environ.get("FAST_REASONING_EFFORT", "low")
PREMIUM_REASONING_EFFORT = os.environ.get("PREMIUM_REASONING_EFFORT", "high")
PREMIUM_CREDIT_COST = int(os.environ.get("PREMIUM_CREDIT_COST", "3"))

# Groq's free-tier tokens-per-minute (TPM) ceiling per model — the hard cap
# a single request (prompt tokens + the max_tokens reserved for the
# response) must fit under, or Groq rejects it outright with a 413. This is
# Groq's own tariff limit (https://console.groq.com/docs/rate-limits), not
# something this bot controls — update this dict if the Groq account ever
# moves off the free/developer plan and gets higher limits.
# AITUNNEL — российский агрегатор с OpenAI-совместимым API и оплатой рублями.
# Подключён вторым провайдером: у него нет минутных лимитов Groq, поэтому он
# используется как запас, когда Groq упирается в свой TPM. Ключ пустой =
# провайдер выключен, бот работает как раньше, только на Groq.
AITUNNEL_API_KEY = os.environ.get("AITUNNEL_API_KEY", "")
AITUNNEL_BASE_URL = os.environ.get("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/")
# Модели, которые нужно слать в AITUNNEL, а не в Groq. Список, а не префикс:
# у агрегатора id вида "qwen3.7-flash" ничем не отличаются от груковских по
# форме, угадывать нельзя.
AITUNNEL_MODELS = {
    m.strip()
    for m in os.environ.get(
        "AITUNNEL_MODELS", "qwen3.7-flash,gemini-3.5-flash-lite"
    ).split(",")
    if m.strip()
}

MODEL_TOKEN_CEILINGS = {
    "openai/gpt-oss-20b": 8000,
    "openai/gpt-oss-120b": 8000,
    "qwen/qwen3.6-27b": 8000,
    "llama-3.1-8b-instant": 6000,
    "llama-3.3-70b-versatile": 12000,
    # У агрегатора нет минутного лимита, ограничение только по балансу,
    # поэтому потолок ставим по размеру контекста, а не по тарифу.
    "qwen3.7-flash": 128000,
    "gemini-3.5-flash-lite": 128000,
}
DEFAULT_MODEL_TOKEN_CEILING = 8000  # fallback for a model not listed above

# Default response budget (max_tokens) per model when a caller doesn't ask
# for something more specific — unchanged (4096) for the gpt-oss models this
# bot uses by default; noticeably lower for a model with a much smaller
# ceiling above, where 4096 alone could already eat most of its whole
# per-minute budget before any prompt tokens are even counted.
MODEL_RESPONSE_TOKENS = {
    "openai/gpt-oss-20b": 4096,
    "openai/gpt-oss-120b": 4096,
    "qwen/qwen3.6-27b": 4096,
    "llama-3.1-8b-instant": 1024,
    "llama-3.3-70b-versatile": 4096,
    # AITUNNEL models have no per-minute ceiling (see MODEL_TOKEN_CEILINGS
    # above), so there's no reason to squeeze them down to the 2048 default.
    "qwen3.7-flash": 4096,
    "gemini-3.5-flash-lite": 4096,
}
DEFAULT_MODEL_RESPONSE_TOKENS = 2048

# Per-model daily sub-limit for PAYING tiers — checked and consumed IN
# ADDITION to (not instead of) the normal daily quota above: a request to a
# sub-limited model spends one unit from the normal quota AND one from this
# counter, never a separate budget stacked on top of it. Exists for
# AITUNNEL models that bill real money per call (unlike Groq). Only applies
# to an active subscription, active purchased/admin-granted unlimited pass,
# or spent bonus_credits (see handlers._is_paid_tier) — the free tier uses
# MODEL_TRIAL_TOTALS below instead, not this dict.
MODEL_DAILY_SUBLIMIT_PAID = int(os.environ.get("MODEL_DAILY_SUBLIMIT_PAID", "5"))
# A recurring monthly subscription gets DOUBLE the base paid limit — worth
# more than a one-off message package or a single purchased/gifted hour of
# unlimited access, both of which stay at MODEL_DAILY_SUBLIMIT_PAID. Its own
# env var (not "paid * 2" inlined at every call site) so it can be retuned
# independently later; defaults to exactly double.
MODEL_DAILY_SUBLIMIT_SUBSCRIPTION = int(
    os.environ.get("MODEL_DAILY_SUBLIMIT_SUBSCRIPTION", str(MODEL_DAILY_SUBLIMIT_PAID * 2))
)
MODEL_DAILY_SUBLIMITS = {
    "gemini-3.5-flash-lite": {
        "paid": MODEL_DAILY_SUBLIMIT_PAID,
        "subscription": MODEL_DAILY_SUBLIMIT_SUBSCRIPTION,
    },
}

# Правка 1: the free tier gets a one-time TRIAL instead of a recurring daily
# allowance — a "N per day, forever, for every free user" allowance has no
# upper bound as the user count grows (this was written after a ~100-user
# spike was expected from a one-off blogger promo: at the old 2/day free
# rate, that alone was projected to burn the whole manually-topped-up
# budget in about two weeks, with no ceiling on it). A trial does have a
# ceiling: at most MODEL_TRIAL_TOTAL_FREE per free user, ever, never
# refilled — see db.model_trial_usage / handlers._enforce_model_sublimit.
MODEL_TRIAL_TOTAL_FREE = int(os.environ.get("MODEL_TRIAL_TOTAL_FREE", "5"))
MODEL_TRIAL_TOTALS = {
    "gemini-3.5-flash-lite": MODEL_TRIAL_TOTAL_FREE,
}

# Models that accept an image/file directly in the request and don't need
# the two-stage Groq-vision-then-solve pipeline (see handlers.py's photo/PDF
# handling) — a set, not a single constant, since more multimodal models
# are expected later.
MULTIMODAL_MODELS = {"gemini-3.5-flash-lite"}

# Backup provider for when Groq itself is the problem (rate/size limits),
# routed through AITUNNEL — see handlers._ask_ai_with_fallback. Not offered
# as a menu choice (Qwen alone is strictly worse than Groq's free, faster
# models or Gemini's stronger/multimodal one — see MODEL_OPTIONS), but it
# has no per-minute ceiling and costs a fraction of Gemini, which is
# exactly what an emergency backstop needs. Empty disables the fallback
# entirely.
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "qwen3.7-flash")
# Two independent spend brakes on the fallback (it's real AITUNNEL money,
# triggered automatically without the user asking for it): a global cap
# across every user, and a per-user cap so one stuck retry loop or one
# heavy user can't eat the whole day's budget alone.
FALLBACK_DAILY_CAP = int(os.environ.get("FALLBACK_DAILY_CAP", "300"))
FALLBACK_USER_DAILY_CAP = int(os.environ.get("FALLBACK_USER_DAILY_CAP", "20"))


def _parse_cost_map(raw: str) -> dict:
    """Parses "model_id=price,model_id=price" into {model_id: price}. Same
    "comma list in an env var, set() or dict() at import time" convention
    as AITUNNEL_MODELS above — silently skips a malformed entry rather than
    crashing startup over a typo in an operational tuning value."""
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        model_id, _, price = pair.partition("=")
        model_id = model_id.strip()
        try:
            result[model_id] = float(price.strip())
        except ValueError:
            continue
    return result


# Правка 2: rough estimated cost per request, in rubles — this bot's OWN
# running tally of spend (see db.record_model_cost / get_cost_summary and
# DAILY_COST_CAP below), never the provider's real invoice; that's only
# ever knowable on the provider's own billing dashboard. Read from the
# environment (not hardcoded) so the average can be retuned as real billing
# data comes in without a deploy. Any model not listed here — every Groq
# model, image generation, STT — costs nothing and is never charged against
# DAILY_COST_CAP. Defaults are from actual measured AITUNNEL billing:
# Gemini ran 0.12-0.53₽/request, ~0.3₽ on average; Qwen (the fallback) ran
# ~0.05₽.
MODEL_COST_PER_REQUEST = _parse_cost_map(
    os.environ.get("MODEL_COST_PER_REQUEST", "gemini-3.5-flash-lite=0.3,qwen3.7-flash=0.05")
)

# Правка 3: once today's estimated spend (db.get_today_cost, using the
# prices above) reaches this many rubles, paid models (and the reserve
# fallback to Qwen) stop being offered to FREE-tier users for the rest of
# the day — Groq-based models keep working for them as always, and a
# subsequent Groq outage just fails normally rather than falling back to a
# paid provider on someone else's dime. Never affects a paying user
# (active subscription, unlimited pass, or bonus_credits on hand) — they
# already paid for their allowance, so cutting them off isn't an option.
# 0 disables the whole cap: free users keep getting paid models exactly as
# the trial/sub-limit above already governs, with no extra spend ceiling.
DAILY_COST_CAP = float(os.environ.get("DAILY_COST_CAP", "30"))

DAILY_FREE_MESSAGES = int(os.environ.get("DAILY_FREE_MESSAGES", "10"))
DAILY_FREE_PREMIUM_MESSAGES = int(os.environ.get("DAILY_FREE_PREMIUM_MESSAGES", "5"))
# Images (generate from scratch AND edit a photo) get their own separate
# daily pool — NOT shared with the premium-chat quota above. Kept flat
# across free/subscription/promo tiers on purpose (no elevated allowance
# for paying tiers yet): generation itself is free (flux), but editing
# costs real money per call (kontext, ~$0.04), so this is the one dial
# that directly controls actual out-of-pocket spend, independent of
# whatever the premium chat quota is tuned to.
DAILY_FREE_IMAGE_MESSAGES = int(os.environ.get("DAILY_FREE_IMAGE_MESSAGES", "5"))
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

# Rough ceiling (in estimated tokens, not real ones — see ai._estimate_tokens)
# on how much dialog history rides along with a request, on top of the
# MAX_HISTORY_TURNS count cap above. Exists so the common "long
# photo-recognized problem + a chat history" case doesn't blow past Groq's
# free-tier per-minute token budget and get flat-out refused (413) — history
# gets trimmed newest-to-oldest to fit under this before every request. The
# system prompt and the current message are never counted against/trimmed by
# this budget, only history is.
REQUEST_TOKEN_BUDGET = int(os.environ.get("REQUEST_TOKEN_BUDGET", "4000"))

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

# Same idea as GROQ_MAX_CONCURRENT, but for AITUNNEL — separate cap because
# the reason isn't a provider rate limit (AITUNNEL doesn't have Groq's TPM
# ceiling), it's money: AITUNNEL is billed per call, so an unbounded burst
# or a runaway loop burns through the balance in minutes.
AITUNNEL_MAX_CONCURRENT = int(os.environ.get("AITUNNEL_MAX_CONCURRENT", "3"))

# Правка 6: the groq SDK retries a 429/5xx/connection error internally,
# with its own backoff, BEFORE it ever raises anything our code can see —
# invisible retries, invisible delay. Measured live: the same fast model
# answered in 0.5s one moment and 34.6s the next, because the SDK's
# default (2 retries = up to 3 attempts, each waiting on Groq's own
# Retry-After) silently ate 30+ seconds before finally giving up and
# raising, at which point handlers._ask_ai_with_fallback would have
# already routed to the reserve model in a fraction of that time. Lowering
# this makes Groq give up and hand off to the fallback sooner — the
# fallback logic itself (daily caps, honest "who answered" footer) is
# unchanged, this only controls how many attempts happen before Groq's own
# client concedes. Only applies to the Groq client — AITUNNEL keeps the
# SDK default, it's not what this was slow on.
GROQ_MAX_RETRIES = int(os.environ.get("GROQ_MAX_RETRIES", "1"))

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

# Hour (0-23, QUOTA_TZ) the daily DB backup is sent to every ADMIN_IDS
# member — see main._backup_loop. Empty or "off" (case-insensitive)
# disables the daily job; /backup still works on demand either way. Kept
# as a raw string here (not int()'d) since "off" is a valid value and
# validation/logging on a bad value belongs in main.py, after logging is
# configured — this module loads too early for a warning to reliably show.
BACKUP_HOUR = os.environ.get("BACKUP_HOUR", "4")
