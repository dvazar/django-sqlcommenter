# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## 0.1.0 - unreleased

### Added

- `SqlCommentMiddleware`: one annotation scope per HTTP request, streaming
  responses included.
- A `BaseCommand.execute` patch: every management command annotates its SQL,
  with no per-command opt-in.
- Automatic tags: `route` and `controller` for the matched URL pattern and
  view name, `action` for the HTTP method, and `command` for the management
  command being run.
- A `callsite` tag naming the project source line that issued the query, as
  `path/to/file.py:LINE`, resolved by a hand-rolled frame walk and reported
  relative to `SQLCOMMENT_PROJECT_ROOT` (or `BASE_DIR`).
- `sql_scope()` and `sql_tags()`: the public API third-party adapters build on.
- Settings `SQLCOMMENT_ENABLED`, `SQLCOMMENT_CALLSITE`, `SQLCOMMENT_MAX_LENGTH`,
  `SQLCOMMENT_TAGS`, `SQLCOMMENT_POSITION`, `SQLCOMMENT_SEPARATOR` and
  `SQLCOMMENT_PROJECT_ROOT`, validated by a Django system check.

### Notes for readers coming from another sqlcommenter implementation

Two deliberate deviations from the sqlcommenter specification, both
documented in the README:

- the comment is **prepended** to the statement by default rather than
  appended, so it survives PostgreSQL's `track_activity_query_size`
  truncation. Set `SQLCOMMENT_POSITION = "trailing"` for spec behavior.
- the URL-encoding safe set is widened to `/:._-{}<>`, so a route pattern
  reads as `cart/<int:pk>/` rather than `cart/%3Cint:pk%3E/`. Values still
  round-trip through `unquote` unchanged.
