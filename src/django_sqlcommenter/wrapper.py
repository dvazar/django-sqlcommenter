"""The ``execute_wrapper`` that injects the sqlcomment into every query.

Installed once per connection via the ``connection_created`` signal
rather than pushed and popped per request. Two reasons:

* ``execute_wrappers`` is per-connection state
  (``django/db/backends/base/base.py``) whose lifetime does not match a
  request under ASGI;
* a streaming response produces its body *after* the middleware chain
  returns, so a per-request wrapper would already be popped and every
  query made during streaming would go unannotated.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db.backends.signals import connection_created
from django.dispatch import receiver

from ._report import report_once
from .callsite import resolve_callsite
from .comment import build_comment
from .conf import TRAILING, conf
from .scope import current_scope

__all__ = ["SKIP_PREFIXES", "QueryAnnotator", "install_on_connection"]

#: Transaction bookkeeping the ORM issues on its own behalf. Annotating
#: it costs bytes and tells nobody anything.
SKIP_PREFIXES = (
    "SAVEPOINT",
    "RELEASE",
    "ROLLBACK",
    "BEGIN",
    "COMMIT",
    "SET ",
    "SHOW ",
)


class QueryAnnotator:
    """Inject a sqlcomment describing the current scope into each query."""

    def __call__(
        self,
        execute: Callable[..., Any],
        sql: str,
        params: Any,
        many: bool,
        context: Any,
    ) -> Any:
        try:
            sql = self._annotate(sql, params)
        except Exception as error:
            # A comment is telemetry. Whatever went wrong building it —
            # a scope handed a non-mapping through the public
            # set_scope, a tag key that will not sort, a statement that
            # is not str — the statement itself still has to run.
            # BaseException is deliberately not caught: KeyboardInterrupt,
            # SystemExit and CancelledError must pass through.
            report_once(
                f"Failed to annotate a SQL query "
                f"({type(error).__name__}); the query ran without a "
                f"comment."
            )
        return execute(sql, params, many, context)

    def _annotate(self, sql: str, params: Any) -> str:
        """Return ``sql`` with a comment spliced in, or unchanged."""
        scope = current_scope()
        # Ordered by cost: most queries have no scope at all, and the
        # settings lookup is cheaper than upper-casing the statement.
        if (
            scope is None
            or not conf.ENABLED
            or sql[:20].upper().startswith(SKIP_PREFIXES)
        ):
            return sql

        tags: dict[str, str] = {**conf.TAGS, **scope.tags}
        if scope.callsite and conf.CALLSITE:
            # setdefault: an explicit callsite tag from the caller wins.
            tags.setdefault("callsite", resolve_callsite() or "unknown")

        comment = build_comment(tags, max_length=conf.MAX_LENGTH)
        if not comment:
            return sql

        if params is not None:
            # The driver interpolates %-placeholders only when params
            # are passed. Percent signs produced by URL-encoding must be
            # doubled — but only then, or a literal %% reaches the log.
            comment = comment.replace("%", "%%")

        separator = conf.SEPARATOR
        if conf.POSITION == TRAILING:
            return f"{sql}{separator}{comment}"
        return f"{comment}{separator}{sql}"


_ANNOTATOR = QueryAnnotator()


@receiver(connection_created, dispatch_uid="django_sqlcommenter")
def install_on_connection(
    sender: Any = None,
    connection: Any = None,
    **kwargs: Any,
) -> None:
    """Append the annotator to a freshly opened connection, at most once.

    ``SQLCOMMENT_ENABLED`` is checked here *and* on every query: this
    keeps a disabled deployment from paying for the wrapper at all,
    while :class:`QueryAnnotator` still honors the switch when it is
    flipped at runtime on a connection that already carries one.
    """
    if not conf.ENABLED or connection is None:
        return
    if any(
        isinstance(wrapper, QueryAnnotator)
        for wrapper in connection.execute_wrappers
    ):
        return
    connection.execute_wrappers.append(_ANNOTATOR)
