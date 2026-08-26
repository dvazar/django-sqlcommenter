"""Django app config: wire the annotator into the process on startup."""

from __future__ import annotations

from django.apps import AppConfig

__all__ = ["SqlCommenterConfig"]


class SqlCommenterConfig(AppConfig):
    """Add ``"django_sqlcommenter"`` to ``INSTALLED_APPS`` and you are done."""

    name = "django_sqlcommenter"
    verbose_name = "SQL Commenter"

    def ready(self) -> None:
        from django.core.checks import register

        from . import install
        from .checks import check_settings

        register(check_settings)
        install()
