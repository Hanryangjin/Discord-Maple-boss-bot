from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import threading
import time


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.lock:
            conn = self.connect()
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.executescript(Path(__file__).with_name('schema.sql').read_text())
            finally:
                conn.close()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @contextmanager
    def transaction(self, write=True):
        with self.lock:
            conn = self.connect()
            try:
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self, conn, guild_id):
        conn.execute('INSERT OR IGNORE INTO guild_settings(guild_id) VALUES(?)', (guild_id,))
        settings = conn.execute('SELECT * FROM guild_settings WHERE guild_id=?', (guild_id,)).fetchone()
        if not settings['initialized']:
            names = json.loads(Path(__file__).with_name('jobs.json').read_text())['jobs']
            from .validation import key
            conn.executemany('INSERT INTO jobs(guild_id,name,name_key) VALUES(?,?,?)',
                             [(guild_id, n, key(n)) for n in names])
            conn.execute('INSERT INTO cycles(guild_id) VALUES(?)', (guild_id,))
            conn.execute('UPDATE guild_settings SET initialized=1 WHERE guild_id=?', (guild_id,))

    def backup(self, directory: str) -> str:
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f'maplebot-{time.time_ns()}.sqlite3'
        with self.lock:
            source = self.connect()
            copy = sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
                source.close()
        return str(target)
