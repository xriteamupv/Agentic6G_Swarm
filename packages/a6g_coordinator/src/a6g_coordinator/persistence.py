"""Checkpointer + store backends for the in-process coordinator host (SPEC A1 12.3).

* ``postgres``: ``AsyncPostgresSaver`` + ``AsyncPostgresStore`` (the reference deployment
  without the Agent Server: mission state survives a coordinator kill/restart);
* ``sqlite``: a local file for both (development, the simulation stack);
* ``memory``: in-process only (unit tests).
"""

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore


@contextlib.asynccontextmanager
async def open_persistence(
    kind: str, *, dsn: str | None = None, path: Path | str | None = None
) -> AsyncIterator[tuple[BaseCheckpointSaver, BaseStore]]:
    if kind == "postgres":
        if not dsn:
            raise ValueError("postgres persistence needs a DSN (--dsn or $A6G_PG_DSN)")
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore

        async with (
            AsyncPostgresSaver.from_conn_string(dsn) as saver,
            AsyncPostgresStore.from_conn_string(dsn) as store,
        ):
            await saver.setup()
            await store.setup()
            yield saver, store
    elif kind == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from langgraph.store.sqlite.aio import AsyncSqliteStore

        base = Path(path or "var/coordinator")
        base.parent.mkdir(parents=True, exist_ok=True)
        async with (
            AsyncSqliteSaver.from_conn_string(f"{base}.checkpoints.sqlite") as saver,
            AsyncSqliteStore.from_conn_string(f"{base}.store.sqlite") as store,
        ):
            await store.setup()
            yield saver, store
    elif kind == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        yield InMemorySaver(), InMemoryStore()
    else:
        raise ValueError(f"unknown persistence {kind!r} (postgres | sqlite | memory)")
