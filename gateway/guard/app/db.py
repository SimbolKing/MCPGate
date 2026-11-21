from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base


def _is_sqlite_memory(database_url: str) -> bool:
    url = make_url(database_url)
    return url.drivername.startswith("sqlite") and (url.database in (None, "", ":memory:"))


def create_db_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    kwargs: dict[str, Any] = {}
    if url.drivername.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if _is_sqlite_memory(database_url):
            kwargs["poolclass"] = StaticPool
        elif url.database:
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, **kwargs)

    if url.drivername.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(bind=engine)


def reset_db(engine: Engine) -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def get_session(request: Request) -> Generator[Session, None, None]:
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    with session_factory() as session:
        yield session
