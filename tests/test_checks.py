from __future__ import annotations

import os
from pathlib import Path

import pytest

from django.conf import settings
from django.core.checks import CheckMessage
from django.core.checks import Warning as CheckWarning
from django.core.signals import setting_changed
from django.test import override_settings

from django_sqlcommenter.checks import check_settings


def _ids(messages: list[CheckMessage]) -> list[str]:
    return [message.id or "" for message in messages]


def test_a_default_configuration_is_clean() -> None:
    assert check_settings() == []


@pytest.mark.parametrize("value", ["abc", True, 0, -5, None])
def test_max_length_must_be_a_positive_int(value) -> None:
    # A bool is an int and True budgets one byte, so every tag would be
    # dropped and nothing annotated — silently. None is not "unset":
    # conf reads it with a default, so an explicit None wins.
    with override_settings(SQLCOMMENT_MAX_LENGTH=value):
        assert _ids(check_settings()) == ["django_sqlcommenter.E001"]


@pytest.mark.parametrize("value", [["env", "prod"], None])
def test_tags_must_be_a_mapping(value) -> None:
    with override_settings(SQLCOMMENT_TAGS=value):
        assert _ids(check_settings()) == ["django_sqlcommenter.E002"]


@pytest.mark.parametrize("value", [10, None])
def test_separator_must_be_a_string(value) -> None:
    # None renders as the literal "None" and the database rejects
    # every statement; nothing downstream catches that.
    with override_settings(SQLCOMMENT_SEPARATOR=value):
        assert _ids(check_settings()) == ["django_sqlcommenter.E003"]


def test_separator_must_not_close_the_comment() -> None:
    with override_settings(SQLCOMMENT_SEPARATOR=" */ "):
        assert _ids(check_settings()) == ["django_sqlcommenter.E007"]


def test_position_must_be_one_of_two_values() -> None:
    # "traling" is silently treated as leading, so only a check can
    # ever tell anyone about the typo.
    with override_settings(SQLCOMMENT_POSITION="traling"):
        assert _ids(check_settings()) == ["django_sqlcommenter.E005"]


def test_project_root_must_be_a_string_or_a_path() -> None:
    with override_settings(SQLCOMMENT_PROJECT_ROOT=42):
        assert _ids(check_settings()) == ["django_sqlcommenter.E004"]


def test_project_root_may_be_a_path_object() -> None:
    with override_settings(SQLCOMMENT_PROJECT_ROOT=Path("/srv/app")):
        assert check_settings() == []


@pytest.mark.parametrize("value", ["app", "~/app", "/", "//"])
def test_project_root_must_be_an_absolute_directory(value) -> None:
    # A relative root matches no file; "/" matches every file and
    # starts tagging the standard library as project code.
    with override_settings(SQLCOMMENT_PROJECT_ROOT=value):
        assert _ids(check_settings()) == ["django_sqlcommenter.E006"]


def test_no_project_root_at_all_is_a_warning() -> None:
    with override_settings():
        del settings.BASE_DIR
        setting_changed.send(sender=None, setting="BASE_DIR", enter=True)
        messages = check_settings()
    assert _ids(messages) == ["django_sqlcommenter.W001"]
    assert isinstance(messages[0], CheckWarning)


def test_an_unreadable_root_suppresses_the_root_warning() -> None:
    # The warning reads conf.PROJECT_ROOT, which runs str() on the
    # setting — exactly what E004 rejected. One accurate error beats an
    # error plus a crash inside the check framework.
    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    with override_settings(SQLCOMMENT_PROJECT_ROOT=Exploding()):
        assert _ids(check_settings()) == ["django_sqlcommenter.E004"]


def test_unrelated_errors_do_not_hide_the_root_warning() -> None:
    # A project with a position typo and no BASE_DIR has two problems
    # and should be told about both at once, not one per run.
    with override_settings(SQLCOMMENT_POSITION="traling"):
        del settings.BASE_DIR
        setting_changed.send(sender=None, setting="BASE_DIR", enter=True)
        ids = _ids(check_settings())
    assert ids == ["django_sqlcommenter.E005", "django_sqlcommenter.W001"]


def test_a_root_that_cannot_be_read_is_an_error_not_a_crash() -> None:
    # os.path.normpath calls __fspath__, which is user code. A check
    # that raises is worse than the setting it was checking.
    class Exploding(os.PathLike):
        def __fspath__(self) -> str:
            raise RuntimeError("boom")

    with override_settings(SQLCOMMENT_PROJECT_ROOT=Exploding()):
        assert _ids(check_settings()) == ["django_sqlcommenter.E004"]


def test_an_unreadable_base_dir_is_an_error_not_a_crash() -> None:
    # conf.PROJECT_ROOT falls back to str(BASE_DIR), so BASE_DIR is
    # user code on this path too. Its own id, not E004: silencing an
    # unreadable SQLCOMMENT_PROJECT_ROOT must not also silence this.
    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    with override_settings(BASE_DIR=Exploding()):
        assert _ids(check_settings()) == ["django_sqlcommenter.E009"]


def test_a_bad_max_depth_is_reported() -> None:
    # Same shape as SQLCOMMENT_MAX_LENGTH, including the bool trap:
    # True is 1, and a one-frame walk finds nothing.
    for bad in (0, -1, True, "30", 2.5):
        with override_settings(SQLCOMMENT_MAX_DEPTH=bad):
            assert _ids(check_settings()) == ["django_sqlcommenter.E008"]
    with override_settings(SQLCOMMENT_MAX_DEPTH=200):
        assert _ids(check_settings()) == []


def test_a_separator_that_comments_out_the_statement_is_rejected() -> None:
    # "--" opens a line comment; with the default leading position the
    # whole statement ends up behind it.
    with override_settings(SQLCOMMENT_SEPARATOR=" -- "):
        assert _ids(check_settings()) == ["django_sqlcommenter.E007"]
