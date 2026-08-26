from __future__ import annotations

from django_sqlcommenter.comment import build_comment, serialize_tags


def test_pairs_are_sorted_lexicographically() -> None:
    assert serialize_tags({"route": "/x", "action": "GET"}) == (
        "action='GET',route='/x'"
    )


def test_readable_characters_survive_encoding() -> None:
    assert serialize_tags({"callsite": "app/api.py:118"}) == (
        "callsite='app/api.py:118'"
    )


def test_django_route_syntax_survives_encoding() -> None:
    # This is what ResolverMatch.route looks like; percent-encoding the
    # angle brackets would make the most-read tag the least readable.
    assert serialize_tags({"route": "cart/<int:pk>/"}) == (
        "route='cart/<int:pk>/'"
    )


def test_openapi_route_syntax_survives_encoding() -> None:
    assert serialize_tags({"route": "/p/{id}"}) == "route='/p/{id}'"


def test_space_is_encoded() -> None:
    assert serialize_tags({"a": "x y"}) == "a='x%20y'"


def test_keys_are_encoded_too() -> None:
    assert serialize_tags({"a b": "1"}) == "a%20b='1'"


def test_star_is_encoded_so_a_comment_cannot_close_early() -> None:
    assert "*/" not in serialize_tags({"a": "*/ DROP TABLE x --"})


def test_single_quote_is_encoded() -> None:
    result = serialize_tags({"a": "it's"})
    assert result == "a='it%27s'"
    assert result.count("'") == 2


def test_a_lone_surrogate_degrades_the_tag_not_the_statement() -> None:
    # A path that is not valid UTF-8 decodes to lone surrogates, so a
    # callsite tag can carry one. Raising here would take down every
    # query in the process instead of one comment.
    result = serialize_tags({"callsite": "app/\ud800.py:1"})
    assert result.startswith("callsite='app/")
    assert result.isascii()


def test_a_non_string_value_is_coerced_rather_than_raising() -> None:
    # sql_scope(order=order.pk) with an int is the mistake everyone
    # makes once. Raising here would fail every query in the scope.
    assert serialize_tags({"order": 7}) == "order='7'"


def test_falsy_values_that_are_not_empty_are_kept() -> None:
    # Truthiness filtering would silently lose shard 0 — and 0 is a
    # perfectly ordinary shard.
    assert serialize_tags({"shard": 0}) == "shard='0'"
    assert serialize_tags({"readonly": False}) == "readonly='False'"


def test_a_none_value_is_dropped() -> None:
    assert serialize_tags({"tenant": None, "b": "1"}) == "b='1'"


def test_a_value_whose_str_raises_drops_the_tag_not_the_query() -> None:
    # Pass a Django model instead of its pk and __str__ can raise on an
    # unsaved foreign key. Telemetry must never fail a query.
    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert serialize_tags({"order": Exploding(), "b": "1"}) == "b='1'"


def test_a_key_that_renders_to_nothing_drops_the_pair() -> None:
    # A SQLCOMMENT_TAGS dict with an empty key is trivially reachable,
    # and `='v'` is a pair a spec-compliant parser would reject.
    assert serialize_tags({"": "v", "b": "1"}) == "b='1'"


def test_empty_values_are_dropped() -> None:
    assert serialize_tags({"a": "", "b": "1"}) == "b='1'"


def test_no_tags_serializes_to_empty_string() -> None:
    assert serialize_tags({}) == ""


def test_build_wraps_pairs_in_a_block_comment() -> None:
    assert build_comment({"a": "1"}) == "/*a='1'*/"


def test_build_with_no_tags_produces_no_comment() -> None:
    assert build_comment({}) == ""


def test_build_with_only_empty_values_produces_no_comment() -> None:
    assert build_comment({"a": ""}) == ""


def test_custom_tags_are_dropped_before_known_ones() -> None:
    tags = {"route": "/x", "callsite": "a.py:1", "mine": "y" * 100}
    result = build_comment(tags, max_length=60)
    assert "route=" in result
    assert "mine=" not in result


def test_traceparent_is_dropped_before_callsite() -> None:
    tags = {
        "route": "/x",
        "callsite": "a.py:1",
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    }
    result = build_comment(tags, max_length=45)
    assert "callsite=" in result
    assert "traceparent=" not in result


def test_result_never_exceeds_the_budget() -> None:
    tags = {"route": "/x", "callsite": "a.py:1", "mine": "y" * 100}
    assert len(build_comment(tags, max_length=60)) <= 60


def test_the_budget_is_exact_to_the_byte() -> None:
    # /*a='1'*/ is nine characters, so nine fits and eight does not.
    # This pins the four characters charged for the delimiters.
    assert build_comment({"a": "1"}, max_length=9) == "/*a='1'*/"
    assert build_comment({"a": "1"}, max_length=8) == ""


def test_a_budget_below_the_delimiters_yields_no_comment() -> None:
    assert build_comment({"a": "1"}, max_length=0) == ""


def test_a_single_oversized_tag_yields_no_comment() -> None:
    assert build_comment({"route": "y" * 100}, max_length=20) == ""
