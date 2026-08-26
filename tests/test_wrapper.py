from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import pytest

from django.db import connection
from django.db.backends.signals import connection_created
from django.test import override_settings

from django_sqlcommenter import _report as report_module
from django_sqlcommenter.scope import (
    SqlScope,
    reset_scope,
    set_scope,
    sql_scope,
)
from django_sqlcommenter.wrapper import QueryAnnotator, install_on_connection


class Recorder:
    """Stands in for the next executor in the wrapper chain."""

    def __init__(self) -> None:
        self.sql: str | None = None
        self.params: Any = None

    def __call__(self, sql: str, params: Any, many: bool, ctx: Any) -> str:
        self.sql = sql
        self.params = params
        return "executed"


class ExplodingTags(Mapping):
    """A mapping whose ``keys()`` raises a ``BaseException``.

    It has to be a real ``Mapping`` rather than a ``dict`` subclass:
    ``{**d}`` takes a C fast path for dict subclasses and never calls
    an overridden ``keys()``.
    """

    def keys(self):
        raise asyncio.CancelledError

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


@pytest.fixture
def run():
    annotator = QueryAnnotator()
    recorder = Recorder()

    def _run(sql: str, params: Any = None) -> str | None:
        annotator(recorder, sql, params, False, {})
        return recorder.sql

    _run.recorder = recorder  # type: ignore[attr-defined]
    _run.annotator = annotator  # type: ignore[attr-defined]
    return _run


ANNOTATE = override_settings(SQLCOMMENT_CALLSITE=False, SQLCOMMENT_TAGS={})


@ANNOTATE
def test_comment_is_prepended_by_default(run) -> None:
    with sql_scope(route="/x"):
        assert run("SELECT 1") == "/*route='/x'*/ SELECT 1"


@ANNOTATE
@override_settings(SQLCOMMENT_POSITION="trailing")
def test_comment_can_be_appended(run) -> None:
    with sql_scope(route="/x"):
        assert run("SELECT 1") == "SELECT 1 /*route='/x'*/"


@ANNOTATE
@override_settings(SQLCOMMENT_SEPARATOR="\n")
def test_separator_can_be_a_newline(run) -> None:
    with sql_scope(route="/x"):
        assert run("SELECT 1") == "/*route='/x'*/\nSELECT 1"


@ANNOTATE
@override_settings(SQLCOMMENT_POSITION="trailing", SQLCOMMENT_SEPARATOR="\n")
def test_separator_applies_to_a_trailing_comment(run) -> None:
    with sql_scope(route="/x"):
        assert run("SELECT 1") == "SELECT 1\n/*route='/x'*/"


@ANNOTATE
@override_settings(SQLCOMMENT_MAX_LENGTH=14, SQLCOMMENT_SEPARATOR="\n")
def test_separator_is_not_charged_to_the_budget(run) -> None:
    # The comment is exactly 14 characters, so it fits the budget to the
    # byte. If the separator counted too, the tag would be dropped and
    # the statement would come back bare.
    with sql_scope(route="/x"):
        assert run("SELECT 1") == "/*route='/x'*/\nSELECT 1"


@ANNOTATE
def test_return_value_is_passed_through(run) -> None:
    with sql_scope(route="/x"):
        outcome = run.annotator(run.recorder, "SELECT 1", None, False, {})
    assert outcome == "executed"


@ANNOTATE
def test_no_scope_leaves_the_statement_untouched(run) -> None:
    assert run("SELECT 1") == "SELECT 1"


@ANNOTATE
@override_settings(SQLCOMMENT_ENABLED=False)
def test_disabling_at_runtime_leaves_the_statement_untouched(run) -> None:
    # The switch is honored per query, not only at install time, so a
    # consumer's suite can turn it off with override_settings.
    with sql_scope(route="/x"):
        assert run("SELECT 1") == "SELECT 1"


@ANNOTATE
@pytest.mark.parametrize(
    "sql",
    [
        "SAVEPOINT s1",
        "RELEASE SAVEPOINT s1",
        "ROLLBACK TO SAVEPOINT s1",
        "BEGIN",
        "BEGIN IMMEDIATE",
        "COMMIT",
        "SET TIME ZONE 'UTC'",
        "SHOW timezone",
        # Lower case is what makes the .upper() call load-bearing.
        # Django's own backends shout, so without these the call can
        # be deleted and the whole suite still passes.
        "savepoint s1",
        "begin",
        "commit",
    ],
)
def test_bookkeeping_statements_are_not_annotated(run, sql) -> None:
    with sql_scope(route="/x"):
        assert run(sql) == sql


@ANNOTATE
@pytest.mark.parametrize("sql", ["SETVAL('seq', 1)", "SHOWROOM x"])
def test_a_statement_merely_starting_with_a_prefix_is_annotated(
    run, sql
) -> None:
    # The trailing space in "SET " and "SHOW " is the only thing that
    # stops those prefixes from swallowing an unrelated first word.
    with sql_scope(route="/x"):
        assert run(sql) == f"/*route='/x'*/ {sql}"


@ANNOTATE
def test_a_broken_scope_loses_the_comment_not_the_query(
    run, caplog, reported
) -> None:
    # set_scope is the public extension point, so an adapter author can
    # hand it something that is not a mapping. Losing the comment is
    # acceptable; losing every query in the scope is not.
    token = set_scope(SqlScope(tags=["not", "a", "mapping"]))
    try:
        with caplog.at_level(logging.ERROR):
            assert run("SELECT 1") == "SELECT 1"
    finally:
        reset_scope(token)
    assert "Failed to annotate" in caplog.text


@ANNOTATE
def test_percent_is_doubled_when_params_are_present(run) -> None:
    with sql_scope(route="/a b"):
        result = run("SELECT %s", ["x"])
    assert result is not None
    assert "%%20" in result


@ANNOTATE
def test_percent_is_not_doubled_without_params(run) -> None:
    with sql_scope(route="/a b"):
        result = run("SELECT 1", None)
    assert result is not None
    assert "%20" in result
    assert "%%20" not in result


@ANNOTATE
def test_params_are_passed_through_untouched(run) -> None:
    with sql_scope(route="/x"):
        run("SELECT %s", ["x"])
    assert run.recorder.params == ["x"]


@override_settings(SQLCOMMENT_CALLSITE=False, SQLCOMMENT_TAGS={"env": "prod"})
def test_static_tags_are_merged_in(run) -> None:
    with sql_scope(route="/x"):
        result = run("SELECT 1")
    assert result is not None
    assert "env='prod'" in result


@override_settings(SQLCOMMENT_CALLSITE=False, SQLCOMMENT_TAGS={"route": "s"})
def test_scope_tags_win_over_static_tags(run) -> None:
    with sql_scope(route="/x"):
        result = run("SELECT 1")
    assert result is not None
    assert "route='/x'" in result
    assert "route='s'" not in result


@override_settings(SQLCOMMENT_CALLSITE=True, SQLCOMMENT_TAGS={})
def test_callsite_is_added_when_enabled(run) -> None:
    with sql_scope(route="/x"):
        result = run("SELECT 1")
    assert result is not None
    assert "callsite='tests/test_wrapper.py:" in result


@override_settings(SQLCOMMENT_CALLSITE=True, SQLCOMMENT_TAGS={})
def test_scope_can_opt_out_of_callsite(run) -> None:
    with sql_scope(route="/x", with_callsite=False):
        result = run("SELECT 1")
    assert result is not None
    assert "callsite=" not in result


@override_settings(SQLCOMMENT_CALLSITE=True, SQLCOMMENT_TAGS={})
def test_an_explicit_callsite_tag_is_not_overwritten(run) -> None:
    with sql_scope(route="/x", callsite="mine.py:1"):
        result = run("SELECT 1")
    assert result is not None
    assert "callsite='mine.py:1'" in result


@override_settings(
    SQLCOMMENT_CALLSITE=False, SQLCOMMENT_TAGS={}, SQLCOMMENT_MAX_LENGTH=8
)
def test_an_unfittable_comment_leaves_the_statement_untouched(run) -> None:
    with sql_scope(route="/some/long/route"):
        assert run("SELECT 1") == "SELECT 1"


@ANNOTATE
def test_the_annotation_failure_is_reported_once(
    run, caplog, reported
) -> None:
    token = set_scope(SqlScope(tags=["not", "a", "mapping"]))
    try:
        with caplog.at_level(logging.ERROR):
            run("SELECT 1")
            run("SELECT 2")
    finally:
        reset_scope(token)
    assert caplog.text.count("Failed to annotate") == 1


@ANNOTATE
def test_an_exception_from_execute_is_not_swallowed(run) -> None:
    # The guard covers building the comment, never running the
    # statement: a driver error has to reach the caller unchanged.
    def boom(sql: str, params: Any, many: bool, ctx: Any) -> str:
        raise RuntimeError("driver said no")

    with sql_scope(route="/x"), pytest.raises(RuntimeError, match="driver"):
        run.annotator(boom, "SELECT 1", None, False, {})


@ANNOTATE
def test_executemany_is_annotated_once_for_the_whole_batch(run) -> None:
    with sql_scope(route="/x"):
        run.annotator(run.recorder, "INSERT %s", [["a"], ["b"]], True, {})
    assert run.recorder.sql is not None
    assert run.recorder.sql.startswith("/*route='/x'*/ INSERT")


@ANNOTATE
def test_many_and_context_are_passed_through_untouched(run) -> None:
    seen: list[Any] = []

    def spy(sql: str, params: Any, many: bool, ctx: Any) -> str:
        seen.append((many, ctx))
        return "executed"

    marker = {"connection": object()}
    with sql_scope(route="/x"):
        run.annotator(spy, "SELECT 1", None, True, marker)
    assert seen == [(True, marker)]


@ANNOTATE
def test_execute_is_called_exactly_once_when_it_raises(run) -> None:
    # Pins the guard's boundary. If `execute` moved inside the try, the
    # except branch would call it a second time — a wasted read for a
    # SELECT, a duplicate row for an INSERT — and the exception would
    # still reach the caller, so nothing else would notice.
    calls: list[str] = []

    def boom(sql: str, params: Any, many: bool, ctx: Any) -> str:
        calls.append(sql)
        raise RuntimeError("driver said no")

    with sql_scope(route="/x"), pytest.raises(RuntimeError, match="driver"):
        run.annotator(boom, "SELECT 1", None, False, {})
    assert len(calls) == 1


@ANNOTATE
def test_a_base_exception_from_annotating_is_not_swallowed(run) -> None:
    # CancelledError is a BaseException in 3.8+, and this wrapper runs
    # under ASGI: swallowing one would let a cancelled request keep
    # issuing queries instead of unwinding.
    token = set_scope(SqlScope(tags=ExplodingTags()))
    try:
        with pytest.raises(asyncio.CancelledError):
            run.annotator(run.recorder, "SELECT 1", None, False, {})
    finally:
        reset_scope(token)


@ANNOTATE
def test_a_broken_logger_does_not_fail_the_query(run, reported) -> None:
    # Reporting is part of the guard. A custom logging.Filter is the
    # realistic way it raises: Logger.filter calls it unprotected.
    class Exploding(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            raise RuntimeError("logging blew up")

    exploding = Exploding()
    report_module.logger.addFilter(exploding)
    token = set_scope(SqlScope(tags=["not", "a", "mapping"]))
    try:
        assert run("SELECT 1") == "SELECT 1"
    finally:
        reset_scope(token)
        report_module.logger.removeFilter(exploding)


@pytest.fixture
def wrappers():
    """Restore ``execute_wrappers`` after a test that mutates it.

    ``AppConfig.ready()`` installs an annotator on the connection at
    startup, and under ``django_db`` that connection is never reopened —
    so a test that strips the list has to put it back, or every later
    test runs against a connection the app never wired up.
    """
    original = list(connection.execute_wrappers)
    yield connection.execute_wrappers
    connection.execute_wrappers[:] = original


def _uninstall() -> None:
    connection.execute_wrappers[:] = [
        w
        for w in connection.execute_wrappers
        if not isinstance(w, QueryAnnotator)
    ]


def test_annotator_is_appended(wrappers) -> None:
    _uninstall()
    install_on_connection(connection=connection)
    assert any(isinstance(w, QueryAnnotator) for w in wrappers)


def test_installing_twice_appends_once(wrappers) -> None:
    _uninstall()
    install_on_connection(connection=connection)
    install_on_connection(connection=connection)
    assert sum(isinstance(w, QueryAnnotator) for w in wrappers) == 1


@override_settings(SQLCOMMENT_ENABLED=False)
def test_disabled_installs_nothing(wrappers) -> None:
    _uninstall()
    install_on_connection(connection=connection)
    assert not any(isinstance(w, QueryAnnotator) for w in wrappers)


def test_installing_without_a_connection_is_a_noop() -> None:
    # The signal always supplies one, but this is a public function and
    # is documented as callable directly.
    install_on_connection()


@pytest.mark.django_db
@override_settings(SQLCOMMENT_CALLSITE=True, SQLCOMMENT_TAGS={})
def test_annotated_query_actually_executes(wrappers) -> None:
    """End to end: the annotated statement runs and returns its row.

    ``connection.queries`` cannot observe this *on SQLite*, which is
    what this suite runs on: the debug cursor calls
    ``last_executed_query()``, and SQLite's implementation returns the
    statement Django handed down rather than what the driver sent. On
    PostgreSQL, MySQL and Oracle it would show the comment. A second
    wrapper appended after the annotator sits *inside* it in the chain,
    which makes it the one place that sees what the driver sees on
    every backend.
    """
    seen: list[str] = []

    def recorder(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    _uninstall()
    install_on_connection(connection=connection)
    wrappers.append(recorder)
    with sql_scope(command="probe"), connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1
    annotated = seen[-1]
    assert annotated.startswith("/*")
    assert annotated.endswith("*/ SELECT 1")
    assert "command='probe'" in annotated
    assert "callsite='tests/test_wrapper.py:" in annotated


@ANNOTATE
def test_percent_is_doubled_for_an_empty_parameter_tuple(run) -> None:
    # Django passes () — not None — for a statement with no
    # placeholders, so this is the case the rule actually exists for.
    # `if params:` instead of `if params is not None:` passes every
    # other test in this file and breaks psycopg2 on any route with a
    # space in it.
    with sql_scope(route="/a b"):
        result = run("SELECT 1", ())
    assert result is not None
    assert "%%20" in result


def test_the_connection_created_receiver_is_registered() -> None:
    # Removing the @receiver decorator passes the whole suite: every
    # other test installs the annotator by calling the function.
    # A receiver entry is (key, receiver, sender, is_async); the
    # dispatch_uid is the first half of the key.
    uids = [entry[0][0] for entry in connection_created.receivers]
    assert "django_sqlcommenter" in uids
