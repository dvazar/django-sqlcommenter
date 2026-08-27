# django-sqlcommenter

[![CI](https://github.com/dvazar/django-sqlcommenter/actions/workflows/ci.yml/badge.svg)](https://github.com/dvazar/django-sqlcommenter/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/dvazar/django-sqlcommenter/graph/badge.svg)](https://codecov.io/gh/dvazar/django-sqlcommenter)
[![PyPI](https://img.shields.io/pypi/v/django-sqlcommenter.svg)](https://pypi.org/project/django-sqlcommenter/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-sqlcommenter.svg)](https://pypi.org/project/django-sqlcommenter/)
[![Django versions](https://img.shields.io/pypi/frameworkversions/django/django-sqlcommenter.svg?label=django)](https://pypi.org/project/django-sqlcommenter/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Annotate every Django SQL query with the code and request that issued it.

## What it does

Install it, and the SQL your application sends to the database arrives
carrying a sqlcommenter comment:

```sql
/*action='GET',callsite='shop/services/pricing.py:118',controller='shop:cart',route='cart/<int:pk>/'*/ SELECT "item"."id", ...
```

The tags are built once per request — not once per query — and spliced in
by a per-connection `execute_wrapper`, so nothing in your code changes. The
one exception is `callsite`, which names the line that issued *this*
statement and is therefore resolved per query; see
[Performance](#performance) for what that costs and how to turn it off.

## Why

`pg_stat_activity`, the slow-query log and pgBadger all show you SQL with
no link back to the code that issued it. You find the statement that is
eating the database and then spend an afternoon guessing which view, which
serializer, which management command produced it.

An APM has the link, but only inside the APM, only for the traces it
sampled, and usually only for HTTP requests — the nightly `manage.py`
job that locks a table for eleven minutes is exactly the thing it does
not see. A comment in the statement is visible to every tool that can
read SQL text, including the ones your DBA already has open:

```sql
SELECT query FROM pg_stat_activity WHERE state = 'active';
```

## Install

```bash
pip install django-sqlcommenter
```

```python
INSTALLED_APPS = [
    ...,
    "django_sqlcommenter",
]

MIDDLEWARE = [
    # First, so that queries made by session and auth middleware are
    # annotated too.
    "django_sqlcommenter.middleware.SqlCommentMiddleware",
    ...,
]
```

That is the whole setup. The app config installs the query wrapper and
patches `BaseCommand.execute`, so management commands are annotated
without the middleware being involved at all.

## Settings

| Setting | Type | Default | Purpose |
| --- | --- | --- | --- |
| `SQLCOMMENT_ENABLED` | `bool` | `True` | Master switch. Read at startup and again on every query. |
| `SQLCOMMENT_CALLSITE` | `bool` | `True` | Resolve the calling project source line and emit it as `callsite`. |
| `SQLCOMMENT_MAX_LENGTH` | `int` | `512` | Comment budget in bytes. Over budget, the lowest-ranked tag is dropped first. |
| `SQLCOMMENT_TAGS` | `Mapping[str, str]` | `{}` | Tags added to every query in the process, regardless of scope. |
| `SQLCOMMENT_POSITION` | `str` | `"leading"` | `"leading"` or `"trailing"` — which side of the statement the comment goes on. |
| `SQLCOMMENT_SEPARATOR` | `str` | `" "` | What goes between the comment and the statement. |
| `SQLCOMMENT_PROJECT_ROOT` | `str` or path | `BASE_DIR`, then `""` | Root that `callsite` paths are reported relative to. |
| `SQLCOMMENT_MAX_DEPTH` | `int` | `100` | How many stack frames the `callsite` walk may climb before giving up. |

### Turning it off

`SQLCOMMENT_ENABLED` is read per query, so
`override_settings(SQLCOMMENT_ENABLED=False)` works inside a consumer's
test that asserts on generated SQL text: the wrapper stays installed and
goes inert.

Turning it off *before startup* is a different thing. Nothing is
installed at all, and flipping the setting back on at runtime does
nothing until the process restarts.

### The separator

Words do not convey this one, so:

```python
SQLCOMMENT_SEPARATOR = "\n"
```

```sql
/*action='GET',route='cart/<int:pk>/'*/
SELECT "item"."id", ...
```

In `pg_stat_activity`, in slow-query logs and in anything that echoes SQL
back at a human, a comment and a statement on separate lines are far
easier to read than one long line — and it costs the same one byte.

The separator is not charged against `SQLCOMMENT_MAX_LENGTH`, which
budgets the comment alone.

### They are validated by a system check

These settings are read inside the execute wrapper, on the path of every
query, and by then it is too late to complain. A bad value either
silently disables annotation for the whole process or reaches the
database as broken SQL. So they are checked at startup instead, and a
typo shows up in `manage.py check`:

- `SQLCOMMENT_MAX_LENGTH` must be a positive `int` — and not a `bool`,
  because `True` budgets one byte and drops every tag without a word
  (`django_sqlcommenter.E001`);
- `SQLCOMMENT_TAGS` must be a mapping (`E002`);
- `SQLCOMMENT_SEPARATOR` must be a string (`E003`), and must contain
  neither `*/`, which would close the comment a second time and make every
  statement a syntax error, nor `--`, which opens a line comment and with
  the default leading position would comment the statement out entirely
  (`E007`);
- `SQLCOMMENT_MAX_DEPTH` must be a positive `int`, and not a `bool`, for
  the same reason (`E008`);
- `SQLCOMMENT_POSITION` must be `"leading"` or `"trailing"` (`E005`);
- `SQLCOMMENT_PROJECT_ROOT` must be a string or a readable path (`E004`),
  and must be an absolute path below the filesystem root — a relative root
  matches no source file and `/` makes the whole filesystem project code
  (`E006`). Neither it nor the `BASE_DIR` it falls back to being readable
  is a separate failure with its own id (`E009`), so silencing one does not
  silence the other;
- when `callsite` is on and there is no project root at all, you get a
  warning saying no `callsite` tag will ever be emitted (`W001`).

Note that `None` is not "unset" for any of these except
`SQLCOMMENT_PROJECT_ROOT`. Settings are read with
`getattr(settings, name, default)`, so an explicit `None` beats the
default and travels all the way to the query: `SQLCOMMENT_SEPARATOR =
None` renders as the literal `"None"`.

`SQLCOMMENT_ENABLED` and `SQLCOMMENT_CALLSITE` are deliberately
unchecked — `bool()` cannot raise on anything.

## It isn't working

No comment appears. Almost always one of two things:

1. **No scope is open.** Outside an HTTP request, a management command or
   an explicit `sql_scope(...)`, there is nothing to describe and nothing
   is emitted. Confirm the wiring by running a query inside
   `with sql_scope(check="1"):` and looking at what the database received.
2. **Only `callsite` is missing.** Then there is no usable project root:
   `SQLCOMMENT_PROJECT_ROOT` is unset and so is `BASE_DIR`, or the root
   does not actually contain your code.

`manage.py check` reports the second case, and every other bad setting,
by name — run it first.

## Tags you get

| Tag | Set by | Value |
| --- | --- | --- |
| `route` | `SqlCommentMiddleware` | The raw `request.path` at first, refined to the matched URL pattern (`ResolverMatch.route`) once the view resolves. |
| `action` | `SqlCommentMiddleware` | The HTTP method. |
| `controller` | `SqlCommentMiddleware` | `ResolverMatch.view_name` — the namespaced view name, e.g. `shop:cart`. |
| `command` | The `BaseCommand.execute` patch | The command's module name, i.e. what you type on the command line. |
| `callsite` | The execute wrapper | `path/to/file.py:LINE` for the nearest project frame, relative to the project root. `unknown` when no project frame is found within `SQLCOMMENT_MAX_DEPTH` frames. |

`route` and `controller` are two different questions: with an API
framework that mounts one dispatching view over many URLs, `controller`
is the same for all of them and `route` is what tells them apart.

`callsite` is found by walking the stack for the nearest frame belonging
to your project. That walk stops after `SQLCOMMENT_MAX_DEPTH` frames and
reports `unknown`, which means "we gave up", not "a library issued this".
The default of 100 is generous on purpose: a queryset evaluated directly
sits 6-8 frames from the wrapper, but one evaluated inside a template sits
28 frames away with no nesting at all, and 5 further frames for every
level of `{% include %}`. If a server-rendered page reports `unknown`,
raise the setting — it costs nothing on queries that resolve, because a
walk that finds project code stops there whatever the ceiling is. Only a
query with no project frame above it pays the full walk, at 10.4 µs for
the default 100 frames.

## Adding your own tags

Three layers, in increasing order of locality — later ones win:

```python
# Deployment-wide, in settings:
SQLCOMMENT_TAGS = {"env": "prod"}
```

```python
# Per request, in a middleware subclass:
from django_sqlcommenter import SqlCommentMiddleware


class MyMiddleware(SqlCommentMiddleware):
    def get_request_tags(self, request):
        return {"tenant": request.tenant_id}
```

```python
# Per code region:
from django_sqlcommenter import sql_tags

with sql_tags(order=str(order.pk)):
    ...
```

`get_request_tags` runs once per request, before the view. If it raises —
or returns something that is not a mapping — the failure costs its tags,
not the request: the middleware is documented to mount first, so failing
there would take the site down before anything else ran. The failure is
logged (see [Logging](#logging)).

Values are coerced with `str()`, so `sql_scope(order=order.pk)` with an
integer works rather than failing every query in the scope. Prefer to
pass the string yourself: `str()` on a model instance runs your
`__str__`, which can hit the ORM.

## Untrusted values in tags

**Put hostile data in a tag.** That is what tags are for: a tenant slug,
a search term, an order reference, a header. The `route` and `action`
tags this package sets itself come straight off the socket — a client
chooses its own request path and its own method token — so untrusted
input is the normal case rather than the exception.

Every key and every value is URL-encoded before it reaches the comment,
and the safe set excludes `*` and `'`. Closing the comment early is
therefore not filtered, it is **not expressible**: no input produces a
`*`, so no input produces a `*/`, and none produces the quote that would
break the `key='value'` grammar. The whole body of a comment is drawn
from `A-Za-z0-9_.-~/:{}<>%` plus the grammar's own quotes and commas.

```python
with sql_scope(route="*/ DROP TABLE auth_user; --"):
    ContentType.objects.count()
```

```sql
/*route='%2A/%20DROP%20TABLE%20auth_user%3B%20--'*/ SELECT COUNT(*) ...
```

`tests/test_injection.py` is the enforcement. It runs a corpus of
payloads — comment terminators, quote escapes, statement separators,
placeholders in all four binding styles, null bytes, lone surrogates,
astral characters — through every entry point that can set a tag, and
against a real driver, asserting each time that the statement came
through byte-identical and the comment matches that grammar. It opens
with a control that runs one of the payloads through a *naive*
implementation and watches SQLite drop the table without raising.

Two things are outside that boundary, both by design:

- **`SQLCOMMENT_SEPARATOR` is spliced in verbatim**, because it exists to
  be a space or a newline and encoding it would defeat it. It is a
  setting, so anyone who can set it can already write raw SQL or swap
  the database backend — it is not a hole in the boundary, it is outside
  it. The system checks reject the two values that would break the
  comment rather than merely abuse it.
- **A long client-chosen `route` costs you your other tags.** `route`
  outranks everything, so a 40 KB URL fills the budget, evicts
  `callsite` and `controller`, and is then dropped itself for not
  fitting alone — leaving a bare statement. The query is served either
  way. Raise `SQLCOMMENT_MAX_LENGTH` if you would rather keep the tags,
  or lower it if you would rather cap the bytes.

## Annotating background work

Anything that is not an HTTP request and not a management command needs
an entry point of its own. That entry point is `sql_scope`:

```python
import functools

from django_sqlcommenter import sql_scope


def annotate_task(func):
    """Wrap a task callable so its queries name the task."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with sql_scope(task=func.__name__, queue="default"):
            return func(*args, **kwargs)

    return wrapper
```

Register that with whatever hook your queue provides — a task base class,
a signal, a worker middleware — and every query the task makes is
annotated.

No ready-made adapters ship with this package, on purpose. Every task
queue worth using already lets you register your own middleware, and a
scope is three lines; an adapter here would be a thin wrapper that has to
track someone else's release cycle.

`sql_scope` replaces any enclosing scope rather than merging into it: a
task picked up by a worker belongs to the task, not to the command that
happened to host the worker process. Use `sql_tags` when you want to
merge.

## Deviations from the sqlcommenter spec

| Deviation | Spec | Here |
| --- | --- | --- |
| Position | The comment is appended to the statement. | It is prepended by default, so it survives PostgreSQL's `track_activity_query_size` truncation in `pg_stat_activity` (and MySQL's 1024-byte log truncation). Set `SQLCOMMENT_POSITION = "trailing"` for spec behavior. |
| Safe set | URL-encoding leaves only `/` un-encoded. | The safe set is widened to `/:._-{}<>`. |
| Separator | Always a single space. | Configurable via `SQLCOMMENT_SEPARATOR`; the spec has no such knob. |

The widened safe set is about the tag people read most. `<>` is Django's
own `path()` syntax, so under the spec the `route` tag would be the least
readable thing in the comment:

Under the spec:

```sql
/*route='cart/%3Cint:pk%3E/'*/ SELECT ...
```

Here:

```sql
/*route='cart/<int:pk>/'*/ SELECT ...
```

Standard parsers are unaffected: `unquote` is a no-op on those literals,
so a value round-trips unchanged. The safe set still excludes `*` and
`'`, which makes it impossible for a serialized tag to close the comment
early or break the `key='value'` grammar.

## Performance

Measured on CPython 3.14 on an Apple-silicon laptop, timing
`QueryAnnotator.__call__` against a no-op executor. Take the absolute
numbers as an order of magnitude and the ratios as the point — and note
that the cost of an annotated query is not a single number, because it
scales with how many tags you put in the comment:

- a query made **outside any scope** costs **0.12 µs** over a bare
  execute — that is the whole cost of having the package installed on
  paths it does not describe;
- an **annotated** query costs about **0.80 µs per tag**, measured as a
  clean slope from one tag to four, plus about **1.4 µs** for
  `callsite` — which is the frame walk *and* the extra tag the walk
  produces, roughly 0.5 µs and 0.9 µs of it respectively. So the
  four-tag comment this README opens with costs **4.5 µs**, or **3.2
  µs** with `callsite` off;
- the **middleware** costs **0.67 µs per request**, not per query. A
  request making 200 queries pays it once;
- the **frame walk** itself is hand-rolled rather than delegated to
  `traceback`. Against a stack of 20 non-project frames — the worst
  case, where the walk cannot stop early and has to traverse all of
  them — it costs **2.3 µs**, against **67 µs** for
  `traceback.extract_stack()` on the same stack, which materializes
  every frame and reads source lines through `linecache`. On the
  realistic path, 6-8 Django frames between the wrapper and project
  code, it is **1.0 µs**. Even in the best case, where the caller's own
  frame is project code, it still costs **0.47 µs**: it does
  `sys._getframe` and one loop iteration regardless. (The walk memoizes
  per filename, and that lookup costs **0.04 µs** — but that is one step
  inside the walk, not the walk.)

If `callsite` is not worth 1.5 µs on some path, turn it off — globally:

```python
SQLCOMMENT_CALLSITE = False
```

or just for the region that issues tens of thousands of queries:

```python
with sql_scope(task="rebuild-index", with_callsite=False):
    ...
```

Transaction bookkeeping (`BEGIN`, `COMMIT`, `SAVEPOINT`, `SET ...`, and
friends) is never annotated: it would cost bytes and tell nobody
anything.

## Logging

Annotating is telemetry, so every path that builds a tag or a comment is
guarded — a failure there costs the comment, never the query. But a guard
that says nothing turns a bug into a mystery, so each distinct failure is
logged **once per process**, with a traceback, at `ERROR`, to the logger
named `django_sqlcommenter`:

```python
LOGGING = {
    "version": 1,
    "loggers": {
        "django_sqlcommenter": {
            "handlers": ["console"],
            "level": "ERROR",
        },
    },
}
```

Once per *distinct message*, so a failure in one place does not silence a
different failure somewhere else.

## Limitations

- **Queries issued before `AppConfig.ready()`** are not annotated. The
  wrapper does not exist yet.
- **Code outside an entry point is not annotated** until a scope is
  opened. No scope means no comment — deliberately, since tags with no
  unit of work behind them describe nothing.
- **`executemany` gets one comment for the whole batch**, not one per
  row. The wrapper sees one statement.
- **Whether `connection.queries` and django-debug-toolbar show the comment
  depends on the backend.** Django's debug cursor does not log the statement
  it was handed: it calls `last_executed_query()`, and what that returns
  differs. SQLite reports the SQL Django passed down, so the comment is
  invisible there. PostgreSQL, MySQL and Oracle report what the driver
  actually sent, so the comment **is** visible — a test asserting on
  `connection.queries[-1]["sql"]` can begin failing on those backends once
  the package is installed, and a `callsite` tag makes the text move whenever
  the code moves. `assertNumQueries` is unaffected either way, since it only
  counts. `executemany` is the exception: the debug cursor skips
  `last_executed_query` for a batch, so a batch never shows its comment on any
  backend.
- **`SQLCOMMENT_POSITION = "trailing"` is unsafe on Oracle.** Django's
  Oracle backend strips a trailing `;` or `/` from every statement, and a
  trailing comment ends in `*/` — so Oracle receives `…*`, an unterminated
  comment. The default `leading` position is unaffected, on Oracle and
  everywhere else.
- **A lazily rendered `TemplateResponse` resolves no `callsite`.** A
  class-based view returning `TemplateResponse` renders after the view has
  returned, so by the time the template evaluates a queryset there is no
  project frame left on the stack at all — `callsite` comes out `unknown`
  no matter how high `SQLCOMMENT_MAX_DEPTH` goes. The other tags are
  unaffected: the scope is still open, so `route`, `action` and
  `controller` are all present. A function view that evaluates its
  querysets before returning is unaffected.
- **A scope does not cross into a thread you start yourself.** A bare
  `threading.Thread`, or a direct `ThreadPoolExecutor.submit`, starts an
  empty context, and those queries go unannotated silently. Open a scope
  inside the thread. `asyncio.to_thread` and asgiref's `sync_to_async`
  do copy the context, so those carry the scope.
- **On a 404, a `CommonMiddleware` redirect, or any upstream
  short-circuit, `process_view` never runs**, so the raw `request.path`
  ships as `route` instead of the URL pattern. A scan of nonexistent URLs
  then puts a distinct `route` value in `pg_stat_activity` per probed
  path.
- **A long client-chosen path evicts the other tags**, and past a point
  the comment with them. See [Untrusted values in
  tags](#untrusted-values-in-tags).

## Attribution

The comment format comes from the sqlcommenter specification
(https://google.github.io/sqlcommenter/spec/), which Google archived in 2025.
This code was written from the specification and is not derived from Google's
implementation. The package is distributed under the same  Apache-2.0 license
as the original.
