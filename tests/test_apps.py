"""How the package wires itself into a process."""

from __future__ import annotations

import pytest

from django.core.checks.registry import registry
from django.core.management.base import BaseCommand
from django.test import override_settings

from django_sqlcommenter.checks import check_settings


def _unpatched(self, *args, **options):
    """Stand-in for a pristine ``BaseCommand.execute``."""
    return None


def test_ready_patched_the_management_commands() -> None:
    """INSTALLED_APPS membership is enough — no manual wiring.

    Asserting on ``connection.execute_wrappers`` would prove nothing:
    ``tests/test_wrapper.py`` installs the annotator by hand, and
    merely importing that module registers the ``connection_created``
    receiver, so both are true whatever ``ready()`` does. Patching
    ``BaseCommand.execute`` is the one effect only ``install()``
    produces and nothing else leaves behind.
    """
    assert getattr(BaseCommand.execute, "_sqlcommenter_patched", False)


def test_ready_registered_the_system_check() -> None:
    assert check_settings in registry.registered_checks


def test_public_api_is_importable_from_the_package_root() -> None:
    import django_sqlcommenter as pkg

    for name in (
        "SqlCommentMiddleware",
        "SqlScope",
        "build_comment",
        "current_scope",
        "install",
        "serialize_tags",
        "sql_scope",
        "sql_tags",
    ):
        assert hasattr(pkg, name), name


def test_dir_lists_the_public_api() -> None:
    # __getattr__ resolves lazily, so without __dir__ autocomplete and
    # help() would show nothing at all.
    import django_sqlcommenter as pkg

    assert "sql_scope" in dir(pkg)
    assert "SqlCommentMiddleware" in dir(pkg)


def test_an_unknown_attribute_raises_the_conventional_error() -> None:
    import django_sqlcommenter as pkg

    with pytest.raises(AttributeError, match="has no attribute 'nope'"):
        _ = pkg.nope


def test_install_is_idempotent() -> None:
    from django_sqlcommenter import install

    before = BaseCommand.execute
    install()
    assert BaseCommand.execute is before


def test_install_does_nothing_when_disabled_at_startup() -> None:
    # Different from turning it off at runtime: nothing is installed at
    # all, and flipping the setting back on will not help.
    from django_sqlcommenter import install
    from django_sqlcommenter.management import patch_management_commands

    original = BaseCommand.execute
    BaseCommand.execute = _unpatched  # type: ignore[method-assign]
    try:
        with override_settings(SQLCOMMENT_ENABLED=False):
            install()
        assert not getattr(BaseCommand.execute, "_sqlcommenter_patched", False)
        patch_management_commands()
        assert getattr(BaseCommand.execute, "_sqlcommenter_patched", False)
    finally:
        BaseCommand.execute = original  # type: ignore[method-assign]
