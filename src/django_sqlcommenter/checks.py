"""System checks for the ``SQLCOMMENT_*`` settings.

These settings are read inside the execute wrapper, on the path of
every query, and by then it is too late to complain. A bad value does
not fail at startup: at best it silently disables annotation for the
whole process, and at worst it reaches the database as broken SQL.
Checking them here turns that into one line from ``manage.py check``.

``None`` is not "unset" for any of these except
``SQLCOMMENT_PROJECT_ROOT``. ``conf`` reads settings with
``getattr(settings, name, default)``, so an explicit ``None`` beats the
default and travels all the way to the query — ``SQLCOMMENT_SEPARATOR
= None`` renders as the literal ``"None"`` and every statement comes
back a syntax error.

``SQLCOMMENT_ENABLED`` and ``SQLCOMMENT_CALLSITE`` are deliberately
unchecked: ``bool()`` cannot raise on anything.

Nothing in here may raise. Several of these values are user objects
whose ``__str__`` or ``__fspath__`` runs arbitrary code, and a check
that crashes turns ``manage.py check`` into a traceback instead of a
message someone can act on.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error
from django.core.checks import Warning as CheckWarning

from .conf import POSITIONS, conf

__all__ = ["check_settings"]

#: Distinguishes "the setting is absent" from "the setting is None".
_UNSET = object()

_HOT_PATH = (
    "It is read inside the execute wrapper, so a bad value costs every "
    "query in the process instead of failing once at startup."
)

#: Settings that must be a positive int, with their check ids.
_POSITIVE_INTS: tuple[tuple[str, str], ...] = (
    ("SQLCOMMENT_MAX_LENGTH", "E001"),
    ("SQLCOMMENT_MAX_DEPTH", "E008"),
)

#: Setting name, accepted type, how to name it, and the check id.
_TYPED: tuple[tuple[str, Any, str, str], ...] = (
    ("SQLCOMMENT_TAGS", Mapping, "a mapping", "E002"),
    ("SQLCOMMENT_SEPARATOR", str, "a string", "E003"),
)


def _error(message: str, hint: str, code: str) -> Error:
    """Build one ``Error`` with the package's id prefix."""
    return Error(message, hint=hint, id=f"django_sqlcommenter.{code}")


def _is_a_usable_root(root: str | os.PathLike[str]) -> bool:
    """Whether ``root`` can name a project directory.

    ``"/"`` is absolute and still useless: it makes the whole
    filesystem project code, so ``callsite`` starts naming files in the
    standard library. POSIX keeps a leading double slash, so ``"//"``
    has to be rejected by name — it normalizes to itself rather than to
    ``os.sep``.
    """
    normalized = os.path.normpath(root)
    return os.path.isabs(normalized) and normalized not in (
        os.sep,
        os.sep * 2,
    )


def _readable_project_root() -> str | None:
    """``conf.PROJECT_ROOT``, or ``None`` when it cannot be read.

    Reading it runs ``str()`` on the setting — and on ``BASE_DIR`` when
    the setting is absent — which is user code either way. A system
    check must never be the thing that crashes: a traceback instead of
    a message is strictly worse than the misconfiguration it was
    looking for.
    """
    try:
        return conf.PROJECT_ROOT
    except Exception:
        return None


def check_settings(**kwargs: Any) -> list[CheckMessage]:
    """Validate the ``SQLCOMMENT_*`` settings. Registered in ``ready()``."""
    messages: list[CheckMessage] = []

    for name, code in _POSITIVE_INTS:
        value = getattr(settings, name, _UNSET)
        if value is not _UNSET and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            # A bool is an int, and True is 1: a one-byte budget drops
            # every tag, and a one-frame walk finds nothing. Either way
            # nothing is ever annotated, without a word.
            messages.append(
                _error(
                    f"{name} must be a positive int, not {value!r}.",
                    _HOT_PATH,
                    code,
                )
            )

    for name, expected, described, code in _TYPED:
        value = getattr(settings, name, _UNSET)
        if value is not _UNSET and not isinstance(value, expected):
            messages.append(
                _error(
                    f"{name} must be {described}, not {type(value).__name__}.",
                    _HOT_PATH,
                    code,
                )
            )

    separator = getattr(settings, "SQLCOMMENT_SEPARATOR", "")
    if isinstance(separator, str):
        for bad, why in (
            (
                "*/",
                "It would close the comment a second time and make "
                "every statement a syntax error.",
            ),
            (
                "--",
                "It opens a line comment, so with the default leading "
                "position it would comment out the whole statement.",
            ),
        ):
            if bad in separator:
                messages.append(
                    _error(
                        f"SQLCOMMENT_SEPARATOR must not contain {bad!r}.",
                        why,
                        "E007",
                    )
                )

    position = getattr(settings, "SQLCOMMENT_POSITION", _UNSET)
    if position is not _UNSET and position not in POSITIONS:
        messages.append(
            _error(
                f"SQLCOMMENT_POSITION must be one of {POSITIONS}, not "
                f"{position!r}.",
                "Anything that is not 'trailing' is treated as "
                "'leading', so a typo here changes nothing visible.",
                "E005",
            )
        )

    # None is genuinely "unset" here, and only here: conf falls back to
    # BASE_DIR, and from there to no callsite at all.
    root = getattr(settings, "SQLCOMMENT_PROJECT_ROOT", None)
    readable = root is None or isinstance(root, (str, os.PathLike))
    usable = True
    if readable and root is not None:
        try:
            usable = _is_a_usable_root(root)
        except Exception:
            # os.path.normpath calls __fspath__, which is user code.
            readable = False

    if not readable:
        messages.append(
            _error(
                f"SQLCOMMENT_PROJECT_ROOT must be a string or a path "
                f"that can be read, not {type(root).__name__}.",
                _HOT_PATH,
                "E004",
            )
        )
    elif not usable:
        messages.append(
            _error(
                f"SQLCOMMENT_PROJECT_ROOT must be an absolute path "
                f"below the filesystem root, not {root!r}.",
                "A relative root matches no source file and '/' makes "
                "the whole filesystem project code, so the callsite "
                "tag ends up either missing or meaningless.",
                "E006",
            )
        )

    if readable and conf.CALLSITE:
        configured = _readable_project_root()
        if configured is None:
            messages.append(
                _error(
                    "Neither SQLCOMMENT_PROJECT_ROOT nor BASE_DIR can "
                    "be read as a string.",
                    _HOT_PATH,
                    "E009",
                )
            )
        elif not configured:
            messages.append(
                CheckWarning(
                    "There is no project root, so no callsite tag will "
                    "ever be emitted.",
                    hint="Set SQLCOMMENT_PROJECT_ROOT or BASE_DIR, or "
                    "turn SQLCOMMENT_CALLSITE off to say so on purpose.",
                    id="django_sqlcommenter.W001",
                )
            )

    return messages
