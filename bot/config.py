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

DB_PATH = os.environ.get("DB_PATH", "assistant.db")
MAX_HISTORY_TURNS = 10

ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()
}
