"""Settings access with defaults.

Read lazily and snapshotted rather than captured at import, so Django's
``override_settings`` works — in this package's tests and in the
consumer's.

The snapshot is not tidiness. ``LazySettings`` caches only names it
*finds*, so an unset setting costs a raised and caught
``AttributeError`` on every read — measured at 0.80 us against 0.03 us
once cached. Six of those on the query path came to 4.7 us, more than
``build_comment`` and the callsite frame walk put together.

One consequence worth knowing: assigning to or deleting an attribute
of ``settings`` directly, instead of going through
``override_settings``, does not send ``setting_changed``, so the
snapshot will not notice. Django advises
against that anyway, and ``callsite.py`` already makes the same bet.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property
from typing import Any

from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver

from .comment import DEFAULT_MAX_LENGTH

__all__ = ["LEADING", "POSITIONS", "TRAILING", "conf"]

#: Where the comment goes relative to the statement.
LEADING = "leading"
TRAILING = "trailing"
POSITIONS = (LEADING, TRAILING)


class _Conf:
    """Typed, defaulted view over the ``SQLCOMMENT_*`` settings.

    Each value is computed once and kept in the instance dictionary
    until any setting changes.
    """

    @cached_property
    def ENABLED(self) -> bool:
        """Master switch, honored per query.

        Turning it off at runtime leaves the wrapper installed but
        inert, so ``override_settings(SQLCOMMENT_ENABLED=False)`` works
        inside a test that asserts on generated SQL text. Turning it
        off *before startup* is different: :func:`install` returns
        early, so nothing is installed at all and flipping it back on
        later does nothing until the process restarts.
        """
        return bool(getattr(settings, "SQLCOMMENT_ENABLED", True))

    @cached_property
    def CALLSITE(self) -> bool:
        """Whether to resolve the calling source line per query."""
        return bool(getattr(settings, "SQLCOMMENT_CALLSITE", True))

    @cached_property
    def MAX_LENGTH(self) -> int:
        """Comment budget in bytes."""
        return int(
            getattr(settings, "SQLCOMMENT_MAX_LENGTH", DEFAULT_MAX_LENGTH)
        )

    @cached_property
    def MAX_DEPTH(self) -> int:
        """How many frames the callsite walk may climb before giving up.

        A larger number is nearly free. The limit is only ever reached
        on a query whose answer would be ``unknown`` anyway: a walk
        that finds project code stops there, so the common 6-8-frame
        path costs the same whatever the ceiling is. Raise it when
        querysets evaluated deep inside nested templates report no
        callsite.
        """
        from .callsite import DEFAULT_MAX_DEPTH

        return int(
            getattr(settings, "SQLCOMMENT_MAX_DEPTH", DEFAULT_MAX_DEPTH)
        )

    @cached_property
    def TAGS(self) -> Mapping[str, str]:
        """Tags added to every query, regardless of scope."""
        tags: Mapping[str, str] = getattr(settings, "SQLCOMMENT_TAGS", {})
        return tags

    @cached_property
    def POSITION(self) -> str:
        """``"leading"`` (default) or ``"trailing"``.

        Leading survives PostgreSQL's ``track_activity_query_size``
        truncation and MySQL's 1024-byte log truncation; trailing is
        what the sqlcommenter spec prescribes.
        """
        return str(getattr(settings, "SQLCOMMENT_POSITION", LEADING))

    @cached_property
    def SEPARATOR(self) -> str:
        r"""What goes between the comment and the statement.

        A single space by default, which is what every sqlcommenter
        implementation emits. Set it to ``"\n"`` to put the statement on
        its own line: in ``pg_stat_activity``, in slow-query logs and in
        anything that echoes SQL back at a human, a comment and a
        statement on separate lines are far easier to read than one long
        line. It costs the same one byte.

        Not counted against ``SQLCOMMENT_MAX_LENGTH``, which budgets the
        comment alone.
        """
        return str(getattr(settings, "SQLCOMMENT_SEPARATOR", " "))

    @cached_property
    def PROJECT_ROOT(self) -> str:
        """Root that ``callsite`` paths are reported relative to.

        Falls back to ``BASE_DIR``, then to ``""``. ``BASE_DIR`` is a
        ``startproject`` template convention rather than a setting
        Django requires, so a project assembled by hand may not have
        one — and reading a missing setting on the query path would
        raise ``AttributeError`` out of the execute wrapper and fail
        every query. ``""`` means "report no callsite", which is the
        right degradation: telemetry goes quiet, queries keep running.
        """
        root = getattr(settings, "SQLCOMMENT_PROJECT_ROOT", None)
        if root is None:
            root = getattr(settings, "BASE_DIR", None)
        return "" if root is None else str(root)


conf = _Conf()


@receiver(setting_changed)
def _clear_snapshot(sender: object, **kwargs: Any) -> None:
    """Drop the snapshot whenever any setting changes.

    Deliberately unfiltered, unlike the receiver in ``callsite.py``.
    :attr:`_Conf.PROJECT_ROOT` falls back to ``BASE_DIR``, which is not
    a ``SQLCOMMENT_*`` name, so filtering on the prefix would strand a
    stale root — and ``callsite``'s cache is computed against that
    root, so the staleness would spread. Clearing a dictionary of at
    most seven entries is not worth being clever about.
    """
    conf.__dict__.clear()
