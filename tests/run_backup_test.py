import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "backup_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for _ext in ("", "-wal", "-shm"):
    _p = os.environ["DB_PATH"] + _ext
    if os.path.exists(_p):
        os.remove(_p)

from bot import db  # noqa: E402
from bot.handlers import cmd_backup, send_database_backup  # noqa: E402
from bot.main import _parse_backup_hour  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeUser:
    def __init__(self, id):
        self.id = id


class FakeMessage:
    def __init__(self, text, user_id):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = FakeBot()
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.sent_documents = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text))

    async def send_document(self, chat_id, document, **kwargs):
        # aiogram's FSInputFile exposes .path. The caller (send_database_backup)
        # deletes this file in a finally block right after this call returns,
        # so inspect its content NOW, not after send_database_backup returns.
        path = document.path
        existed_at_send_time = os.path.exists(path)
        tables, players_count, chat_log_count = None, None, None
        if existed_at_send_time:
            conn = sqlite3.connect(path)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            cur = conn.execute("SELECT COUNT(*) FROM players")
            players_count = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM chat_log")
            chat_log_count = cur.fetchone()[0]
            conn.close()
        self.sent_documents.append(
            (chat_id, path, existed_at_send_time, tables, players_count, chat_log_count)
        )


ADMIN_ID = 9999
NON_ADMIN_ID = 1234


# --- Seed some real data so the backup has something to verify ---
for i in range(20):
    db._ensure_player(1000 + i, f"user{i}")
    db.log_message(1000 + i, f"user{i}", "user", f"hello {i}")
    db.log_message(1000 + i, f"user{i}", "assistant", f"hi {i}")

with db._lock:
    cur = db._conn.execute("SELECT COUNT(*) FROM players")
    (original_players,) = cur.fetchone()
    cur = db._conn.execute("SELECT COUNT(*) FROM chat_log")
    (original_chat_log,) = cur.fetchone()
    cur = db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    original_tables = {row[0] for row in cur.fetchall()}


async def run():
    # --- 1. /backup from a non-admin does nothing ---
    msg = FakeMessage("/backup", NON_ADMIN_ID)
    await cmd_backup(msg)
    check("non-admin /backup sends nothing", msg.sent == [] and msg.bot.sent_documents == [])

    # --- 2. Backup opens as a valid SQLite DB with matching tables/row counts ---
    fake_bot = FakeBot()
    await send_database_backup(fake_bot, ADMIN_ID)
    check("backup sent exactly one document to the admin", len(fake_bot.sent_documents) == 1)
    (
        sent_chat_id, backup_path, existed_at_send_time,
        backup_tables, backup_players, backup_chat_log,
    ) = fake_bot.sent_documents[0]
    check("backup document sent to the admin's own id", sent_chat_id == ADMIN_ID)
    check("backup file existed at send time (before cleanup)", existed_at_send_time is True)
    check("backup has the same tables as the original", backup_tables == original_tables)
    check(f"players row count matches ({backup_players} == {original_players})", backup_players == original_players)
    check(f"chat_log row count matches ({backup_chat_log} == {original_chat_log})", backup_chat_log == original_chat_log)

    # --- 4. Temp file is gone after the send completes ---
    check("temp backup file removed after send", not os.path.exists(backup_path))

    # --- 3. A backup taken during active concurrent writes is still valid ---
    stop_writing = threading.Event()

    def writer_thread():
        i = 0
        while not stop_writing.is_set():
            db._ensure_player(2000 + (i % 50), f"writer{i}")
            db.log_message(2000 + (i % 50), f"writer{i}", "user", f"spam {i}")
            i += 1
            time.sleep(0.001)

    t = threading.Thread(target=writer_thread, daemon=True)
    t.start()
    time.sleep(0.05)  # let the writer get going first

    # The destination directory is created here and removed with everything
    # in it, rather than assuming some path already exists on the machine:
    # this used to point at a hardcoded /tmp/pricing_test/, which only
    # existed as leftovers from earlier runs — on a clean machine
    # sqlite3.connect() failed outright with "unable to open database file".
    with tempfile.TemporaryDirectory() as concurrent_dir:
        concurrent_dest = os.path.join(concurrent_dir, "concurrent_backup.db")
        await asyncio.to_thread(db.backup_database, concurrent_dest)
        stop_writing.set()
        t.join(timeout=2)

        concurrent_conn = sqlite3.connect(concurrent_dest)
        try:
            cur = concurrent_conn.execute("PRAGMA integrity_check")
            integrity = cur.fetchone()[0]
            check(
                f"backup taken during concurrent writes passes integrity_check (got {integrity!r})",
                integrity == "ok",
            )
            cur = concurrent_conn.execute("SELECT COUNT(*) FROM players")
            (concurrent_players,) = cur.fetchone()
            check(
                f"concurrent backup has a plausible player count (got {concurrent_players})",
                concurrent_players > original_players,
            )
        finally:
            # Must close before the TemporaryDirectory is torn down, or the
            # open handle keeps the file alive/undeletable on some platforms.
            concurrent_conn.close()

    # --- 5. BACKUP_HOUR=off disables the scheduler ---
    check("_parse_backup_hour('off') -> None", _parse_backup_hour("off") is None)
    check("_parse_backup_hour('OFF') -> None (case-insensitive)", _parse_backup_hour("OFF") is None)
    check("_parse_backup_hour('') -> None", _parse_backup_hour("") is None)
    check("_parse_backup_hour('   ') -> None", _parse_backup_hour("   ") is None)
    check("_parse_backup_hour('4') -> 4", _parse_backup_hour("4") == 4)
    check("_parse_backup_hour('notanumber') -> None (invalid, disabled not crashed)", _parse_backup_hour("notanumber") is None)
    check("_parse_backup_hour('99') -> None (out of range, disabled not crashed)", _parse_backup_hour("99") is None)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
