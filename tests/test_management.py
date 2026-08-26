from __future__ import annotations

from typing import Any

import pytest

from django.core.management import call_command
from django.core.management.base import BaseCommand

from django_sqlcommenter.management import patch_management_commands
from django_sqlcommenter.scope import current_scope


def _run(command: BaseCommand) -> Any:
    """Invoke a command the way ``call_command`` does.

    ``BaseCommand.execute`` reads ``options["skip_checks"]`` straight out
    of the dict, so a caller that bypasses ``call_command`` — which fills
    it in — has to supply it itself or get a ``KeyError``.
    """
    return command.execute(
        verbosity=0, no_color=True, force_color=False, skip_checks=True
    )


@pytest.fixture(autouse=True)
def restore_base_command():
    original = BaseCommand.execute
    yield
    BaseCommand.execute = original


def test_scope_is_open_inside_handle() -> None:
    seen: list[dict[str, str] | None] = []

    class Probe(BaseCommand):
        # Override handle, never execute: overriding execute would
        # bypass the very patch under test.
        def handle(self, *args, **options):
            scope = current_scope()
            seen.append(dict(scope.tags) if scope else None)

    patch_management_commands()
    _run(Probe())
    assert seen[0] is not None
    assert "command" in seen[0]


def test_command_module_name_becomes_the_tag() -> None:
    seen: list[str] = []

    class Probe(BaseCommand):
        def handle(self, *args, **options):
            scope = current_scope()
            assert scope is not None
            seen.append(scope.tags["command"])

    patch_management_commands()
    _run(Probe())
    # Probe is defined in this test module.
    assert seen == ["test_management"]


def test_scope_is_closed_after_the_command() -> None:
    class Probe(BaseCommand):
        def handle(self, *args, **options):
            return None

    patch_management_commands()
    _run(Probe())
    assert current_scope() is None


def test_scope_is_closed_after_a_failing_command() -> None:
    class Probe(BaseCommand):
        def handle(self, *args, **options):
            raise RuntimeError("boom")

    patch_management_commands()
    with pytest.raises(RuntimeError):
        _run(Probe())
    assert current_scope() is None


def test_patching_twice_does_not_double_wrap() -> None:
    patch_management_commands()
    once = BaseCommand.execute
    patch_management_commands()
    assert BaseCommand.execute is once


def test_a_dotted_command_module_is_reduced_to_its_basename() -> None:
    # A real command lives in myapp/management/commands/foo.py and has
    # to be tagged `foo`, not the whole dotted path. Every other probe
    # here lives in a module with no dot in its name, so none of them
    # can tell rsplit(".", 1)[-1] from [0].
    seen: list[str] = []

    def handle(self, *args, **options):
        scope = current_scope()
        assert scope is not None
        seen.append(scope.tags["command"])

    probe = type(
        "Command",
        (BaseCommand,),
        {"__module__": "myapp.management.commands.foo", "handle": handle},
    )
    patch_management_commands()
    _run(probe())
    assert seen == ["foo"]


def test_the_return_value_is_passed_through() -> None:
    # BaseCommand.execute returns its output and call_command hands it
    # back — that is how `call_command("sqlmigrate", ...)` returns SQL.
    class Probe(BaseCommand):
        def handle(self, *args, **options):
            return "out"

    patch_management_commands()
    assert _run(Probe()) == "out"


def test_call_command_is_annotated_too() -> None:
    # Every other test invokes execute() directly. This one goes
    # through the path applications actually use.
    seen: list[str] = []

    class Probe(BaseCommand):
        def handle(self, *args, **options):
            scope = current_scope()
            assert scope is not None
            seen.append(scope.tags["command"])

    patch_management_commands()
    call_command(Probe(), verbosity=0)
    assert seen == ["test_management"]
