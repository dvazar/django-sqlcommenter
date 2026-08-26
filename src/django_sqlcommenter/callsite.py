"""Resolve the project source line that issued a query.

Walks raw frames via ``f_back`` rather than calling
``traceback.extract_stack()``: the latter materializes the whole stack
and reads source lines through ``linecache`` for every frame. Measured
on CPython 3.14 over twenty frames none of which are project code —
the worst case, since the walk cannot stop early — this costs 2.3 us
against 67.0 us for ``extract_stack``. The realistic path, 6-8 Django
frames between the execute wrapper and project code, measures 1.0 us;
the best case, where the caller is itself project code, 0.47 us; and
the memoized per-filename lookup inside the walk 0.04 us.

Enabling ``callsite`` costs a query more than the walk alone: it adds
a tag, and serializing that fourth tag is another 0.9 us. The two
together are the 1.4 us the README quotes per annotated query.

Raising ``SQLCOMMENT_MAX_DEPTH`` is nearly free, because the limit is
only ever reached on the queries that would answer ``unknown`` anyway:
a walk that finds project code stops there regardless of the ceiling.
Walking the full default of 100 frames and giving up costs 10.4 us,
and only a query with no project frame above it ever pays that.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import FrameType
from typing import Any

from django.core.signals import setting_changed
from django.dispatch import receiver

from .conf import conf

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "clear_cache",
    "relative_path",
    "resolve_callsite",
]

#: Give up after this many frames, unless ``SQLCOMMENT_MAX_DEPTH`` says
#: otherwise. A project frame is normally 6-8 frames up when the ORM is
#: driven directly, but a queryset evaluated inside a template is far
#: deeper: measured on Django 5.2, 28 frames for a template with no
#: nesting and 5 more for every level of ``{% include %}``. The old
#: limit of 30 therefore gave up on any page with two partials in it.
#: 100 covers roughly fourteen levels of nesting.
DEFAULT_MAX_DEPTH = 100

#: Path segments that are never project code even when they sit inside
#: the project root — a local virtualenv does. Compared segment by
#: segment rather than as a substring: a checkout under a directory
#: named ``site-packages-demo`` is still a checkout.
FOREIGN_MARKERS = frozenset({"site-packages", "dist-packages"})

#: Trailing separator included, so a sibling directory whose name
#: merely begins with ours — ``django_sqlcommenter_extras`` — is not
#: mistaken for this package.
_PACKAGE_DIR = f"{Path(__file__).resolve().parent}{os.sep}"

#: Absolute filename -> root-relative path, or None when not ours.
#: Bounded by the number of distinct source files in the process.
_cache: dict[str, str | None] = {}


def clear_cache() -> None:
    """Drop the filename cache. For tests, and after changing settings."""
    _cache.clear()


@receiver(setting_changed)
def _clear_cache_on_setting_change(
    sender: object, setting: str = "", **kwargs: Any
) -> None:
    """Drop the cache when the project root moves.

    :func:`relative_path` memoizes on the filename alone, but its
    answer depends on the root. Putting the root in the cache key would
    move a ``getattr`` and a ``str()`` onto the query path to handle
    something that happens a handful of times per process — more
    expensive than the lookup it protects. ``override_settings`` fires
    this on the way in and on the way out, so a consumer's tests get
    correct answers without knowing the cache exists.
    """
    if setting in ("SQLCOMMENT_PROJECT_ROOT", "BASE_DIR"):
        _cache.clear()


def relative_path(filename: str) -> str | None:
    """Return ``filename`` relative to the project root, or ``None``.

    ``None`` means the file is not project code: a dependency, the
    standard library, this package itself (whose frames sit between the
    query and its real caller), or anything at all when there is no
    project root to be relative to.
    """
    try:
        return _cache[filename]
    except KeyError:
        pass

    result: str | None = None
    root = conf.PROJECT_ROOT
    if (
        root
        and not filename.startswith(_PACKAGE_DIR)
        and FOREIGN_MARKERS.isdisjoint(Path(filename).parts)
    ):
        # normpath then join: the setting may or may not carry a
        # trailing separator, and a doubled one would make the prefix
        # match nothing — silently turning callsite off deployment-wide.
        prefix = os.path.join(os.path.normpath(root), "")
        if filename.startswith(prefix):
            result = filename[len(prefix) :]

    _cache[filename] = result
    return result


def resolve_callsite() -> str | None:
    """Return ``path/to/file.py:LINE`` for the nearest project frame."""
    frame: FrameType | None = sys._getframe(1)
    for _ in range(conf.MAX_DEPTH):
        if frame is None:
            return None
        relative = relative_path(frame.f_code.co_filename)
        if relative is not None:
            return f"{relative}:{frame.f_lineno}"
        frame = frame.f_back
    return None
