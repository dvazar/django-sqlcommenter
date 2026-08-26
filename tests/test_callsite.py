from __future__ import annotations

import threading
from typing import Any

from django.conf import settings
from django.core.signals import setting_changed
from django.test import override_settings

from django_sqlcommenter import callsite as callsite_module
from django_sqlcommenter.callsite import relative_path, resolve_callsite
from django_sqlcommenter.conf import conf


def _one_level_down() -> str | None:
    return resolve_callsite()


def setup_function() -> None:
    callsite_module.clear_cache()


def test_project_file_becomes_root_relative() -> None:
    absolute = f"{conf.PROJECT_ROOT}/tests/test_callsite.py"
    assert relative_path(absolute) == "tests/test_callsite.py"


def test_site_packages_is_not_ours() -> None:
    absolute = f"{conf.PROJECT_ROOT}/.venv/lib/site-packages/django/x.py"
    assert relative_path(absolute) is None


def test_a_file_outside_the_root_is_not_ours() -> None:
    assert relative_path("/usr/lib/python3.14/json/decoder.py") is None


def test_the_package_itself_is_not_ours() -> None:
    assert relative_path(callsite_module.__file__) is None


def test_results_are_cached() -> None:
    absolute = f"{conf.PROJECT_ROOT}/tests/test_callsite.py"
    relative_path(absolute)
    assert absolute in callsite_module._cache


def test_returns_the_calling_test_file_and_line() -> None:
    result = resolve_callsite()
    assert result is not None
    path, _, line = result.rpartition(":")
    assert path == "tests/test_callsite.py"
    assert line.isdigit()


def test_walks_past_frames_that_are_not_ours() -> None:
    result = _one_level_down()
    assert result is not None
    assert result.startswith("tests/test_callsite.py:")


def _through_foreign_frames(depth: int) -> str | None:
    """Call ``resolve_callsite`` from ``depth`` frames that are not ours.

    The frames have to be compiled under a filename outside the project
    root: a plain nested function defined here would be project code,
    and the walk would stop on its very first step.
    """
    source = (
        "def recurse(depth, leaf):\n"
        "    if depth:\n"
        "        return recurse(depth - 1, leaf)\n"
        "    return leaf()\n"
    )
    namespace: dict[str, Any] = {}
    exec(compile(source, "/nowhere/lib/vendor.py", "exec"), namespace)
    result: str | None = namespace["recurse"](depth, resolve_callsite)
    return result


def test_a_project_frame_below_foreign_ones_is_still_found() -> None:
    result = _through_foreign_frames(conf.MAX_DEPTH // 2)
    assert result is not None
    assert result.startswith("tests/test_callsite.py:")


def test_gives_up_when_no_project_frame_is_within_reach() -> None:
    assert _through_foreign_frames(conf.MAX_DEPTH + 5) is None


def test_the_walk_stops_when_the_stack_runs_out() -> None:
    """The other way to give up: no frames left, budget unspent.

    A thread's stack is four frames deep at the bottom, so the walk
    reaches ``None`` long before ``MAX_DEPTH``. Every other test here
    exercises the depth limit instead, and the two exits are separate
    lines of code.
    """
    seen: list[str | None] = []

    def probe() -> None:
        seen.append(resolve_callsite())

    # A root nothing is under, so no frame on that short stack is ours.
    with override_settings(SQLCOMMENT_PROJECT_ROOT="/nowhere-at-all"):
        callsite_module.clear_cache()
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
    assert seen == [None]


def test_project_root_setting_overrides_base_dir() -> None:
    with override_settings(SQLCOMMENT_PROJECT_ROOT="/srv/app"):
        callsite_module.clear_cache()
        assert relative_path("/srv/app/pkg/mod.py") == "pkg/mod.py"


def test_defaults_are_applied_when_settings_are_absent() -> None:
    assert conf.ENABLED is True
    assert conf.CALLSITE is True
    assert conf.MAX_LENGTH == 512
    assert conf.TAGS == {}
    assert conf.POSITION == "leading"
    assert conf.SEPARATOR == " "


def test_settings_override_defaults() -> None:
    with override_settings(SQLCOMMENT_MAX_LENGTH=64, SQLCOMMENT_ENABLED=False):
        assert conf.MAX_LENGTH == 64
        assert conf.ENABLED is False


def test_no_project_root_at_all_yields_no_callsite() -> None:
    # BASE_DIR is a startproject convention, not a required setting.
    # Reading a missing one on the query path would fail every query.
    #
    # `del` on the settings object sends no setting_changed, and an
    # empty override_settings() sends none either, so the conf snapshot
    # has to be dropped by hand — the same caveat conf.py records for
    # direct assignment. Without this the test passes or fails
    # depending on what ran before it.
    with override_settings():
        del settings.BASE_DIR
        setting_changed.send(sender=None, setting="BASE_DIR", enter=True)
        assert conf.PROJECT_ROOT == ""
        assert relative_path("/srv/app/pkg/mod.py") is None


def test_a_trailing_slash_in_the_root_is_tolerated() -> None:
    with override_settings(SQLCOMMENT_PROJECT_ROOT="/srv/app/"):
        assert relative_path("/srv/app/pkg/mod.py") == "pkg/mod.py"


def test_the_cache_is_dropped_when_the_root_moves() -> None:
    # No manual clear_cache() here on purpose: override_settings fires
    # setting_changed going in and coming out, and the cache listens.
    assert relative_path("/srv/app/pkg/mod.py") is None
    with override_settings(SQLCOMMENT_PROJECT_ROOT="/srv/app"):
        assert relative_path("/srv/app/pkg/mod.py") == "pkg/mod.py"
    assert relative_path("/srv/app/pkg/mod.py") is None


def test_the_cache_is_dropped_when_base_dir_moves() -> None:
    # BASE_DIR is the fallback root, so it has to invalidate the cache
    # too — and nothing else in the suite covers that branch.
    assert relative_path("/srv/other/pkg/mod.py") is None
    with override_settings(BASE_DIR="/srv/other"):
        assert relative_path("/srv/other/pkg/mod.py") == "pkg/mod.py"
    assert relative_path("/srv/other/pkg/mod.py") is None


def test_a_sibling_of_the_package_directory_is_not_the_package() -> None:
    # Matching the bare package directory with startswith would
    # swallow a sibling whose name merely begins with it.
    absolute = f"{conf.PROJECT_ROOT}/src/django_sqlcommenter_extras/x.py"
    assert relative_path(absolute) == "src/django_sqlcommenter_extras/x.py"


def test_only_a_whole_path_segment_counts_as_foreign() -> None:
    # A directory that merely contains the marker in its name is not a
    # virtualenv; substring matching would blank out every file for
    # anyone whose checkout happens to sit under one.
    absolute = f"{conf.PROJECT_ROOT}/site-packages-demo/x.py"
    assert relative_path(absolute) == "site-packages-demo/x.py"


def test_max_depth_clears_the_frames_django_actually_adds() -> None:
    # The test above derives its depth from the setting and so scales
    # with it; this pins a floor. 6-8 real Django frames sit between
    # the execute wrapper and the nearest project frame when the ORM is
    # driven directly — but a queryset evaluated inside a template is
    # far deeper: 28 frames with no nesting, plus 5 for every level of
    # {% include %}. The floor has to clear a realistically nested page,
    # or callsite silently reports "unknown" on server-rendered views.
    assert callsite_module.DEFAULT_MAX_DEPTH >= 80
    assert conf.MAX_DEPTH >= 80


def test_max_depth_is_configurable() -> None:
    with override_settings(SQLCOMMENT_MAX_DEPTH=3):
        assert _through_foreign_frames(10) is None
    with override_settings(SQLCOMMENT_MAX_DEPTH=200):
        assert _through_foreign_frames(10) is not None


def test_the_settings_snapshot_is_dropped_for_any_setting() -> None:
    # conf caches each value until a setting changes, and the receiver
    # cannot filter on the name: PROJECT_ROOT falls back to BASE_DIR,
    # which is not a SQLCOMMENT_* setting.
    original = conf.PROJECT_ROOT
    with override_settings(BASE_DIR="/srv/other"):
        assert conf.PROJECT_ROOT == "/srv/other"
    assert original == conf.PROJECT_ROOT
