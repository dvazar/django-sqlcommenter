"""Views and middleware that the end-to-end tests drive.

The seam tests run Django's own request handler rather than calling
middleware by hand, so URL resolution, :meth:`process_view`, the
streaming handoff and the frame depth the callsite walk has to climb
are all exercised the way a deployment exercises them.

Every view issues a real query. That is the point: the unit tests stop
at a recorder standing in for the driver, and the seams between the
middleware, the scope, the wrapper and the database are exactly what
they cannot reach.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from django.contrib.contenttypes.models import ContentType
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.test import override_settings

#: The deployment the end-to-end tests run against: this package's
#: middleware first, as it is documented to be mounted, and one that
#: queries below it.
DEPLOYMENT = override_settings(
    ROOT_URLCONF="tests.urls",
    MIDDLEWARE=[
        "django_sqlcommenter.middleware.SqlCommentMiddleware",
        "tests.views.QueryingMiddleware",
    ],
    SQLCOMMENT_TAGS={},
)

#: Fragment identifying the query every view in this module makes.
PROBE_SQL = 'FROM "django_content_type"'


def count() -> int:
    """One real, unparameterized query."""
    return ContentType.objects.count()


def count_labelled(label: str) -> int:
    """One real query carrying a bound parameter.

    The parameter matters: the driver interpolates ``%``-placeholders
    only when parameters are passed, which is the branch that doubles
    the percent signs URL-encoding puts in the comment.
    """
    return ContentType.objects.filter(app_label=label).count()


def plain(request: HttpRequest) -> HttpResponse:
    return HttpResponse(str(count()))


def detail(request: HttpRequest, pk: int) -> HttpResponse:
    return HttpResponse(str(count_labelled("auth")))


def stream(request: HttpRequest) -> StreamingHttpResponse:
    """Produce the body — and its queries — after the chain returns."""

    def body():
        for _ in range(2):
            yield str(count()).encode()

    return StreamingHttpResponse(body())


async def async_plain(request: HttpRequest) -> HttpResponse:
    """An async view whose query runs in a worker thread, as deployed."""
    return HttpResponse(str(await sync_to_async(count)()))


def boom(request: HttpRequest) -> HttpResponse:
    raise ValueError("view exploded")


class QueryingMiddleware:
    """Issue a query before the view, the way session and auth do.

    Mounted *below* :class:`~django_sqlcommenter.middleware.
    SqlCommentMiddleware`, so its query lands in the coarse scope that
    is keyed on the raw path — before ``process_view`` has had a chance
    to refine anything. It is the only way to observe the first phase
    end to end, and the only way to reach the wrapper on a request
    whose URL does not resolve at all.
    """

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> Any:
        count()
        return self.get_response(request)
