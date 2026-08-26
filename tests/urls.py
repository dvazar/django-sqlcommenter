"""A real urlconf for the end-to-end tests.

``tests.settings`` keeps an empty one so that the unit tests pay
nothing for URL resolution; the seam tests switch to this with
``override_settings(ROOT_URLCONF="tests.urls")``.
"""

from __future__ import annotations

from django.urls import path

from tests import views

urlpatterns = [
    path("plain/", views.plain, name="plain"),
    path("detail/<int:pk>/", views.detail, name="detail"),
    path("stream/", views.stream, name="stream"),
    path("async/", views.async_plain, name="async"),
    path("boom/", views.boom, name="boom"),
]
