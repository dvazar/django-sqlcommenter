from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from django.http import (
    FileResponse,
    HttpResponse,
    StreamingHttpResponse,
)
from django.test import RequestFactory

from django_sqlcommenter.middleware import (
    SqlCommentMiddleware,
    _rescope_streaming,
)
from django_sqlcommenter.scope import (
    SqlScope,
    current_scope,
    reset_scope,
    set_scope,
)


def test_scope_is_open_during_the_request() -> None:
    seen: list[dict[str, str] | None] = []

    def get_response(request):
        scope = current_scope()
        seen.append(dict(scope.tags) if scope else None)
        return HttpResponse("ok")

    SqlCommentMiddleware(get_response)(RequestFactory().get("/p/"))
    assert seen[0] == {"route": "/p/", "action": "GET"}


def test_scope_is_closed_after_the_request() -> None:
    SqlCommentMiddleware(lambda r: HttpResponse("ok"))(
        RequestFactory().get("/p/")
    )
    assert current_scope() is None


def test_process_view_refines_the_route() -> None:
    request = RequestFactory().get("/p/abc/")
    middleware = SqlCommentMiddleware(lambda r: None)
    token = middleware.open_scope(request)
    try:
        request.resolver_match = SimpleNamespace(
            route="p/<uuid:pk>/", view_name="app:detail"
        )
        middleware.process_view(request, lambda r: None, (), {})
        scope = current_scope()
        assert scope is not None
        assert scope.tags["route"] == "p/<uuid:pk>/"
        assert scope.tags["controller"] == "app:detail"
    finally:
        reset_scope(token)


def test_get_request_tags_is_merged_in() -> None:
    class Custom(SqlCommentMiddleware):
        def get_request_tags(self, request):
            return {"traceparent": "00-a-b-01"}

    seen: list[dict[str, str] | None] = []

    def get_response(request):
        scope = current_scope()
        seen.append(dict(scope.tags) if scope else None)
        return HttpResponse("ok")

    Custom(get_response)(RequestFactory().get("/p/"))
    assert seen[0] is not None
    assert seen[0]["traceparent"] == "00-a-b-01"


def test_streaming_body_is_produced_inside_the_scope() -> None:
    seen: list[str | None] = []

    def body():
        for _ in range(2):
            scope = current_scope()
            seen.append(scope.tags["route"] if scope else None)
            yield b"chunk"

    response = SqlCommentMiddleware(lambda r: StreamingHttpResponse(body()))(
        RequestFactory().get("/sse")
    )
    list(response.streaming_content)
    assert seen == ["/sse", "/sse"]


def test_scope_does_not_leak_after_streaming() -> None:
    response = SqlCommentMiddleware(
        lambda r: StreamingHttpResponse(iter([b"a", b"b"]))
    )(RequestFactory().get("/sse"))
    list(response.streaming_content)
    assert current_scope() is None


@pytest.mark.asyncio
async def test_async_chain_opens_and_closes_the_scope() -> None:
    seen: list[str | None] = []

    async def get_response(request):
        scope = current_scope()
        seen.append(scope.tags["route"] if scope else None)
        return HttpResponse("ok")

    await SqlCommentMiddleware(get_response)(RequestFactory().get("/a"))
    assert seen == ["/a"]
    assert current_scope() is None


@pytest.mark.asyncio
async def test_async_streaming_body_is_produced_inside_the_scope() -> None:
    seen: list[str | None] = []

    async def body():
        for _ in range(2):
            scope = current_scope()
            seen.append(scope.tags["route"] if scope else None)
            yield b"chunk"

    async def get_response(request):
        return StreamingHttpResponse(body())

    response = await SqlCommentMiddleware(get_response)(
        RequestFactory().get("/sse")
    )
    async for _ in response.streaming_content:
        pass
    assert seen == ["/sse", "/sse"]
    assert current_scope() is None


def test_scope_is_not_visible_to_the_consumer_between_chunks() -> None:
    # The scope is set and reset around each next(), never held across
    # the yield — which is what scope.py's docstring promises readers.
    response = SqlCommentMiddleware(
        lambda r: StreamingHttpResponse(iter([b"a", b"b"]))
    )(RequestFactory().get("/sse"))
    iterator = iter(response.streaming_content)
    next(iterator)
    assert current_scope() is None


def test_a_view_that_raises_still_closes_the_scope() -> None:
    def get_response(request):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        SqlCommentMiddleware(get_response)(RequestFactory().get("/p/"))
    assert current_scope() is None


def test_a_raising_request_tag_hook_does_not_break_the_request() -> None:
    # The hook is a consumer override and this middleware mounts
    # first, so a raise here would fail the site before anything else.
    class Custom(SqlCommentMiddleware):
        def get_request_tags(self, request):
            raise RuntimeError("tracing is down")

    seen: list[dict[str, str] | None] = []

    def get_response(request):
        scope = current_scope()
        seen.append(dict(scope.tags) if scope else None)
        return HttpResponse("ok")

    Custom(get_response)(RequestFactory().get("/p/"))
    assert seen[0] == {"route": "/p/", "action": "GET"}


def test_a_request_tag_hook_returning_none_does_not_break_it_either() -> None:
    class Custom(SqlCommentMiddleware):
        def get_request_tags(self, request):
            return None

    Custom(lambda r: HttpResponse("ok"))(RequestFactory().get("/p/"))
    assert current_scope() is None


def test_request_tags_win_over_the_defaults() -> None:
    class Custom(SqlCommentMiddleware):
        def get_request_tags(self, request):
            return {"route": "override"}

    seen: list[str] = []

    def get_response(request):
        scope = current_scope()
        assert scope is not None
        seen.append(scope.tags["route"])
        return HttpResponse("ok")

    Custom(get_response)(RequestFactory().get("/p/"))
    assert seen == ["override"]


def test_process_view_preserves_the_callsite_flag() -> None:
    request = RequestFactory().get("/p/abc/")
    middleware = SqlCommentMiddleware(lambda r: None)
    token = set_scope(SqlScope(tags={"route": "/p/"}, callsite=False))
    try:
        request.resolver_match = SimpleNamespace(
            route="p/<uuid:pk>/", view_name="app:detail"
        )
        middleware.process_view(request, lambda r: None, (), {})
        scope = current_scope()
        assert scope is not None
        assert scope.callsite is False
    finally:
        reset_scope(token)


def test_process_view_without_a_resolver_match_changes_nothing() -> None:
    request = RequestFactory().get("/p/abc/")
    request.resolver_match = None
    middleware = SqlCommentMiddleware(lambda r: None)
    token = middleware.open_scope(request)
    try:
        middleware.process_view(request, lambda r: None, (), {})
        scope = current_scope()
        assert scope is not None
        assert scope.tags["route"] == "/p/abc/"
    finally:
        reset_scope(token)


def test_a_broken_request_tag_hook_is_reported(reported, caplog) -> None:
    # A guard that says nothing turns a bug into a mystery.
    class Custom(SqlCommentMiddleware):
        def get_request_tags(self, request):
            raise RuntimeError("tracing is down")

    with caplog.at_level(logging.ERROR):
        Custom(lambda r: HttpResponse("ok"))(RequestFactory().get("/p/"))
    assert "Failed to collect request tags" in caplog.text


def test_a_base_exception_from_the_tag_hook_is_not_swallowed() -> None:
    # Swallowing CancelledError inside an ASGI middleware would break
    # cancellation when a client disconnects.
    class Custom(SqlCommentMiddleware):
        def get_request_tags(self, request):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        Custom(lambda r: HttpResponse("ok"))(RequestFactory().get("/p/"))


@pytest.mark.asyncio
async def test_async_scope_is_not_visible_between_chunks() -> None:
    # The sync twin of this is pinned; the async one was not, and
    # holding the scope across the yield there survives the suite.
    async def body():
        yield b"a"
        yield b"b"

    async def get_response(request):
        return StreamingHttpResponse(body())

    response = await SqlCommentMiddleware(get_response)(
        RequestFactory().get("/sse")
    )
    iterator = response.streaming_content.__aiter__()
    await iterator.__anext__()
    assert current_scope() is None


def test_a_streaming_response_with_no_scope_is_left_alone() -> None:
    # Nothing to re-enter. Wrapping it anyway would cost the response
    # its iterator to carry a scope that does not exist.
    #
    # ``_iterator`` rather than the ``streaming_content`` property: the
    # property builds a fresh map object on every access, so identity
    # can only be checked against what it wraps.
    response = StreamingHttpResponse(iter([b"a", b"b"]))
    before = response._iterator
    _rescope_streaming(response)
    assert response._iterator is before
    assert current_scope() is None


def test_process_view_keeps_the_raw_path_when_the_match_is_empty() -> None:
    # Both tags are conditional, and the false side of each is what
    # stops an empty match from replacing a good raw-path tag with
    # nothing at all.
    request = RequestFactory().get("/p/")
    middleware = SqlCommentMiddleware(lambda r: None)
    token = middleware.open_scope(request)
    try:
        request.resolver_match = SimpleNamespace(route="", view_name="")
        middleware.process_view(request, lambda r: None, (), {})
        scope = current_scope()
        assert scope is not None
        assert scope.tags["route"] == "/p/"
        assert "controller" not in scope.tags
    finally:
        reset_scope(token)


def test_a_file_response_keeps_its_zero_copy_path(tmp_path) -> None:
    # Rewrapping streaming_content sets file_to_stream to None, and the
    # WSGI handler then loses wsgi.file_wrapper — a real cost for a
    # response that issues no queries at all.
    path = tmp_path / "x.txt"
    path.write_bytes(b"hello")
    handle = path.open("rb")
    try:
        response = SqlCommentMiddleware(lambda r: FileResponse(handle))(
            RequestFactory().get("/f")
        )
        assert response.file_to_stream is not None
    finally:
        # Not response.close(): that fires request_finished, which
        # reaches for the database this test has no need to open.
        handle.close()
