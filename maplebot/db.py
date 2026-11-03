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
                conn.executescript(
                    Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                )
                self.migrate_job_icons(conn)
            finally:
                conn.close()

    def migrate_job_icons(self,conn):
        from .job_icons import catalog
        conn.execute('BEGIN IMMEDIATE')
        try:
            columns={r['name'] for r in conn.execute('PRAGMA table_info(jobs)')}
            if 'icon_key' not in columns:
                conn.execute('ALTER TABLE jobs ADD COLUMN icon_key TEXT')
            version=conn.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0] or 1
            if version<2:
                # Version 1 always seeded the original 47 jobs in this immutable order.
                # Bind by original row identity, preserving renamed seed jobs while
                # leaving subsequently added jobs unbound, even if names match.
                order=catalog().get('legacy_seed_order',[])
                if order:
                    guilds=conn.execute('SELECT guild_id FROM guild_settings WHERE initialized=1').fetchall()
                    for guild in guilds:
                        rows=conn.execute('SELECT id FROM jobs WHERE guild_id=? ORDER BY id LIMIT ?',(guild[0],len(order))).fetchall()
                        if len(rows)!=len(order):continue
                        for row,icon_key in zip(rows,order):
                            conn.execute('UPDATE jobs SET icon_key=? WHERE id=? AND icon_key IS NULL',(icon_key,row[0]))
                conn.execute('INSERT OR IGNORE INTO schema_versions VALUES(2)')
            if version<3:
                # Add the requested built-in job once; preserve existing row identity,
                # activation state and any explicitly assigned artwork.
                from .validation import key
                guilds=conn.execute('SELECT guild_id FROM guild_settings WHERE initialized=1').fetchall()
                for guild in guilds:
                    existing=conn.execute('SELECT id,icon_key FROM jobs WHERE guild_id=? AND name_key=?',
                                          (guild[0],key('레테'))).fetchone()
                    if existing is None:
                        conn.execute('INSERT INTO jobs(guild_id,name,name_key,icon_key) VALUES(?,?,?,?)',
                                     (guild[0],'레테',key('레테'),'job-048'))
                    elif existing['icon_key'] is None:
                        conn.execute('UPDATE jobs SET icon_key=?,version=version+1 WHERE id=?',
                                     ('job-048',existing['id']))
                    else:
                        continue
                    conn.execute('UPDATE guild_settings SET revision=revision+1 WHERE guild_id=?',(guild[0],))
                conn.execute('INSERT OR IGNORE INTO schema_versions VALUES(3)')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

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
            names = json.loads(
                Path(__file__).with_name("jobs.json").read_text(encoding="utf-8")
            )["jobs"]
            from .validation import key
            from .job_icons import seed_icon_keys
            images=seed_icon_keys()
            conn.executemany('INSERT INTO jobs(guild_id,name,name_key,icon_key) VALUES(?,?,?,?)',
                             [(guild_id, n, key(n),images.get(key(n))) for n in names])
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
