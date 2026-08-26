"""Annotate every Django SQL query with the code that issued it.

Add ``"django_sqlcommenter"`` to ``INSTALLED_APPS`` and
``"django_sqlcommenter.middleware.SqlCommentMiddleware"`` first in
``MIDDLEWARE``. Every query then carries a sqlcommenter comment naming
the route, the source line and whatever else you add.

To annotate work that is not an HTTP request or a management command —
a task queue, a scheduler, a consumer loop — open a scope yourself::

    from django_sqlcommenter import sql_scope

    with sql_scope(task="rebuild-index", queue="default"):
        ...

Adapters for specific task queues deliberately do not ship here: every
one of them already lets you register your own middleware, and a scope
is three lines.
"""

from __future__ import annotations

__all__ = [
    "SqlCommentMiddleware",
    "SqlScope",
    "build_comment",
    "current_scope",
    "install",
    "reset_scope",
    "serialize_tags",
    "set_scope",
    "sql_scope",
    "sql_tags",
]


def __dir__() -> list[str]:
    """List the public API, which ``__getattr__`` resolves lazily."""
    return sorted(__all__)


# No `default_app_config`: it was removed in Django 4.1, and this package
# requires 5.2+. Django auto-detects the single AppConfig in apps.py.


def __getattr__(name: str) -> object:
    """Expose the public API without importing Django at package import.

    ``AppConfig`` discovery imports this module before the app registry
    is ready; importing the middleware here would pull in
    ``django.http`` too early.
    """
    if name in {
        "SqlScope",
        "current_scope",
        "reset_scope",
        "set_scope",
        "sql_scope",
        "sql_tags",
    }:
        from . import scope

        return getattr(scope, name)
    if name in {"build_comment", "serialize_tags"}:
        from . import comment

        return getattr(comment, name)
    if name == "SqlCommentMiddleware":
        from .middleware import SqlCommentMiddleware

        return SqlCommentMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def install() -> None:
    """Wire the annotator into this process. Idempotent.

    Called by :class:`~django_sqlcommenter.apps.SqlCommenterConfig`;
    call it yourself only if you are not using ``INSTALLED_APPS``.

    Returns without doing anything when ``SQLCOMMENT_ENABLED`` is off
    at startup, so nothing is installed and flipping the setting on
    later has no effect until the process restarts. That is different
    from turning it off at runtime, which leaves an installed wrapper
    inert.
    """
    from .conf import conf

    if not conf.ENABLED:
        return

    from . import wrapper  # noqa: F401  (registers the signal receiver)
    from .management import patch_management_commands

    patch_management_commands()
