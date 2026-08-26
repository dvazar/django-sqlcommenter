"""Adversarial tests: nothing that reaches a tag may reach the statement.

**Threat model.** Everything this package puts in a comment comes from
one of five places, and they are not equally trusted:

* ``request.path`` and ``request.method`` — chosen by whoever opened
  the socket. Fully hostile, arbitrary bytes, no upper bound on length.
* values passed to :func:`sql_scope` and :func:`sql_tags` — usually
  derived from request data: a tenant slug, an order reference, a
  search term. Hostile in practice, even when a developer thinks of
  them as internal.
* whatever :meth:`get_request_tags` returns — a consumer override, and
  the obvious thing to put in it is a header value.
* ``SQLCOMMENT_*`` settings — these are code. They share the trust
  level of ``DATABASES``, and a developer who can set
  ``SQLCOMMENT_SEPARATOR`` can already write raw SQL. Pinned at the end
  of this file so the boundary is stated rather than assumed.
* ``callsite`` — a filename from the interpreter.

**The guarantee.** For the first four, nothing can add a single
character to the *statement*. Whatever a tag contains ends up inside
one balanced ``/*…*/`` block whose entire body is drawn from a closed
alphabet — :data:`ALPHABET` — containing neither ``*`` nor ``'``.
Closing the comment early is therefore not merely filtered, it is not
expressible, and the same goes for breaking the ``key='value'``
grammar. :data:`BODY` states that as one regular expression, and every
test here ends by asserting it.

The corpus is not decorative:
:func:`test_the_encoding_is_what_stands_between_a_payload_and_a_table`
runs one of these payloads through a *naive* implementation and watches
sqlite3 drop the table without so much as raising.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from django.db import connection
from django.test import RequestFactory, override_settings

from django_sqlcommenter.comment import SAFE_CHARS, build_comment
from django_sqlcommenter.conf import LEADING, TRAILING
from django_sqlcommenter.middleware import SqlCommentMiddleware
from django_sqlcommenter.scope import (
    SqlScope,
    reset_scope,
    set_scope,
    sql_scope,
    sql_tags,
)
from django_sqlcommenter.wrapper import QueryAnnotator
from tests import views
from tests.views import DEPLOYMENT, PROBE_SQL

#: Every character ``quote`` can emit under this package's safe set:
#: the unreserved set it never encodes, plus ``SAFE_CHARS``, plus the
#: ``%`` that introduces an escape. Asserted, not assumed, by
#: :func:`test_the_encoder_cannot_emit_anything_outside_its_alphabet`.
ALPHABET = r"A-Za-z0-9_.\-~/:{}<>%"

#: ``key='value'`` pairs, comma-joined. Both halves are drawn from
#: :data:`ALPHABET`, so the only quotes a conforming body can contain
#: are the grammar's own, the only commas are its separators, and
#: ``*`` — hence ``*/`` — cannot occur at all.
BODY = re.compile(
    rf"^[{ALPHABET}]+='[{ALPHABET}]*'(,[{ALPHABET}]+='[{ALPHABET}]*')*$"
)

#: What an attacker sends. Named so a failure says which shape broke.
PAYLOADS: dict[str, str] = {
    "close-comment": "*/ DROP TABLE django_content_type; --",
    "close-and-reopen": "*/ UNION SELECT 1 /*",
    "bare-terminator": "*/",
    "bare-opener": "/*",
    "quote-escape": "' OR '1'='1",
    "quote-and-drop": "'; DROP TABLE auth_user; --",
    "backslash-quote": "\\'; DROP TABLE auth_user; --",
    "line-comment": "--",
    "hash-comment": "#",
    "statement-break": "; DROP TABLE auth_user",
    "newline-break": "\n; DROP TABLE auth_user",
    "crlf": "\r\n",
    "format-placeholder": "%s",
    "named-placeholder": "%(pk)s",
    "qmark-placeholder": "?",
    "colon-placeholder": ":pk",
    "doubled-percent": "%%",
    "percent-hex": "%2A",
    "null-byte": "\x00; DROP TABLE auth_user",
    "line-separator": " DROP TABLE auth_user",
    "lone-surrogate": "\ud800",
    "non-ascii": "Ünïcødé",
    "astral": "🙈",
    "backtick": "`auth_user`",
    "double-quote": '"auth_user"',
    "long": "A" * 600,
}

ATTACK = pytest.mark.parametrize(
    "payload", list(PAYLOADS.values()), ids=list(PAYLOADS)
)

#: Payloads in-budget for the default 512-byte comment. Everything
#: longer degrades to no comment at all, which is correct and makes it
#: useless for asserting that a tag arrived.
FITS = {k: v for k, v in PAYLOADS.items() if len(v) < 200}

FITTING_ATTACK = pytest.mark.parametrize(
    "payload", list(FITS.values()), ids=list(FITS)
)

#: Payloads a real HTTP client can put in a URL path. Two are left out:
#: Django rejects a null byte in ``PATH_INFO`` before any of this
#: package runs, which is the correct place for it to be rejected, and
#: a lone surrogate is not expressible in a URL at all — it is what an
#: undecodable filesystem path becomes, which is why it is in the
#: corpus, and the scope tests are where it belongs.
WIRE = {
    k: v for k, v in PAYLOADS.items() if "\x00" not in v and "\ud800" not in v
}

WIRE_ATTACK = pytest.mark.parametrize(
    "payload", list(WIRE.values()), ids=list(WIRE)
)

#: Turn annotation into a single, predictable comment: no static tags
#: to dilute the payload, no callsite to add a second one.
ISOLATED = override_settings(SQLCOMMENT_TAGS={}, SQLCOMMENT_CALLSITE=False)


def comment_of(
    annotated: str,
    original: str,
    position: str = LEADING,
    separator: str = " ",
) -> str:
    """Return the comment spliced into ``original``, or ``""``.

    Asserts on the way that the statement itself came through
    untouched — which is half of what this file is about.
    """
    if annotated == original:
        return ""
    if position == TRAILING:
        assert annotated.startswith(original + separator), annotated
        return annotated[len(original) + len(separator) :]
    assert annotated.endswith(separator + original), annotated
    return annotated[: -(len(original) + len(separator))]


def assert_inert(annotated: str, original: str, **kwargs: str) -> str:
    """Assert ``annotated`` is ``original`` plus one inert comment.

    Returns the comment body, so a caller can go on to assert that the
    payload did arrive — an encoder that dropped everything would pass
    every safety check here and be useless.
    """
    comment = comment_of(annotated, original, **kwargs)
    if not comment:
        return ""
    assert comment.startswith("/*"), comment
    assert comment.endswith("*/"), comment
    body = comment[2:-2]
    # Stated twice on purpose: BODY implies both, but a failure on the
    # narrow assertion names what went wrong.
    assert "*" not in body, body
    assert BODY.match(body), body
    return body


def plain_statement(sql_log) -> str:
    """The probe statement as the driver sees it with no comment.

    Taken by running the query outside any scope rather than by cutting
    the comment off an annotated one: a test that reconstructs the
    statement from the output it is checking cannot catch a comment
    that swallowed part of it.
    """
    views.count()
    statement = sql_log.matching(PROBE_SQL)[-1]
    assert not statement.startswith("/*")
    return statement


@pytest.fixture
def annotate():
    """Run one statement through the annotator, return what it became."""
    annotator = QueryAnnotator()

    def _annotate(sql: str = "SELECT 1", params: object = None) -> str:
        seen: list[str] = []

        def execute(sql, params, many, context):
            seen.append(sql)

        annotator(execute, sql, params, False, {})
        return seen[0]

    return _annotate


# The control. Without it, every test below could be passing because
# the corpus is harmless rather than because the encoder works.


def test_the_encoding_is_what_stands_between_a_payload_and_a_table():
    """Interpolated naively, this payload drops the table.

    Encoded, it does not. sqlite3 does not even raise on the naive
    statement: ``*/`` closes
    the comment, the ``DROP`` that follows is a complete statement, and
    the trailing ``--`` comments out the wreckage so nothing looks
    wrong. Run on a throwaway in-memory database, never the test one.
    """
    payload = PAYLOADS["close-comment"]

    with closing(sqlite3.connect(":memory:")) as naive:
        naive.execute("CREATE TABLE django_content_type (a int)")
        naive.execute(f"/*route='{payload}'*/ SELECT 1")
        assert not _table_exists(naive), "the payload must be dangerous"

    with closing(sqlite3.connect(":memory:")) as guarded:
        guarded.execute("CREATE TABLE django_content_type (a int)")
        guarded.execute(f"{build_comment({'route': payload})} SELECT 1")
        assert _table_exists(guarded)


def _table_exists(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='django_content_type'"
    ).fetchone()
    return bool(row[0])


# The encoder's alphabet, which everything below rests on.


def test_the_encoder_cannot_emit_anything_outside_its_alphabet():
    """Sweep the whole BMP plus an astral sample through the serializer.

    ``ALPHABET`` is a claim about ``urllib.parse.quote`` under this
    package's safe set. Claims about somebody else's library are worth
    exactly what verifies them.
    """
    corpus = "".join(chr(c) for c in range(0x10000)) + "🙈"
    emitted = set(serialized(corpus))
    allowed = set(re.findall(rf"[{ALPHABET}]", "".join(sorted(emitted))))
    assert emitted - allowed == set()
    assert "*" not in emitted
    assert "'" not in emitted


def serialized(value: str) -> str:
    """The value half of a serialized pair, without its quotes."""
    from django_sqlcommenter.comment import serialize_tags

    return serialize_tags({"t": value}).removeprefix("t='").removesuffix("'")


def test_the_safe_set_excludes_the_two_characters_that_matter():
    # Pinning the constant itself: a well-meaning widening of the safe
    # set is the one edit that would quietly undo this whole file.
    assert "*" not in SAFE_CHARS
    assert "'" not in SAFE_CHARS


@ATTACK
def test_a_hostile_pair_cannot_escape_the_comment(payload):
    assert_inert(f"{build_comment({payload: payload})} SELECT 1", "SELECT 1")


@ISOLATED
@FITTING_ATTACK
def test_an_in_budget_payload_does_reach_the_comment(annotate, payload):
    """Keeps every safety assertion in this file from being vacuous.

    An encoder that returned ``""`` for everything would satisfy the
    whole threat model and be worthless. This is the test that says the
    payloads are carried — encoded — rather than quietly discarded.
    """
    with sql_scope(route=payload):
        body = assert_inert(annotate(), "SELECT 1")
    assert body.startswith("route='")
    assert len(body) > len("route=''")


def test_no_combination_of_payloads_can_escape_the_comment():
    """Every payload as a key against every payload as a value.

    The pairwise sweep is what catches a defect that needs two halves —
    a key ending in a character that completes a delimiter the value
    begins.
    """
    values = list(PAYLOADS.values())
    for key in values:
        for value in values:
            annotated = f"{build_comment({key: value})} SELECT 1"
            assert_inert(annotated, "SELECT 1")


# Every way a tag gets in.


@ISOLATED
@ATTACK
def test_a_hostile_scope_value_cannot_escape_the_comment(annotate, payload):
    with sql_scope(route=payload):
        assert_inert(annotate(), "SELECT 1")


@ISOLATED
@ATTACK
def test_a_hostile_scope_key_cannot_escape_the_comment(annotate, payload):
    with sql_scope(**{payload: "v"}):
        assert_inert(annotate(), "SELECT 1")


@ISOLATED
@ATTACK
def test_a_hostile_merged_tag_cannot_escape_the_comment(annotate, payload):
    with sql_scope(route="/x"), sql_tags(tenant=payload):
        assert_inert(annotate(), "SELECT 1")


@ISOLATED
@ATTACK
def test_a_hostile_static_setting_cannot_escape_the_comment(annotate, payload):
    with (
        override_settings(SQLCOMMENT_TAGS={"service": payload}),
        sql_scope(route="/x"),
    ):
        assert_inert(annotate(), "SELECT 1")


@ISOLATED
@ATTACK
def test_a_hostile_type_in_a_scope_cannot_escape_the_comment(
    annotate, payload
):
    """A third-party library driving ``set_scope`` directly.

    ``set_scope`` is public, its ``tags`` are typed as ``str`` and
    nothing enforces that at runtime. An object whose ``__str__``
    returns a payload has to be encoded like any other value, not
    trusted because it was not a string to begin with.
    """

    class Hostile:
        def __str__(self) -> str:
            return payload

    token = set_scope(SqlScope(tags={"order": Hostile()}, callsite=False))
    try:
        assert_inert(annotate(), "SELECT 1")
    finally:
        reset_scope(token)


@ISOLATED
@ATTACK
def test_a_hostile_request_tag_hook_cannot_escape_the_comment(payload):
    """The consumer override, which is where a raw header lands."""

    class Custom(SqlCommentMiddleware):
        def get_request_tags(self, request):
            return {"traceparent": request.headers.get("traceparent", "")}

    annotator = QueryAnnotator()
    seen: list[str] = []

    def view(request):
        annotator(
            lambda sql, *a: seen.append(sql), "SELECT 1", None, False, {}
        )
        return SimpleNamespace(streaming=False)

    request = RequestFactory().get("/x", headers={"traceparent": payload})
    Custom(view)(request)
    assert_inert(seen[0], "SELECT 1")


@ISOLATED
@ATTACK
def test_a_hostile_url_pattern_cannot_escape_the_comment(annotate, payload):
    """``route`` and ``controller`` as taken from the resolver match."""
    request = RequestFactory().get("/x")
    middleware = SqlCommentMiddleware(lambda r: None)
    token = middleware.open_scope(request)
    try:
        request.resolver_match = SimpleNamespace(
            route=payload, view_name=payload
        )
        middleware.process_view(request, lambda r: None, (), {})
        assert_inert(annotate(), "SELECT 1")
    finally:
        reset_scope(token)


@ISOLATED
@ATTACK
def test_a_hostile_callsite_cannot_escape_the_comment(
    annotate, payload, monkeypatch
):
    """A source path is not sanitized either, and need not be.

    A filename is not attacker-controlled in any deployment worth
    defending, but it is the one tag the package supplies to itself,
    and it goes through the same encoder as everything else. Pinning it
    keeps a future shortcut — interpolating a path known to be "safe" —
    from being an easy edit.
    """
    monkeypatch.setattr(
        "django_sqlcommenter.wrapper.resolve_callsite", lambda: payload
    )
    with override_settings(SQLCOMMENT_CALLSITE=True), sql_scope(route="/x"):
        assert_inert(annotate(), "SELECT 1")


@ISOLATED
@ATTACK
def test_a_mapping_that_changes_between_reads_cannot_smuggle_a_value(
    annotate, payload
):
    """A tag object that answers differently each time it is read.

    Whatever it returns is encoded, so the second answer is no more
    dangerous than the first — but the point of the test is that there
    is no read the encoder does not cover.
    """
    calls: list[str] = []

    class Shifty(Mapping):
        def __getitem__(self, key):
            calls.append(key)
            return "benign" if len(calls) == 1 else payload

        def __iter__(self):
            return iter(("route",))

        def __len__(self):
            return 1

    token = set_scope(SqlScope(tags=Shifty(), callsite=False))
    try:
        assert_inert(annotate(), "SELECT 1")
    finally:
        reset_scope(token)


# Over the wire: the path and the method, straight off the socket.


@DEPLOYMENT
@WIRE_ATTACK
@pytest.mark.django_db
def test_a_hostile_url_path_cannot_reach_the_statement(
    client, sql_log, payload
):
    """The real attack: a percent-encoded payload in the request line.

    Django unquotes ``PATH_INFO``, so what the middleware tags with is
    the decoded payload, byte for byte. The URL does not resolve — that
    is the point, since an unresolved URL never reaches
    ``process_view`` and the raw path stands as the ``route`` tag all
    the way to the driver.
    """
    plain = plain_statement(sql_log)
    response = client.get("/" + quote(payload, safe=""))
    assert response.status_code == 404
    assert_inert(sql_log.matching(PROBE_SQL)[-1], plain)


@ISOLATED
@ATTACK
def test_a_hostile_http_method_cannot_escape_the_comment(annotate, payload):
    """``action`` comes from the request line too.

    A method token is not restricted to the verbs anyone has heard of:
    a client can send whatever fits, and Django hands it through.
    """
    request = RequestFactory().get("/x")
    request.method = payload
    token = SqlCommentMiddleware(lambda r: None).open_scope(request)
    try:
        assert_inert(annotate(), "SELECT 1")
    finally:
        reset_scope(token)


# Against a real driver.


@pytest.mark.django_db
@ATTACK
def test_a_hostile_tag_leaves_a_real_statement_running(sql_log, payload):
    """The whole chain, on a real connection, for every payload."""
    plain = plain_statement(sql_log)
    expected = views.count()
    with sql_scope(route=payload):
        assert views.count() == expected
    assert_inert(sql_log.matching(PROBE_SQL)[-1], plain)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    ["%s", "%(label)s", "?", ":label", "%%", "%2A"],
    ids=["format", "named", "qmark", "colon", "doubled", "hex"],
)
def test_a_placeholder_in_a_tag_does_not_shift_the_bound_parameters(
    sql_log, payload
):
    """The percent-doubling rule, tested where it can actually fail.

    A parameterized statement is a format string to the driver, and the
    comment is part of it. Get the doubling wrong and the binding
    shifts, the driver raises, or — worst — a parameter lands in the
    wrong position and the query silently answers about something else.
    """
    expected = views.count_labelled("auth")
    assert expected
    with sql_scope(route=payload):
        assert views.count_labelled("auth") == expected
        assert views.count_labelled("no-such-app") == 0


@pytest.mark.django_db
@ATTACK
def test_a_hostile_tag_survives_named_parameters(sql_log, payload):
    """The other driver path: ``%(name)s`` binding.

    Django's SQLite backend runs ``query % {...}`` over the whole
    statement for a mapping of parameters — comment included. Every
    percent in the comment has to be doubled or this raises
    ``ValueError: unsupported format character``, and no ORM query
    takes this path so nothing else in the suite reaches it.
    """
    with sql_scope(route=payload), connection.cursor() as cursor:
        cursor.execute("SELECT %(n)s", {"n": 7})
        assert cursor.fetchone()[0] == 7


@pytest.mark.django_db
@ATTACK
def test_a_hostile_tag_survives_executemany(sql_log, payload):
    with sql_scope(route=payload), connection.cursor() as cursor:
        cursor.execute("CREATE TEMPORARY TABLE probe (a int)")
        cursor.executemany("INSERT INTO probe VALUES (%s)", [(1,), (2,)])
        cursor.execute("SELECT count(*) FROM probe")
        assert cursor.fetchone()[0] == 2


@pytest.mark.django_db
@ATTACK
def test_a_trailing_comment_is_no_easier_to_escape(sql_log, payload):
    """Position ``trailing`` puts the payload after the statement.

    Anything that leaks there is appended to executable SQL rather than
    prepended to it, so the same guarantee has to hold with the
    comment on the other side.
    """
    plain = plain_statement(sql_log)
    with (
        override_settings(SQLCOMMENT_POSITION="trailing"),
        sql_scope(route=payload),
    ):
        views.count()
    assert_inert(sql_log.matching(PROBE_SQL)[-1], plain, position=TRAILING)


# Degradation under a payload chosen for size rather than syntax.


@DEPLOYMENT
@pytest.mark.django_db
def test_an_oversized_path_degrades_to_no_comment_at_all(client, sql_log):
    """A 40 KB URL costs the comment, never the query.

    ``route`` outranks every other tag, so a long enough one evicts the
    rest and is then dropped itself for not fitting alone. The result
    is a bare statement: telemetry goes quiet, the request is served.
    """
    response = client.get("/" + "a" * 40_000)
    assert response.status_code == 404
    annotated = sql_log.matching(PROBE_SQL)[-1]
    assert annotated.startswith("SELECT")


@ISOLATED
def test_a_long_route_evicts_the_lower_ranked_tags_first(annotate):
    """Pinned because it is a real cost, not only a safety property.

    A client that controls ``route`` controls how much of the budget is
    left for everything else. It cannot break a statement, and it can
    cost a deployment the ``callsite`` and ``tenant`` tags it was
    relying on. ``SQLCOMMENT_MAX_LENGTH`` is the dial.

    ``tenant`` rather than a second known key on purpose: it sorts
    *after* ``route``, so a victim chosen by name alone would drop the
    route and keep it. Only a victim chosen by rank drops ``tenant``.
    """
    with sql_scope(route="/" + "a" * 490, tenant="acme"):
        body = assert_inert(annotate(), "SELECT 1")
    assert "tenant=" not in body
    assert "route=" in body


# The boundary, stated rather than assumed.


@ISOLATED
@override_settings(SQLCOMMENT_SEPARATOR="; SELECT 'oops'; ")
def test_the_separator_is_the_one_value_that_is_not_encoded(annotate):
    """Documents where the guarantee stops, and why it stops there.

    ``SQLCOMMENT_SEPARATOR`` is spliced between the comment and the
    statement verbatim, because it exists to be a space or a newline
    and encoding it would defeat it. That makes it the one input that
    can reach the statement — and it is a setting, so anyone who can
    set it can already write raw SQL, add a database router or replace
    the backend. It is not a hole in the boundary; it is outside it.

    The system checks reject the two forms that break the comment
    rather than merely abuse it, so the plausible typo is still caught
    at startup.
    """
    with sql_scope(route="/x"):
        annotated = annotate()
    assert annotated == "/*route='/x'*/; SELECT 'oops'; SELECT 1"


@pytest.mark.parametrize("bad", ["*/", " -- ", "a*/b", "x--y"])
def test_a_separator_that_would_break_the_comment_is_rejected(bad):
    from django_sqlcommenter.checks import check_settings

    with override_settings(SQLCOMMENT_SEPARATOR=bad):
        ids = [m.id for m in check_settings()]
    assert "django_sqlcommenter.E007" in ids
