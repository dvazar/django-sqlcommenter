"""The unit of work a SQL query belongs to, carried in a ContextVar.

A *scope* is the set of tags describing what is running right now — an
HTTP request, a background task, a management command, a scheduled job.
It is computed once at the entry point and only read on the query path,
so a request making 200 queries pays for it once.

This is the extension point: an adapter for any framework (a task queue,
a scheduler, a consumer loop) opens a scope with :func:`sql_scope` and
everything else follows. Adapters do not belong in this package — most
frameworks already let you register your own middleware.

A ContextVar rather than ``connection.execute_wrapper()`` because the
latter pushes onto per-connection state whose lifetime does not match a
request under ASGI: the wrapper would be popped before a streaming
response has produced its body.

A scope does not cross into a thread you start yourself: ``ContextVar``
values are per-context, and a bare ``threading.Thread`` starts an empty
one. Open a scope inside the thread instead. ``asyncio.to_thread`` and
asgiref's ``sync_to_async`` both copy the context, so those do carry it.

This module imports nothing from Django.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

__all__ = [
    "SqlScope",
    "current_scope",
    "reset_scope",
    "set_scope",
    "sql_scope",
    "sql_tags",
]


@dataclass(frozen=True, slots=True)
class SqlScope:
    """Tags describing the current unit of work.

    Attributes:
        tags: Key-value pairs to serialize into the comment.
        callsite: Whether to resolve the calling source line per query.
            The context manager spells this ``with_callsite`` because
            :func:`sql_scope` takes ``**tags`` — a bare ``callsite=``
            there would be swallowed by the flag instead of becoming a
            tag of that name.
    """

    tags: Mapping[str, str]
    callsite: bool = True


_scope: contextvars.ContextVar[SqlScope | None] = contextvars.ContextVar(
    "sqlcommenter_scope",
    default=None,
)


def current_scope() -> SqlScope | None:
    """Return the active scope, or ``None`` outside any unit of work."""
    return _scope.get()


def set_scope(
    scope: SqlScope | None,
) -> contextvars.Token[SqlScope | None]:
    """Set the active scope; pass the token to :func:`reset_scope`."""
    return _scope.set(scope)


def reset_scope(token: contextvars.Token[SqlScope | None]) -> None:
    """Restore the scope that was active when ``token`` was issued."""
    _scope.reset(token)


@contextmanager
def sql_scope(*, with_callsite: bool = True, **tags: str) -> Iterator[None]:
    """Open a fresh scope, discarding any enclosing one.

    Replacing rather than merging is deliberate: a task picked up by a
    worker belongs to the task, not to the command that happened to
    host the worker process.

    Pass ``with_callsite=False`` on bulk paths issuing tens of thousands
    of queries, where the per-query frame walk stops being free.

    Do not hold a scope open across a ``yield`` in a generator you hand
    to someone else. A generator has no context of its own, so the scope
    stays visible to whoever resumed it until the generator is
    exhausted, and resuming it from a different context makes the exit
    raise ``ValueError: Token was created in a different Context``. Open
    the scope inside the loop body, or drive :func:`set_scope` and
    :func:`reset_scope` around each step — which is what this package's
    own streaming middleware does.
    """
    token = _scope.set(SqlScope(tags=dict(tags), callsite=with_callsite))
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def sql_tags(**extra: str) -> Iterator[None]:
    """Merge ``extra`` into the current scope for the duration of the block.

    A no-op outside any scope: tags with no unit of work behind them
    would annotate queries nothing asked to be annotated.
    """
    current = _scope.get()
    if current is None:
        yield
        return
    merged = SqlScope(
        tags={**current.tags, **extra},
        callsite=current.callsite,
    )
    token = _scope.set(merged)
    try:
        yield
    finally:
        _scope.reset(token)
