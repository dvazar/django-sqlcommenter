# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## 0.1.0 - 2026-08-27

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
- `sql_scope()`, `sql_tags()`, and the lower-level `set_scope()` /
  `reset_scope()` / `current_scope()` around `SqlScope`: the public API
  third-party adapters build on. The lower-level pair is what an adapter
  driving a streaming or per-step loop needs, since a scope must not be
  held open across a `yield`.
- Settings `SQLCOMMENT_ENABLED`, `SQLCOMMENT_CALLSITE`, `SQLCOMMENT_MAX_LENGTH`,
  `SQLCOMMENT_MAX_DEPTH`, `SQLCOMMENT_TAGS`, `SQLCOMMENT_POSITION`,
  `SQLCOMMENT_SEPARATOR` and `SQLCOMMENT_PROJECT_ROOT`, validated by a Django
  system check.
- Tag values are treated as hostile input. Keys and values are URL-encoded
  with a safe set that excludes `*` and `'`, so a tag cannot close the
  comment early or break the `key='value'` grammar — the `route` and
  `action` tags come straight off the socket, so this is the normal case
  rather than the exception. See "Untrusted values in tags" in the README.
- Annotation never costs a query. Every path that builds a comment is
  guarded: a tag that cannot be rendered, a broken `get_request_tags()`
  override, a missing `BASE_DIR` or a malformed scope degrades the comment
  and leaves the statement to run, reporting once per process.

### Notes for readers coming from another sqlcommenter implementation

Two deliberate deviations from the sqlcommenter specification, both
documented in the README:

- the comment is **prepended** to the statement by default rather than
  appended, so it survives PostgreSQL's `track_activity_query_size`
  truncation. Set `SQLCOMMENT_POSITION = "trailing"` for spec behavior.
- the URL-encoding safe set is widened to `/:._-{}<>`, so a route pattern
  reads as `cart/<int:pk>/` rather than `cart/%3Cint:pk%3E/`. Values still
  round-trip through `unquote` unchanged.
