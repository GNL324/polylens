import os
import sqlite3
import tempfile

from src.sqlite_utils import closing_connection


def test_closing_connection_releases_file_descriptor():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = f"{tmp}/probe.db"
        before = len(os.listdir("/proc/self/fd"))
        with closing_connection(db_path, row_factory=None) as conn:
            conn.execute("CREATE TABLE t(x INTEGER)")
        after = len(os.listdir("/proc/self/fd"))
        assert after <= before + 1

        with closing_connection(db_path, row_factory=sqlite3.Row) as conn:
            conn.execute("INSERT INTO t(x) VALUES (1)")
        assert len(os.listdir("/proc/self/fd")) <= before + 1

        with closing_connection(db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute("SELECT x FROM t").fetchone()
        assert row[0] == 1
