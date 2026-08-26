"""Serialize sqlcommenter key-value tags into a SQL comment.

Implements the sqlcommenter serialization algorithm
(https://google.github.io/sqlcommenter/spec/) with two deliberate
deviations, both documented in the README:

* the comment defaults to being *prepended* to the statement rather than
  appended, so it survives ``track_activity_query_size`` truncation in
  PostgreSQL's ``pg_stat_activity`` (and the equivalent 1024-byte log
  truncation in MySQL). Switch with ``SQLCOMMENT_POSITION``;
* the URL-encoding safe set is widened from ``/`` to ``/:._-{}<>`` so
  paths and route patterns stay readable. ``<>`` in particular is what
  Django's own ``path()`` syntax uses, so a ``route`` tag reads as
  ``cart/<int:pk>/`` rather than ``cart/%3Cint:pk%3E/``. Standard
  parsers are unaffected — ``unquote`` is a no-op on those literals.

The safe set deliberately excludes ``*`` and ``'``. That makes it
impossible for a serialized tag to contain ``*/`` (which would close the
comment early) or a quote (which would break the ``key='value'``
grammar), so no separate sanitizing pass is needed.

This module imports nothing from Django.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from ._report import report_once

__all__ = [
    "CUSTOM_TAG_RANK",
    "DEFAULT_MAX_LENGTH",
    "SAFE_CHARS",
    "TAG_RANK",
    "build_comment",
    "serialize_tags",
]

#: Characters left un-encoded. Excludes ``*`` and ``'`` by design.
SAFE_CHARS = "/:._-{}<>"

#: Default comment budget in bytes, well under PostgreSQL's 1024-byte
#: track_activity_query_size default so the statement itself still
#: reaches pg_stat_activity behind the comment.
DEFAULT_MAX_LENGTH = 512

#: Keep-priority per tag key. When the serialized comment exceeds the
#: budget, the lowest-ranked tag is dropped first. Caller-supplied keys
#: rank below every key the library sets itself.
TAG_RANK: dict[str, int] = {
    "route": 100,
    "task": 100,
    "command": 100,
    "job": 100,
    "action": 90,
    "queue": 85,
    "controller": 80,
    "callsite": 70,
    "traceparent": 60,
}
CUSTOM_TAG_RANK = 10


def _encode(text: object) -> str:
    """URL-encode ``text``, or return ``""`` if it cannot be rendered.

    ``str(text)`` rather than a declared ``str`` parameter: the type
    hints promise string values but nothing enforces them at runtime,
    and ``sql_scope(order=order.pk)`` with an integer is the mistake
    everyone makes once. Left alone it raises ``TypeError`` out of the
    execute wrapper and fails every query in the scope.

    ``str`` runs arbitrary code, so it is guarded. Pass the model
    rather than its pk — ``sql_scope(order=order)`` — and ``__str__``
    can raise ``RelatedObjectDoesNotExist`` on an unsaved foreign key,
    or ``SynchronousOnlyOperation`` when a lazy string reaches the ORM
    under ASGI.

    ``errors="replace"`` rather than the default ``"strict"``: a lone
    surrogate is what a filesystem path that is not valid UTF-8 decodes
    to, so a ``callsite`` tag can carry one, and ``quote`` would then
    raise ``UnicodeEncodeError``.

    Between them there is no input that makes this function raise, so a
    tag that cannot be encoded degrades the comment and never the
    statement. The result is ASCII either way, so the budget stays an
    exact byte count.
    """
    try:
        rendered = str(text)
    except Exception:
        report_once("Failed to render a tag; it was dropped from the comment.")
        return ""
    return quote(rendered, safe=SAFE_CHARS, errors="replace")


def serialize_tags(tags: Mapping[str, str]) -> str:
    """Return ``key='value'`` pairs, sorted lexicographically, comma-joined.

    A pair is dropped when either half renders to nothing — an empty
    string, ``None``, or an object whose ``__str__`` raised. It would
    carry no information and only eat budget.

    Note what is *not* dropped: ``0`` and ``False`` render to ``"0"``
    and ``"False"``, and a caller who passed shard ``0`` meant shard
    ``0``. Filtering on truthiness would silently lose them.
    """
    pairs: list[str] = []
    for key, value in tags.items():
        rendered = "" if value is None else _encode(value)
        if not rendered:
            continue
        encoded_key = _encode(key)
        if encoded_key:
            pairs.append(f"{encoded_key}='{rendered}'")
    return ",".join(sorted(pairs))


def build_comment(
    tags: Mapping[str, str],
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """Return ``/*...*/`` for ``tags``, dropping low-rank tags to fit.

    Returns an empty string when nothing serializable is left. The
    caller must then leave the statement untouched rather than emit an
    empty comment — the spec forbids mutating a statement for nothing.
    """
    remaining = dict(tags)
    while remaining:
        body = serialize_tags(remaining)
        if not body:
            return ""
        if len(body) + 4 <= max_length:
            return f"/*{body}*/"
        # Deterministic victim: lowest rank, then lowest key name.
        # str(key) as the tie-break: keys are typed as str but nothing
        # enforces it, and comparing an int against a str at the same
        # rank would raise TypeError out of the execute wrapper.
        victim = min(
            remaining,
            key=lambda key: (TAG_RANK.get(key, CUSTOM_TAG_RANK), str(key)),
        )
        del remaining[victim]
    return ""
