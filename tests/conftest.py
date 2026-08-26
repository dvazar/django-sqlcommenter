"""Fixtures shared by the whole suite."""

from __future__ import annotations

import pytest

from django.db import connection
from django.db.backends.signals import connection_created

from django_sqlcommenter import _report as report_module
from django_sqlcommenter.conf import conf
from django_sqlcommenter.scope import current_scope, set_scope
from django_sqlcommenter.wrapper import install_on_connection

#: dispatch_uid for the recorder below, so a failed test cannot leave a
#: receiver behind that annotates every later connection.
_RECORDER_UID = "sqlcommenter-test-recorder"


@pytest.fixture(autouse=True)
def _no_scope_leaks():
    """Fail the test that leaked a scope, rather than the next one.

    A scope lives in a ``ContextVar`` and pytest runs the suite in one
    context, so a test that leaves one set corrupts every test after
    it. Left alone that produces cross-file failures whose membership
    depends on collection order — a bug in the middleware surfacing
    first as a wrapper failure, and differently under ``-k`` or
    ``--lf``.

    It asserts rather than only cleaning up. Several tests catch real
    bugs precisely by checking ``current_scope() is None``, so a
    fixture that silently reset would restore isolation and delete
    that signal in the same stroke.
    """
    yield
    leaked = current_scope()
    set_scope(None)
    assert leaked is None, f"test leaked a scope: {leaked}"


@pytest.fixture(autouse=True)
def _fresh_settings_snapshot():
    """Drop the conf snapshot around every test.

    ``conf`` caches each setting until ``setting_changed`` fires, and
    neither a bare ``override_settings()`` nor a direct ``del`` on the
    settings object fires it. A test reaching for either would
    otherwise leave a stale value behind and make a later test pass or
    fail depending on collection order — which is exactly what happened
    once already.
    """
    conf.__dict__.clear()
    yield
    conf.__dict__.clear()


@pytest.fixture
def reported():
    """Reset the once-per-process report state, and put it back."""
    original = set(report_module._reported)
    report_module._reported.clear()
    yield
    report_module._reported.clear()
    report_module._reported.update(original)


class SqlLog(list):
    """Statements exactly as the driver received them.

    ``connection.queries`` cannot serve this purpose on SQLite, which
    is what the suite runs on: the debug cursor calls
    ``last_executed_query()``, and SQLite's implementation returns the
    statement Django handed down rather than the one the driver sent.
    A wrapper appended *after* the annotator sits inside it in the
    chain, which makes it the one place that sees the real thing on
    every backend.
    """

    def matching(self, fragment: str) -> list[str]:
        """Every statement containing ``fragment``."""
        return [sql for sql in self if fragment in sql]

    def one(self, fragment: str) -> str:
        """The single statement containing ``fragment``."""
        found = self.matching(fragment)
        assert len(found) == 1, (
            f"expected exactly one statement matching {fragment!r}, "
            f"found {len(found)}: {found}"
        )
        return found[0]


@pytest.fixture
def sql_log():
    """Record the SQL that reaches the driver, on every connection.

    Wired through ``connection_created`` rather than appended to the
    open connection alone: under ASGI a sync view runs in a worker
    thread, and ``connections`` is thread-critical, so the query lands
    on a connection that did not exist when the test started. Watching
    the signal is also what lets the test observe that the package's
    own receiver wires that new connection up.
    """
    statements = SqlLog()

    def recorder(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    def wire(sender=None, connection=None, **kwargs):
        if connection is None:
            return
        install_on_connection(connection=connection)
        if recorder not in connection.execute_wrappers:
            connection.execute_wrappers.append(recorder)

    original = list(connection.execute_wrappers)
    connection_created.connect(wire, dispatch_uid=_RECORDER_UID)
    wire(connection=connection)
    try:
        yield statements
    finally:
        connection_created.disconnect(dispatch_uid=_RECORDER_UID)
        connection.execute_wrappers[:] = original
