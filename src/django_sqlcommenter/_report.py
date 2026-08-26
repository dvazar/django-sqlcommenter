"""Report a telemetry failure once, then stay quiet.

Annotating is telemetry, so every path that builds a tag or a comment
is guarded. A guard that says nothing turns a bug into a mystery, and
one that speaks every time floods the log at request or query rate.
Once per distinct message per process is the compromise — distinct, so
that a failure in one place does not silence a different failure
somewhere else.
"""

from __future__ import annotations

import contextlib
import logging

__all__ = ["report_once"]

logger = logging.getLogger("django_sqlcommenter")

_reported: set[str] = set()


def report_once(message: str) -> None:
    """Log ``message`` with a traceback, at most once per process.

    Call only from inside an ``except`` block, so the traceback is
    attached. Reporting is part of the guard, so it must not raise
    either: a custom ``logging.Filter`` is the realistic way it can,
    since ``Logger.filter`` calls one unprotected — unlike a handler's
    ``emit``, which the stdlib already routes to ``handleError``.
    """
    if message in _reported:
        return
    _reported.add(message)
    with contextlib.suppress(Exception):
        logger.exception("%s This is reported once per process.", message)
