import sqlite3
import threading

from bot.config import DB_PATH, STARTING_BALANCE

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS players (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER NOT NULL
    )
    """
)
_conn.commit()


def get_or_create_player(user_id: int, username: str | None) -> int:
    with _lock:
        cur = _conn.execute("SELECT balance FROM players WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            _conn.execute(
                "INSERT INTO players (user_id, username, balance) VALUES (?, ?, ?)",
                (user_id, username, STARTING_BALANCE),
            )
            _conn.commit()
            return STARTING_BALANCE
        _conn.execute(
            "UPDATE players SET username = ? WHERE user_id = ?", (username, user_id)
        )
        _conn.commit()
        return row[0]


def get_balance(user_id: int) -> int:
    with _lock:
        cur = _conn.execute("SELECT balance FROM players WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 0


def set_balance(user_id: int, balance: int) -> None:
    with _lock:
        _conn.execute(
            "UPDATE players SET balance = ? WHERE user_id = ?", (balance, user_id)
        )
        _conn.commit()


def top_players(limit: int = 10) -> list[tuple[str | None, int]]:
    with _lock:
        cur = _conn.execute(
            "SELECT username, balance FROM players ORDER BY balance DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()
