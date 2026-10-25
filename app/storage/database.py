"""Connection management and transactional helpers for SQLite.

A fresh connection is opened per operation. This keeps connections out of
asyncio task/thread hand-offs and lets multiple bot processes safely share one
database file. WAL mode, a busy timeout, and a bounded retry loop handle the
short lock windows that remain.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from app.errors import AppError
from app.storage.migrations import apply_migrations, get_schema_version

logger = logging.getLogger(__name__)

T = TypeVar("T")
_LOCK_MESSAGES = ("database is locked", "database table is locked")


class DatabaseError(AppError):
    """Unexpected storage failure."""


def _is_lock_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in _LOCK_MESSAGES
    )


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        write_retries: int = 3,
        retry_base_delay: float = 0.05,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.write_retries = write_retries
        self.retry_base_delay = retry_base_delay

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,  # explicit transactions
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def initialize(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            apply_migrations(conn)
            version = get_schema_version(conn)
        finally:
            conn.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.chmod(0o600)
        logger.info("database ready path=%s schema_version=%s", self.path, version)
        return version

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def write(self, operation: Callable[[], T]) -> T:
        """Run a complete write operation, retrying transient lock conflicts."""

        delay = self.retry_base_delay
        for attempt in range(self.write_retries + 1):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not _is_lock_error(exc) or attempt >= self.write_retries:
                    if _is_lock_error(exc):
                        raise DatabaseError(
                            "database stayed locked; retry later or increase busy_timeout"
                        ) from exc
                    raise
                logger.warning(
                    "sqlite lock conflict attempt=%s/%s; retrying in %.2fs",
                    attempt + 1,
                    self.write_retries,
                    delay,
                )
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self.transaction(write=False) as conn:
            return operation(conn)
