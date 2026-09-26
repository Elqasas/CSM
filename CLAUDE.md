# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands assume the checked-in virtualenv. On Windows the interpreter is
`.venv/Scripts/python`; substitute `.venv/bin/python` elsewhere.

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py seed_demo --reset    # 60 accounts, one user per access level
.venv/Scripts/python manage.py runserver
```

`seed_demo` users sign in at `/admin/` with password `demo-password-123`.

### Tests and lint

```bash
.venv/Scripts/python -m pytest                              # full suite, 437 tests
.venv/Scripts/python -m pytest tests/test_health_engine.py  # one file
.venv/Scripts/python -m pytest tests/test_access.py::test_only_the_access_package_interprets_role_or_seniority
.venv/Scripts/python -m pytest -k "csat and stale"          # by name
.venv/Scripts/python -m ruff check csm config tests
.venv/Scripts/python -m djlint templates                    # template linting
.venv/Scripts/python -m pytest --cov=csm --cov-report=term  # 94%
.venv/Scripts/python manage.py makemigrations --check --dry-run
```

No database server is needed: tests run on in-memory SQLite, dev on a local
SQLite file. PostgreSQL 16 is the production target, reached by setting
`DATABASE_URL`; `docker-compose.yml` brings up Postgres and MailHog.

**Before deploying, run the suite against PostgreSQL**, not only SQLite:

```bash
DATABASE_URL=postgres://user:pass@host:5432/csm_test   pytest --ds=config.settings.test_postgres
```

The schema leans on stored generated columns, partial unique indexes and JSONB.
Both engines support all three, which is why a divergence would be silent.
Verified clean on 16.15 — including that the partial indexes *enforce*, not
merely exist. `djlint` lints the
templates; H021 and H023 are switched off in `pyproject.toml`, with the reason
recorded there.

`DeprecationWarning` is configured as an error in `pyproject.toml`. A new
warning fails the suite rather than accumulating.

### Scheduled jobs

All three are idempotent and safe to re-run:

```bash
manage.py recalculate_health      # daily half of the dual trigger
manage.py generate_tasks          # renewal, quarterly and rollover reminders
manage.py send_due_notifications  # 07:00 digests, then drain the email outbox
manage.py send_task_reminders     # every 5 min: the hour/half-hour before a timed task
```

**Creating a task notifies nobody.** Only a health alert is announced at the
moment it is raised, because it is the one that means stop what you are doing.
Everything else reaches people through the 07:00 digest - which looks a week
ahead and groups Overdue / Today / Next 7 days, in-app as well as by email - and,
if the task carries a clock time, through its own reminders at T-60 and T-30.
The generators run at 01:00, so notifying at creation put the day's work in a
pile nobody was awake for and said nothing on the morning it was due.

A reminder later than `TASK_REMINDER_GRACE_MINUTES` past its mark is dropped
rather than sent: "in one hour" delivered at twenty past is worse than silence,
and the nearer reminder is still coming. Each offset fires once, guaranteed by
`Notification.dedupe_key` rather than by the worker running on the minute.

## Architecture

Twelve Django apps under `csm/`. The shape that matters is not the app list —
it is ten invariants that cut across apps, each of which is easy to violate by
writing ordinary-looking code.

### The scoring engine is pure

`health/engine.py` takes a frozen `ScoringInput` and an `as_of` date and returns
a frozen `ScoreResult`. It touches no database, calls no clock, and has no side
effects. This is what makes the whole scoring matrix testable as parametrized
unit tests with no fixtures, and every threshold is asserted on both sides of
its boundary.

The split is strict and must stay that way:

- `health/inputs.py` — all reading, builds the `ScoringInput`
- `health/engine.py` — pure computation
- `health/services.py` — all writing (cached fields, history rows, overrides, events)
- `health/config.py` — every threshold, weight and band

`ScoringInput` deliberately omits the account's Class, Size, country and name.
The engine cannot let them influence a score because it never receives them.
Do not add a field to `ScoringInput` to make a calculation convenient.

Thresholds live in `health/config.py` rather than a database table because
changing one retroactively changes the meaning of every stored history row.
**Bump `ENGINE_VERSION` whenever a number there changes** — each history row
records the version that produced it. It is at 1.1.0: 1.0.0 predates the
red-factor cap.

Two structural caps sit in `engine.band()` and neither touches a weight, so old
history stays interpretable. The **omission cap** forces Watch when a factor is
missing and the score still reads 80. The **red-factor cap** forces At Risk when
half or more of the scored factors are red — a share, not a count, because two
red of four is half the picture and two red of five is not. At Risk wins when
both apply.

### One clock

`core/clock.py` is the only place that converts between UTC and the business
timezone. All storage stays UTC. Never call `date.today()` or
`timezone.now().date()` anywhere else: every health factor is a day count, so
the score flips at midnight in Africa/Cairo, and Egypt's DST transitions mean a
fixed offset is wrong for part of the year.

Pass `as_of` explicitly into anything scoring-related rather than letting it
read the clock itself.

### Role interpretation has exactly one home

`core/access/` is the only package that reads `role` or `is_senior`.
`tests/test_access.py::test_only_the_access_package_interprets_role_or_seniority`
walks the source with `ast` and fails if anything else does.

Construct an `Ability(user)` once per request and pass it down. Views, templates
and presenters ask that object; they never inspect the user's role.

- **Row scope** comes from `Ability.accounts()`. Everything that lists,
  searches, filters or aggregates accounts must start there — global search
  especially, or the search bar quietly defeats the scoping.
- **Capabilities** come from `Ability.can()`. An unknown capability raises
  rather than returning False.
- **Field scope** is `SYSTEM_ACCOUNT_FIELDS` in `core/access/abilities.py` —
  fields nobody edits through a form. An ACM holds no `account.edit`
  capability at all, so `editable_account_fields()` returns nothing for them
  before this list is even consulted; the account record is read-only to the
  commercial side entirely.
- **The presenter boundary** is `core/access/presenters.py`. When a user may not
  see health information the presenter *raises* `PermissionDenied` rather than
  returning None — a silent None reads as "no score yet" and would reach a page
  an ACM can see.
- **Activity visibility** is `Ability.visible_activities()`. An ACM sees only
  what a future Outlook/Teams sync would log — `source` values other than
  `MANUAL` — never a CSM's own record of a call or meeting, which is exactly
  the log the health engine reads. Since no integration writes those sources
  yet, an ACM's activity feed is empty until one does.

The hard rule behind all of it: an ACM owns the commercial renewal and must
never see any health signal, including in aggregated dashboard cards.

**Five access levels, from two fields.** `role` gives CSM, ACM, Manager and
Admin; `is_senior` promotes a CSM to CSM Senior, and a database constraint keeps
it meaningful only on a CSM. Admin is a *role*, not a flag on another role: it
holds `frozenset(cap.ALL)` - so a capability added tomorrow is the
administrator's by default - it is the only source of `user.manage`, it sees
every account, and it is the CSM of record on none (`AccountForm` offers only
CSM-role users as owners, plus whoever already holds the account, so editing an
account cannot silently reassign it). `is_superuser` now means only "may reach
Django's own admin site" and is read through `Ability.has_django_admin`.

### Notification routing cannot see roles at all

`notifications/routing.py` imports nothing from `core/access` and
`resolve_recipients` reads five scalar fields, none of which describes anyone's
seniority. That is how "a senior CSM can act on any account but is notified only
on their own" holds structurally rather than by convention. A test enforces the
non-import. The senior reassignment rule is expressed as "the creator is not
this account's CSM or ACM", which needs no seniority flag.

`notifications/dispatcher.py` takes recipients from `task.assignments` and never
widens them — no copying in a senior, no copying in a manager.

### Three business rules are database invariants

Partial unique indexes, not application checks:

- one open health alert per account (`tasks_one_open_health_alert_per_account`)
- one active override per account (`health_one_active_override_per_account`)
- the generated-task idempotency key (`Task.auto_key`, unique)

A check-then-insert leaves a window where the daily job and an event-driven
recalculation both find nothing and both insert. Handle `IntegrityError`; do not
replace a constraint with a query.

`auto_key` contains the business date but no timestamp, so re-running a
generator creates nothing while moving a contract date correctly creates a new
obligation. Generated tasks are **cancelled, never hard-deleted** — a hard
delete frees the key and the task reappears the next morning.

**Every task is editable, generated ones included, and the edit sticks.** Do not
add a guard against editing them: the key is built from the account and the date
the obligation derives from, never from the title or due date, and the generator
creates through `create_task`, which the unique index turns into a no-op when the
key exists. It never updates a task it finds. What replaces a generated task is
the underlying date moving — a new key, a new task, the stale one cancelled —
which is the obligation changing rather than an edit being lost.
`tests/test_engagement_views.py` asserts this by running the real generator after
a hand edit.

### The dual trigger and the event bus

Health recalculates from two directions: model-level hooks in `health/signals.py`
(not view handlers, which get bypassed by import scripts and shell sessions) and
the daily `recalculate_health` command.

**Completing a task, meeting or call writes the Activity.** That is the only
way a log entry is created now - the separate "log an activity" form is gone,
and something that already happened is added with its status set to Completed,
which closes it through `complete_task` and writes the record. The subscriber is
`activities/subscribers.py`, on the `TaskCompleted` event, so tasks stay ignorant
of activities.

**The category decides what it counts as, and this is the part to be careful
with.** Every `ActivityType` except `NOTE` counts as client contact, and a
`MEETING` additionally drives Meeting Attendance. So a client-facing item logs as
its real kind (Meeting, Call, else Other) and moves the score; an internal or
partner-facing one logs as a `NOTE`, which counts for neither contact factor.
Without that rule, ticking off "update the renewal spreadsheet" would raise an
account's health score as though somebody had spoken to the client.
`Activity.task` is a one-to-one, which is what makes the write idempotent under
a replayed event.

**Meetings and calls are task kinds, not activities, and will get their own
model rather than joining either.** `health/inputs.py` reads `Activity` for
three of the five scoring factors, so planned work in that table would raise a
health score because somebody opened a calendar. The axis is planned versus
happened: a Task is an obligation, an Activity is an immutable record of
something that occurred. When the Teams work starts, a `Meeting` model goes in
its own app and a Task may point at it; the integration writes an `Activity`
when the meeting actually happens. Full reasoning, including what was ruled out,
is in README.md.

`core/events.py` is an in-process bus. Events publish inside the transaction
that produced them and deliver on `transaction.on_commit`, so a subscriber never
observes rolled-back state, and one request logging three activities scores the
account once. A failing subscriber is logged and skipped rather than allowed to
break its peers. This is the seam the deferred Jira/Teams/Outlook work will
subscribe to.

### The power/interest axis mapping has one home

`HIGH_INFLUENCE` and `HIGH_INTEREST` in `contacts/models.py` are the only
definition of which levels count as the high half. The `quadrant` property
(Python) and the queryset ordering (SQL) both read them, because the mapping is
expressible twice and *had already drifted*: `quadrant` treated Medium as high
influence while the attention query demanded a strict `Level.HIGH`, with no test
over either.

The mapping is asymmetric on purpose — Medium counts as **high influence, low
interest** — and `tests/test_contacts_grid.py` asserts all nine cells plus
agreement between the property and the queryset.

`influence` and `interest` are **nullable with no default**. Unassessed is a
distinct state from Medium; `quadrant` returns `None` for it, and those contacts
collect in `needs_triage()` rather than being plotted. Do not reintroduce a
default — defaulting to Medium put every contact in Manage Closely at import.

`by_attention()` ranks rather than filters, and is portfolio-wide: any call site
spanning accounts must derive from `Ability.accounts()`, not `Contact.objects`.

### Views go through the access mixins

`core/views.py` holds `AbilityMixin`, `CapabilityRequiredMixin` and
`AccountScopedMixin`. A view never builds its own `Ability` and never looks an
account up itself:

- `AccountScopedMixin` resolves `self.account` from `ability.accounts()`, so a
  request for a colleague's account is a **404, not a 403** — a 403 confirms the
  account exists.
- Templates test `{% if "health.view" in ability.capabilities %}`. Django
  templates cannot pass arguments to a method, so membership is the idiom; no
  template reads a role.
- Accounts reach templates as `AccountPresenter`, not as models. Add a field to
  `SAFE_FIELDS` if it carries no health signal; add a gated property if it does.

New forms with a `URLField` must pass `assume_scheme="https"` (see
`accounts/forms.py`). Django 6 changes the default and the warning is an error
here.

Multi-line template commentary uses `{% comment %}`. Django's `{# #}` is
single-line only and a multi-line one renders onto the page; a test enforces it.

### The importer previews before it writes

`importer/parsing.py` is forgiving about header *shape* (case, spacing,
punctuation, aliases) and strict about *meaning*: an unknown country or an
unrecognised CSM email is an error on the row, never a guess.

Rows stage into `import_rows` first. `services.apply()` writes the staged rows
and re-reads no file, so what was previewed is what is written. Keep it that
way — re-parsing at apply time reintroduces the gap the preview exists to close.

A rule lives in the parser, not the caller:

- **CSM/ACM never create a user.** Each role has a single email column; an
  unknown address is an error on the row. Nothing unmatched is auto-created —
  that would make row scope depend on whatever a sales export contained.

**The accounts sheet is the account form, in the form's order.** `parsing.COLUMNS`
follows `accounts.forms.CANDIDATE_FIELDS` field for field (the logo excepted - no
cell holds an image), and `account_columns()` inserts one column per active custom
field before Description, as the form does. A test asserts the two lists match, so
a field added to the form fails until the sheet carries it too. Builders take the
custom fields from the caller, like `people`, and read no database themselves.

**An update writes only the columns the sheet has.** A missing column leaves the
field alone; a present but empty one clears it. Without that, a sheet exported
before a column existed would blank it on every matched account.

Focal points are no longer part of the accounts sheet. `Contact.is_focal_point`
is set from the contacts screen (or the contacts import's own `focal_rank`
column), never guessed at from an account-level name column.

Templates and parsers share their column tables (`parsing.COLUMNS`,
`contact_parsing.CONTACT_COLUMNS`), and tests round-trip every generated
template through its own parser. Guidance never sits under a header row: in
xlsx it lives on a second worksheet, in CSV it is `#` comment lines above the
header. Under the headers it would import as an account named after the help
text.

**Reading is chosen by filename**, so any new call into `parse()` /
`parse_contacts()` must pass `filename=`. Without it every upload goes down the
xlsx path and a CSV fails as "not a zip file" — a bug that got past the unit
tests once because they passed the filename explicitly while the view did not.

`ImportBatch.kind` picks the handler (`services` vs `contact_services`). It is
asked on the upload form rather than sniffed from headers: both files carry a
client-code column, and guessing wrong writes the wrong table. A contacts file
never creates an account.

### Production refuses to boot misconfigured

`config/settings/prod.py` raises rather than degrading: a non-PostgreSQL
`DATABASE_URL`, the development `SECRET_KEY` default inherited from `base.py`, or
a key under 50 characters each stop startup. `check --deploy` cannot catch the
key one, because a key *is* set — it is simply the one published in this
repository, which would make every session cookie and signed value forgeable.
Failing loudly is the only version anybody notices.

### Engagement entry moves the score through the model hook

`engagement/views.py` writes tickets, CSAT ratings and adoption events. No view
there calls the scoring engine: the `post_save` hooks in `health/signals.py` do,
on commit. Keep it that way — a view that rescored directly would be bypassed by
the importer and by the admin.

The four capabilities are separate on purpose. An ACM holds none of them —
tickets, CSAT and adoption all feed the health picture they are kept clear of,
and `engagement.edit` is CSM-only work besides.

**Testing anything that rescores** needs two things: `django_capture_on_commit_callbacks`,
since a test transaction never commits, and a clear of `health.signals._pending`,
whose module-level guard deduplicates one recalculation per transaction and
otherwise leaks a stale entry from fixture setup. `tests/conftest.py` resets it
between tests.

### Dashboard cards declare, they do not check

`dashboard/cards.py` holds the registry. A card names a capability and
`visible_cards()` drops it when the user lacks one — that is the whole of "an ACM
sees every card except Health Status". Do not add a role check to a card.

Where a card must narrow rather than disappear (the Overdue Tasks card, which an
ACM sees filtered to renewals), the condition is written as *cannot see health*,
not *is an ACM*, so it survives the roles being rearranged.

Card builders receive an already-scoped queryset from the view and must never
call `Account.objects`: row scope and the shared filter are applied once, before
any card runs.

A filter offered on more than one page lives in `accounts/filters.py`, not in
each view. The renewal quarter is on both the account list and analytics, and
written twice it drifts - one page reading `Q4` and the other `2026-4`, or one
treating an unparseable value as "everything" and the other as "nothing" (it is
everything: a filter the page cannot read must leave the page as it was, never
make the portfolio look empty). Choice lists come from `ability.accounts()`
*before* the filter, or selecting a value collapses the control to that value
with no way back. The CSM/ACM selects are scoped the same way: a list of every
CSM in the company would name colleagues whose portfolios the reader cannot
see.

**Before writing any chart, load the `dataviz` skill and run its palette
validator.** It is not decorative: it is what established that traffic-light red
against green measures 5.8 apart under deuteranopia in light mode and 4.5 in
dark (floor is 8), which is why the health donut labels every segment and ships
a table. It is also what rejected a teal second series that measured 6.3 tritan
on the dark surface in favour of the amber that measures 29. Dark mode has its
own lightness band (L 0.48-0.67), so its steps are chosen, never flipped. Chart colours are CSS
custom properties defined once in `static/css/app.css`, with dark steps chosen
for the dark surface rather than flipped. `dashboard/charts.py` is pure geometry
— no database, no request, no clock — so charts are asserted in tests.

Never a dual-axis chart. Two measures of different scale get two charts.

Card widths are chosen so each run of equal widths fills twelve columns; a row
adding up to ten leaves a ragged gap and the next card starts under it rather
than beside it. `tests/test_dashboard.py` asserts the registry never ends
mid-row. A card with a selector of its own resubmits `carried_params` as hidden
inputs - the page filters *and* the other cards' controls - or changing the
quarter on one card silently resets the year on another.

**The charts are controls.** A segment or bar links back to the analytics page
with its own value applied, and with it removed if it is already applied, so a
click is a toggle. `charts.py` stays pure: a slice carries an opaque `key` that
the geometry never interprets, the card names the query parameter, and the URL is
built in the template by `{% toggle_filter %}` (`core/templatetags/query.py`),
which preserves every other parameter. A segment standing for no single value -
the country chart's folded "Other" - has no key and renders inert. A category
stored as the empty string travels as `none`, because an empty query value
already means "no filter". Narrowing to zero accounts leaves no segment to click
back, which is what the removable chips above the cards are for.

### Soft deletion

Accounts are soft-deleted. Use `Account.objects.alive()` — or better,
`Ability.accounts()`, which already does.

## Settled decisions

These override `customer-health-platform-spec-en.md` where they conflict. Full
rationale is in [README.md](README.md).

| Decision | Resolution |
|---|---|
| Health Status | Three values: Healthy, Watch, At Risk. The spec's section 3 says two, contradicting its own three bands. |
| Managed account with no meeting frequency | Factor omitted, scored out of 80, weights **not** redistributed, band capped at Watch. |
| Timezone | Africa/Cairo for all day counts. |
| Missing or stale CSAT | Scored 50 and flagged. Not omitted, not green. |
| Task emails | One digest per person per morning, plus an immediate standalone email per health alert. |

The 80-point cap needs stating precisely: four green factors at 20% each is
exactly 80.00, which the band rule would call Healthy. `engine.band()` forces
Watch and records `capped_by_omission` so the interface can explain an otherwise
puzzling Watch on an all-green account.

## Current state

Built and tested: the data model, the scoring engine and its history, the dual
trigger, the task engine (all four types, routing, idempotency, alert
lifecycle), the authorization layer, notifications, and the demo seed.

Also built: the **web interface** — sign-in, a working landing dashboard,
account list/detail/edit, the power/interest grid and triage tray, the portfolio
attention list, the health panel with override, activity logging and voiding,
and the task queue. Server-rendered Django templates with Bootstrap 5 and HTMX
from a CDN; no JavaScript build step.

Also built: the **analytics dashboard** (`/analytics/`) — twelve cards, one
shared filter, a per-role card registry, server-rendered inline SVG charts.
`/` is a separate working landing page ("My day"), not the analytics grid.

Also built: the **importer** (`/import/`, any CSM, CSM Senior or ACM — an ACM's
`import.run` is narrowed by `Ability.importable_kinds()` to contacts only, since
the importer writes accounts with no `account.edit` check of its own) for
accounts and contacts as .xlsx or .csv; the **export** (`/import/export/`, every
role) of the same two as a read-only, row-scoped .xlsx, with the health columns
present only for a reader who holds `health.view`; **account
custom fields** (`/accounts/custom-fields/`, Admin-only to define, any editor
to fill in) — free text, date/time or a dropdown list, named once and shown
before Description on every account's form; **engagement entry**
(`/engagement/account/<pk>/`) for tickets, CSAT, adoption events and
engagement items; account create and soft delete; contact create, edit and
hard delete (a contact carries no soft-delete columns of its own — everything
that names one, `Activity`/`Task`/`CsatResponse`, is `SET_NULL`); task edit
and delete; and **in-app notifications** (`/notifications/`).

Every capability has a surface except `lifecycle.edit` (enforced by the account
form omitting the field).

Dark mode follows the OS preference with a toggle that overrides it, set in an
inline script before first paint so a dark viewer gets no white flash. Both
palettes are validated steps, not an inversion.

Not built: the **integration seams**; `csm/integrations/` is an app shell with
no models, deliberately deferred.

Open questions are listed at the end of README.md; each blocks a specific
upcoming phase rather than current work.

## Testing conventions

- `tests/conftest.py` snapshots and restores event subscribers around each test
  rather than clearing them, so app-startup registrations survive.
- `tests/factories.py` holds factory-boy factories; role fixtures (`csm_user`,
  `senior_csm_user`, `acm_user`, `manager_user`) are in conftest.
- `time-machine` pins time for day-boundary tests.
- Engine tests need no database at all — they construct `ScoringInput` directly.
- Architectural rules are enforced by tests that parse the source with `ast`
  ([tests/test_access.py](tests/test_access.py),
  [tests/test_task_routing.py](tests/test_task_routing.py)). If one fails, the
  fix is the code, not the test.
