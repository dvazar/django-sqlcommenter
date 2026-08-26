"""End-to-end tests across the seams between modules.

Every other file in this suite tests one module against a stand-in for
its neighbours: a recorder for the driver, a ``SimpleNamespace`` for
the resolver match, a hand-built ``RequestFactory`` request for the
handler. That is the right shape for unit tests and it leaves the
joints uncovered — and the joints are where the last round of defects
lived. Nothing here mocks anything: a real request goes through the
real handler, the real middleware chain and the real ORM, and the
assertions are made against the statement the database driver received.
"""

from __future__ import annotations

import pytest

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.test import override_settings

from django_sqlcommenter.scope import current_scope, sql_scope
from tests import views
from tests.views import DEPLOYMENT as PROJECT
from tests.views import PROBE_SQL as PROBE

pytestmark = pytest.mark.django_db


@PROJECT
def test_a_request_annotates_the_query_its_view_makes(client, sql_log):
    response = client.get("/plain/")
    assert response.status_code == 200
    annotated = sql_log.matching(PROBE)[-1]
    assert annotated.startswith("/*")
    assert "action='GET'" in annotated
    assert "route='plain/'" in annotated


@PROJECT
def test_the_matched_url_pattern_replaces_the_raw_path(client, sql_log):
    """``process_view`` refines the scope, through real resolution.

    The unit test for this hands ``process_view`` a
    ``SimpleNamespace``. Only a real request proves that Django puts a
    ``resolver_match`` on the request before ``process_view`` runs, and
    that ``route`` and ``view_name`` are the attributes it carries.
    """
    client.get("/detail/7/")
    annotated = sql_log.matching(PROBE)[-1]
    # The raw path — with its 7 — is what a naive implementation would
    # tag, giving one distinct route per primary key in the database.
    # The angle brackets survive: they are in the encoder's safe set
    # precisely so a route reads the way it does in urls.py.
    assert "route='detail/<int:pk>/'" in annotated
    assert "controller='detail'" in annotated
    assert "/detail/7/" not in annotated


@PROJECT
def test_a_query_made_before_the_view_carries_the_raw_path(client, sql_log):
    """The reason the scope is opened in two phases.

    Session and authentication middleware query on the way in, long
    before the URL is resolved. A per-view scope would leave those
    unannotated; this coarse first phase is what covers them.
    """
    client.get("/detail/7/")
    first, second = sql_log.matching(PROBE)[-2:]
    assert "route='/detail/7/'" in first
    assert "route='detail/<int:pk>/'" in second


@PROJECT
def test_a_url_that_does_not_resolve_is_still_annotated(client, sql_log):
    """A 404 never reaches ``process_view``, so the raw path stands.

    This is the route-cardinality caveat the README documents, pinned:
    a scanner walking a thousand nonexistent URLs produces a thousand
    distinct ``route`` values.
    """
    response = client.get("/no-such-url/")
    assert response.status_code == 404
    assert "route='/no-such-url/'" in sql_log.matching(PROBE)[-1]


@PROJECT
def test_queries_made_while_streaming_are_annotated(client, sql_log):
    """The gap a per-request execute wrapper leaves open.

    The body of a streaming response — and every query it makes — is
    produced after the middleware chain has already returned. Consuming
    it here, outside the request, is exactly what the WSGI server does.
    """
    response = client.get("/stream/")
    before = len(sql_log.matching(PROBE))
    b"".join(response.streaming_content)
    produced = sql_log.matching(PROBE)[before:]
    assert len(produced) == 2
    assert all("route='stream/'" in sql for sql in produced)


@PROJECT
def test_the_scope_does_not_outlive_the_streamed_body(client, sql_log):
    response = client.get("/stream/")
    b"".join(response.streaming_content)
    assert current_scope() is None
    views.count()
    assert not sql_log.matching(PROBE)[-1].startswith("/*")


@PROJECT
def test_a_view_that_raises_does_not_leak_the_scope(client, sql_log):
    with pytest.raises(ValueError, match="view exploded"):
        client.get("/boom/")
    assert current_scope() is None
    # The query the middleware below ours made still got its comment.
    assert "route='/boom/'" in sql_log.matching(PROBE)[-1]


@PROJECT
def test_the_callsite_names_the_view_that_issued_the_query(client, sql_log):
    """The frame walk, against a real handler stack.

    ``SQLCOMMENT_MAX_DEPTH`` was set from a measurement; this is the
    test that fails if Django adds frames between the view and the
    cursor, or if the walk starts counting from the wrong place.
    """
    client.get("/plain/")
    annotated = sql_log.matching(PROBE)[-1]
    assert "callsite='tests/views.py:" in annotated


@PROJECT
def test_the_callsite_survives_the_depth_of_a_streamed_body(client, sql_log):
    response = client.get("/stream/")
    b"".join(response.streaming_content)
    assert "callsite='tests/views.py:" in sql_log.matching(PROBE)[-1]


@PROJECT
@override_settings(SQLCOMMENT_TAGS={"service": "checkout"})
def test_a_static_tag_reaches_the_driver(client, sql_log):
    client.get("/plain/")
    assert "service='checkout'" in sql_log.matching(PROBE)[-1]


@PROJECT
def test_a_setting_change_reaches_the_very_next_query(client, sql_log):
    """The snapshot is invalidated across module boundaries.

    ``conf`` caches every setting until ``setting_changed`` fires. The
    receiver lives in ``conf``, the cache it also has to drop lives in
    ``callsite``, and the reader lives in ``wrapper`` — three modules
    that only ever meet here.
    """
    client.get("/plain/")
    assert "release=" not in sql_log.matching(PROBE)[-1]
    with override_settings(SQLCOMMENT_TAGS={"release": "b17"}):
        client.get("/plain/")
        assert "release='b17'" in sql_log.matching(PROBE)[-1]
    client.get("/plain/")
    assert "release=" not in sql_log.matching(PROBE)[-1]


@PROJECT
def test_disabling_at_runtime_stops_annotating_an_open_connection(
    client, sql_log
):
    """The switch is honored per query, not only at install time.

    The wrapper is already on the connection by now, which is the case
    the per-query check exists for: a consumer's test suite flips this
    off to assert on generated SQL without restarting the process.
    """
    with override_settings(SQLCOMMENT_ENABLED=False):
        client.get("/plain/")
        assert not sql_log.matching(PROBE)[-1].startswith("/*")
    client.get("/plain/")
    assert sql_log.matching(PROBE)[-1].startswith("/*")


def test_a_query_outside_any_request_is_not_annotated(sql_log):
    views.count()
    assert sql_log.matching(PROBE)[-1].startswith("SELECT")


def test_a_management_command_annotates_the_queries_it_makes(sql_log):
    """``management`` patches ``BaseCommand``; this proves it reaches SQL.

    The unit tests for that patch assert on the scope. Between the
    scope and a comment on a real statement sit the wrapper, the
    serializer and the connection, none of which a command touches in
    those tests.
    """

    class Probe(BaseCommand):
        def handle(self, *args, **options):
            views.count()

    call_command(Probe(), verbosity=0)
    annotated = sql_log.matching(PROBE)[-1]
    assert "command='test_integration'" in annotated
    assert current_scope() is None


def test_transaction_bookkeeping_is_not_annotated(sql_log):
    """SAVEPOINT and friends are skipped, on real statements.

    The unit test feeds the prefixes to the annotator directly. Only a
    real ``atomic()`` block proves the prefix list matches what the ORM
    actually emits.
    """
    with sql_scope(job="nightly"), transaction.atomic():
        views.count()
    bookkeeping = [
        sql
        for sql in sql_log
        if sql.lstrip().upper().startswith(("SAVEPOINT", "RELEASE"))
    ]
    assert bookkeeping, "expected the atomic block to emit savepoints"
    assert not any(sql.startswith("/*") for sql in bookkeeping)
    assert sql_log.matching(PROBE)[-1].startswith("/*")


@PROJECT
def test_the_comment_stays_out_of_connection_queries_on_sqlite(client):
    """Pins the promise the README makes to consumers, backend and all.

    A consumer's own tests assert on ``connection.queries``, and this
    package must not break them. On SQLite that holds because the debug
    cursor reports the statement Django built rather than the one the
    driver ran; on PostgreSQL, MySQL and Oracle the comment *is*
    visible there. The suite runs on SQLite, so this test can only
    pin the SQLite half — the other half is documented, not tested.
    """
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        client.get("/plain/")
    probes = [q["sql"] for q in captured if PROBE in q["sql"]]
    assert probes
    assert not any(sql.startswith("/*") for sql in probes)


@PROJECT
def test_a_parameterized_query_still_binds_its_parameters(client, sql_log):
    """The percent-doubling rule, against a real driver.

    ``count_labelled`` passes a bound parameter, so the driver treats
    the whole statement — comment included — as a format string. If the
    doubling were wrong the query would raise here rather than return
    a number.
    """
    expected = views.count_labelled("auth")
    response = client.get("/detail/7/")
    assert response.status_code == 200
    assert int(response.content) == expected
    assert sql_log.matching(PROBE)[-1].startswith("/*")


@PROJECT
def test_the_comment_is_the_only_difference_from_the_plain_statement(
    client, sql_log
):
    """Whatever else it does, annotation must not rewrite the SQL."""
    with override_settings(SQLCOMMENT_ENABLED=False):
        client.get("/plain/")
    plain = sql_log.matching(PROBE)[-1]
    client.get("/plain/")
    annotated = sql_log.matching(PROBE)[-1]
    assert annotated != plain
    assert annotated.endswith(f" {plain}")
    assert annotated[: -len(plain) - 1].startswith("/*")


@PROJECT
async def test_an_async_request_annotates_its_query(async_client, sql_log):
    """ASGI, end to end, including the thread hop the ORM makes.

    Under ASGI a synchronous database call runs in a worker thread, and
    ``connections`` is thread-critical — so the query lands on a
    connection that did not exist when this test started. Two things
    have to hold for it to be annotated: asgiref has to carry the
    context across the hop, and the package's ``connection_created``
    receiver has to wire the new connection up on its own.
    """
    response = await async_client.get("/async/")
    assert response.status_code == 200
    annotated = sql_log.matching(PROBE)[-1]
    assert "route='async/'" in annotated
    assert "action='GET'" in annotated
