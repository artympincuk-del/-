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
DAILY_FREE_PREMIUM_MESSAGES = int(os.environ.get("DAILY_FREE_PREMIUM_MESSAGES", "3"))
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

ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()
}
