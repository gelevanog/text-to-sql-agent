"""PostgreSQL connections: the owner (schema, seed, Tally's own tables) and the read-only reader (generated queries).

The two roles are separate logins on purpose. Generated SQL never runs on the owner connection, so even a query that
slipped past the validator would run as a role that can only SELECT allow-listed columns, inside a read-only
transaction, under a statement timeout and row-level security.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, owner_url: str, reader_url: str, *, pool_size: int = 4) -> None:
        self.owner_url = owner_url
        self.reader_url = reader_url
        self._pool_size = pool_size
        self._owner_pool: ConnectionPool | None = None
        self._reader_pool: ConnectionPool | None = None

    def _pool(self, url: str) -> ConnectionPool:
        return ConnectionPool(url, min_size=1, max_size=self._pool_size, open=True, kwargs={"autocommit": True})

    @contextmanager
    def owner(self) -> Iterator[psycopg.Connection]:
        if self._owner_pool is None:
            self._owner_pool = self._pool(self.owner_url)
        with self._owner_pool.connection() as conn:
            yield conn

    @contextmanager
    def reader(self) -> Iterator[psycopg.Connection]:
        if self._reader_pool is None:
            self._reader_pool = self._pool(self.reader_url)
        with self._reader_pool.connection() as conn:
            yield conn

    def close(self) -> None:
        for pool in (self._owner_pool, self._reader_pool):
            if pool is not None:
                pool.close()
        self._owner_pool = self._reader_pool = None


def connect(url: str, *, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=autocommit)
