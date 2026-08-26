from __future__ import annotations

import asyncio

import pytest

from django_sqlcommenter.scope import (
    SqlScope,
    current_scope,
    reset_scope,
    set_scope,
    sql_scope,
    sql_tags,
)


def test_no_scope_by_default() -> None:
    assert current_scope() is None


def test_scope_is_visible_inside_the_block() -> None:
    with sql_scope(route="/x"):
        scope = current_scope()
        assert scope is not None
        assert scope.tags == {"route": "/x"}


def test_scope_is_cleared_after_the_block() -> None:
    with sql_scope(route="/x"):
        pass
    assert current_scope() is None


def test_scope_is_cleared_after_an_exception() -> None:
    with pytest.raises(RuntimeError):
        with sql_scope(route="/x"):
            raise RuntimeError("boom")
    assert current_scope() is None


def test_nested_scope_replaces_rather_than_merges() -> None:
    with sql_scope(route="/x"):
        with sql_scope(task="t"):
            scope = current_scope()
            assert scope is not None
            assert scope.tags == {"task": "t"}


def test_callsite_defaults_to_enabled() -> None:
    with sql_scope(route="/x"):
        scope = current_scope()
        assert scope is not None
        assert scope.callsite is True


def test_callsite_can_be_disabled_per_scope() -> None:
    with sql_scope(task="bulk", with_callsite=False):
        scope = current_scope()
        assert scope is not None
        assert scope.callsite is False


def test_a_tag_may_be_named_callsite() -> None:
    # The flag is spelled with_callsite precisely so this works.
    with sql_scope(callsite="mine.py:1"):
        scope = current_scope()
        assert scope is not None
        assert scope.tags == {"callsite": "mine.py:1"}
        assert scope.callsite is True


def test_tags_merge_into_the_current_scope() -> None:
    with sql_scope(route="/x"):
        with sql_tags(cycle="7"):
            scope = current_scope()
            assert scope is not None
            assert scope.tags == {"route": "/x", "cycle": "7"}


def test_tags_override_a_colliding_key() -> None:
    with sql_scope(route="/x"):
        with sql_tags(route="/y"):
            scope = current_scope()
            assert scope is not None
            assert scope.tags["route"] == "/y"


def test_tags_are_removed_after_the_block() -> None:
    with sql_scope(route="/x"):
        with sql_tags(cycle="7"):
            pass
        scope = current_scope()
        assert scope is not None
        assert scope.tags == {"route": "/x"}


def test_tags_preserve_the_callsite_flag() -> None:
    with sql_scope(task="bulk", with_callsite=False):
        with sql_tags(cycle="7"):
            scope = current_scope()
            assert scope is not None
            assert scope.callsite is False


def test_tags_outside_any_scope_are_a_noop() -> None:
    with sql_tags(cycle="7"):
        assert current_scope() is None


def test_set_and_reset_round_trip() -> None:
    token = set_scope(SqlScope(tags={"job": "j"}))
    try:
        scope = current_scope()
        assert scope is not None
        assert scope.tags == {"job": "j"}
    finally:
        reset_scope(token)
    assert current_scope() is None


async def test_concurrent_scopes_do_not_see_each_other() -> None:
    # Task 6 opens a scope per request under ASGI, so two requests in
    # flight on one event loop must not share tags.
    seen: dict[str, str] = {}

    async def unit(name: str) -> None:
        with sql_scope(task=name):
            await asyncio.sleep(0)
            scope = current_scope()
            assert scope is not None
            seen[name] = scope.tags["task"]

    await asyncio.gather(unit("a"), unit("b"))
    assert seen == {"a": "a", "b": "b"}
    assert current_scope() is None


def test_set_scope_none_suppresses_an_enclosing_scope() -> None:
    with sql_scope(route="/x"):
        token = set_scope(None)
        try:
            assert current_scope() is None
        finally:
            reset_scope(token)
        scope = current_scope()
        assert scope is not None
        assert scope.tags == {"route": "/x"}
