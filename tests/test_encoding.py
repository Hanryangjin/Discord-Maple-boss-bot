"""Packaged Korean resources must not depend on the host's default encoding."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EncodingTests(unittest.TestCase):
    def test_initialization_with_non_utf8_default_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            script = '''
import codecs
import locale
import sys
from maplebot.db import Database
assert codecs.lookup(locale.getpreferredencoding(False)).name != 'utf-8'
db = Database(sys.argv[1])
with db.transaction() as conn:
    db.initialize(conn, 1)
    assert conn.execute('SELECT COUNT(*) FROM jobs WHERE guild_id=1').fetchone()[0] > 0
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
'''
            env = {**os.environ, 'LC_ALL': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'}
            result = subprocess.run(
                [sys.executable, '-X', 'utf8=0', '-c', script, str(Path(directory) / 'db.sqlite3')],
                env=env, capture_output=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
