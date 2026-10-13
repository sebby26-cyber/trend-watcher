"""Online backup and archive-first restore of state.db (SQLite backup API).

``backup`` copies the live database (WAL-safe) to ``<state>/backups/state-<UTC>.db``
and verifies ``PRAGMA integrity_check``. ``restore`` never deletes: the current
database is first backed up to ``backups/pre-restore-<UTC>.db``, then the chosen
backup is copied in with the same API and migrated forward if it is older.
No schedule is installed by this module (owner decision; see R1-SPEC-DELTAS).
"""
import sqlite3
from pathlib import Path
from . import migrations
from .state import State


def backup(paths, dest=None):
    db = State(paths.state / "state.db")
    try:
        return migrations.backup_to(db.conn, Path(dest) if dest else paths.state / "backups", "state")
    finally:
        db.close()


def verify(path):
    conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    try:
        status = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise ValueError("backup failed integrity check: " + status)
        version = migrations.current_version(conn)
    finally:
        conn.close()
    if version > migrations.LATEST:
        raise ValueError("backup schema v{} is newer than this code (v{})".format(version, migrations.LATEST))
    return version


def restore(paths, source):
    source = Path(source)
    if not source.is_file():
        raise ValueError("no such backup: " + str(source))
    verify(source)
    target = paths.state / "state.db"
    archived = None
    if target.exists():
        current = State(target, migrate=False)
        try:
            archived = migrations.backup_to(current.conn, paths.state / "backups", "pre-restore")
        finally:
            current.close()
    src = sqlite3.connect("file:{}?mode=ro".format(source), uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    State(target).close()  # forward-migrate an older backup and re-enable WAL
    return archived
