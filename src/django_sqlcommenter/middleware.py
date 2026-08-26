"""The HTTP entry point: open an annotation scope per request."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, cast

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from django.http import HttpRequest, HttpResponseBase, StreamingHttpResponse

from ._report import report_once
from .scope import SqlScope, current_scope, reset_scope, set_scope

__all__ = ["SqlCommentMiddleware"]


def _scoped_iterator(
    iterator: Iterator[Any], scope: SqlScope
) -> Iterator[Any]:
    """Re-enter ``scope`` around the production of each chunk.

    The scope is set and reset around ``next()`` rather than held across
    the ``yield``: the token is then created and released in the same
    context, and nothing leaks into the consumer between chunks.
    """
    iterator = iter(iterator)
    while True:
        token = set_scope(scope)
        try:
            chunk = next(iterator)
        except StopIteration:
            return
        finally:
            reset_scope(token)
        yield chunk


async def _scoped_aiterator(
    iterator: AsyncIterator[Any], scope: SqlScope
) -> AsyncIterator[Any]:
    """Async twin of :func:`_scoped_iterator`."""
    iterator = iterator.__aiter__()
    while True:
        token = set_scope(scope)
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            return
        finally:
            reset_scope(token)
        yield chunk


def _rescope_streaming(response: HttpResponseBase | None) -> None:
    """Keep the scope alive while a streaming body is produced.

    Without this, a server-sent-events endpoint issues every one of its
    queries after the middleware chain has already closed the scope —
    the gap the per-request ``execute_wrapper`` pattern leaves open.
    """
    if not isinstance(response, StreamingHttpResponse):
        return
    if getattr(response, "file_to_stream", None) is not None:
        # A FileResponse streams a file handle, not queries. Rewrapping
        # it would set file_to_stream to None and cost the server its
        # wsgi.file_wrapper zero-copy path for nothing.
        return
    scope = current_scope()
    if scope is None:
        return
    # ``streaming_content`` is typed as the union of both iterator
    # kinds; ``is_async`` is a plain attribute, so it cannot narrow it.
    if response.is_async:
        response.streaming_content = _scoped_aiterator(
            cast("AsyncIterator[Any]", response.streaming_content), scope
        )
    else:
        response.streaming_content = _scoped_iterator(
            cast("Iterator[Any]", response.streaming_content), scope
        )


class SqlCommentMiddleware:
    """Open a SQL annotation scope around each HTTP request.

    Two-phase. ``__call__`` opens a coarse scope keyed on the raw path,
    so queries issued by upstream middleware (session and auth lookups)
    are annotated too. ``process_view`` then refines it to the matched
    URL pattern — which is the only meaningful identity when a single
    view backs many routes, as with API frameworks that mount one
    dispatching view.

    Mount FIRST in ``MIDDLEWARE``.

    Subclass and override :meth:`get_request_tags` to add
    deployment-specific tags — a trace id, a tenant, a build. It runs
    once per request, not once per query.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    def get_request_tags(self, request: HttpRequest) -> Mapping[str, str]:
        """Extra tags for this request. Override in a subclass.

        Called once per request, before the view runs. Return an empty
        mapping to add nothing.
        """
        return {}

    def open_scope(self, request: HttpRequest) -> Any:
        """Open the coarse scope; returns the token to reset it with."""
        tags = {"route": request.path, "action": request.method or ""}
        try:
            tags.update(self.get_request_tags(request))
        except Exception:
            # get_request_tags is a consumer override, and it is
            # telemetry like everything else here: a hook that raises
            # or returns something that is not a mapping has to cost
            # its tags, not the request. This middleware is documented
            # to mount first, so failing here would take the site down
            # before anything else got a chance to run.
            report_once(
                "Failed to collect request tags; the request ran without them."
            )
        return set_scope(SqlScope(tags=tags))

    def __call__(self, request: HttpRequest) -> Any:
        if self._is_async:
            return self._acall(request)
        token = self.open_scope(request)
        try:
            response = self.get_response(request)
            _rescope_streaming(response)
            return response
        finally:
            reset_scope(token)

    async def _acall(self, request: HttpRequest) -> Any:
        token = self.open_scope(request)
        try:
            response = await self.get_response(request)
            _rescope_streaming(response)
            return response
        finally:
            reset_scope(token)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Any,
        view_args: Any,
        view_kwargs: Any,
    ) -> None:
        """Refine the scope now that the URL has been resolved."""
        match = request.resolver_match
        if match is None:
            return None
        scope = current_scope()
        tags = dict(scope.tags) if scope else {}
        if getattr(match, "route", None):
            tags["route"] = match.route
        if getattr(match, "view_name", None):
            tags["controller"] = match.view_name
        # Preserve the flag the way sql_tags does: a consumer who
        # opened the scope with callsite off on a bulk endpoint should
        # not get it back the moment the URL resolves.
        set_scope(
            SqlScope(tags=tags, callsite=scope.callsite if scope else True)
        )
        return None
