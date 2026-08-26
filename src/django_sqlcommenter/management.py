"""Annotate queries made by management commands, with no per-command opt-in."""

from __future__ import annotations

import functools
from typing import Any

from django.core.management.base import BaseCommand

from .scope import sql_scope

__all__ = ["patch_management_commands"]


def patch_management_commands() -> None:
    """Wrap ``BaseCommand.execute`` so every command annotates its SQL.

    Patching the base class rather than each command covers Django's own
    commands (``migrate``, ``loaddata``), third-party commands, and
    ``call_command()`` — and needs no cooperation from any of them.

    The tag is the command's module name, which is the name you type on
    the command line.

    Idempotent: safe to call from ``AppConfig.ready()`` in a process
    that reloads.
    """
    if getattr(BaseCommand.execute, "_sqlcommenter_patched", False):
        return

    original = BaseCommand.execute

    @functools.wraps(original)
    def execute(self: BaseCommand, *args: Any, **options: Any) -> Any:
        name = type(self).__module__.rsplit(".", 1)[-1]
        with sql_scope(command=name):
            return original(self, *args, **options)

    execute._sqlcommenter_patched = True  # type: ignore[attr-defined]
    BaseCommand.execute = execute  # type: ignore[method-assign]
