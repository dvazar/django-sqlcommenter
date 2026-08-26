"""Minimal Django settings for the package's own test suite."""

from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "not-a-secret-this-is-a-test-suite"
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_sqlcommenter",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

MIDDLEWARE = ["django_sqlcommenter.middleware.SqlCommentMiddleware"]

ROOT_URLCONF = "tests.settings"
urlpatterns: list[Any] = []
