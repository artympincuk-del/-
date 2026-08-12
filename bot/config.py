import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
STARTING_BALANCE = int(os.environ.get("STARTING_BALANCE", "1000"))
DB_PATH = os.environ.get("DB_PATH", "roulette.db")
