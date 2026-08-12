import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

FAST_MODEL = os.environ.get("FAST_MODEL", "llama-3.1-8b-instant")
PREMIUM_MODEL = os.environ.get("PREMIUM_MODEL", "llama-3.3-70b-versatile")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen/qwen3.6-27b")
PREMIUM_CREDIT_COST = int(os.environ.get("PREMIUM_CREDIT_COST", "3"))

DAILY_FREE_MESSAGES = int(os.environ.get("DAILY_FREE_MESSAGES", "10"))
DB_PATH = os.environ.get("DB_PATH", "assistant.db")
MAX_HISTORY_TURNS = 10
