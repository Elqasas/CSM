# Bousla — Customer Health & Account Management Platform

Internal platform for managing the health and relationships of dPhish's client
portfolio. It centralises account data, scores account health automatically from
engagement signals, tracks stakeholders, and drives daily follow-up.

Requirements live in `BRD_Customer_Health_Platform.docx` and
`customer-health-platform-spec-en.md`. Where they disagree, the decisions in
[Settled decisions](#settled-decisions) win.

## Running it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # or the dependency list in pyproject.toml
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py seed_demo --reset
.venv/Scripts/python manage.py runserver
```

The demo seed creates sixty accounts and one user at each access level. Sign in
at `/admin/` with any of them; the password is `demo-password-123`.

No database server is needed for development or tests: the default is a local
SQLite file, and the test suite runs in memory. PostgreSQL 16 is the production
target, reached by setting `DATABASE_URL`. `docker-compose.yml` brings up
Postgres and MailHog when you want them.

### Against PostgreSQL

SQLite is not what production runs, and the schema leans on three things the two
engines implement differently: stored generated columns, partial unique indexes,
and JSONB. All of them work on both, which is exactly why a difference would be
quiet. Run the suite against the real thing before deploying:

```bash
DATABASE_URL=postgres://user:pass@localhost:5432/csm_test   .venv/Scripts/python -m pytest --ds=config.settings.test_postgres
```

Verified on PostgreSQL 16.15: all tests pass, both generated columns are created
`ALWAYS` and stored, all four partial unique indexes exist with the right
predicates and are enforced by the database rather than by application code, and
JSON columns are `jsonb`. Seeding produces the same portfolio as on SQLite.

```bash
.venv/Scripts/python -m pytest        # 437 tests
.venv/Scripts/python -m ruff check csm config tests
.venv/Scripts/python -m djlint templates
.venv/Scripts/python -m pytest --cov=csm --cov-report=term   # 94%
```

### Scheduled jobs

Three commands, all safe to run repeatedly:

```bash
manage.py recalculate_health      # the daily half of the dual trigger
manage.py generate_tasks          # renewal and quarterly reminders
manage.py send_due_notifications  # morning digests, then drain the email outbox
```

In a deployment they run through django-q rather than by hand:

```bash
manage.py setup_schedules         # register them; idempotent, belongs in deploy
manage.py setup_schedules --list  # show what is registered
manage.py qcluster                # the worker that actually runs them
```

Times are business-local and ordered: recalculate at 00:30 so the day counts
roll over on the right side of midnight, generate at 01:00 so an alert raised
overnight is in place before the reminders are written, notify at 07:00 so the
digest describes the day the reader is starting. `core/clock.py` converts them,
so Egypt's daylight-saving change does not drift the midnight job into the
previous evening.

**Without `qcluster` running, nothing recalculates overnight.** The event hooks
keep a score current while somebody is working an account; the daily run is what
catches the accounts nobody has touched, which are exactly the ones the score
exists to surface.

## How it is put together

```
csm/core/          enums, the business clock, the domain event bus, access
csm/users/         dPhish staff, entirely separate from client contacts
csm/accounts/      Account and Partner, plus the demo seed
csm/contacts/      stakeholders and the power/interest grid
csm/activities/    the append-only activity log
csm/engagement/    tickets, CSAT and adoption events (the health inputs)
csm/health/        the scoring engine, history and overrides
csm/tasks/         tasks, assignment and the generators
csm/notifications/ routing, in-app notifications and email
```

Four decisions shape everything else.

**The scoring engine is pure.** `health/engine.score()` takes a frozen input
dataclass and a date and returns a frozen result. It reads no database and never
calls the clock, so the whole scoring matrix is tested without fixtures and every
threshold is asserted on both sides of its boundary. `health/inputs.py` does all
the reading; `health/services.py` does all the writing.

**Role interpretation has exactly one home.** `core/access/` is the only place
that reads `role` or `is_senior`, and a test walks the codebase to keep it that
way. Row scope comes from `Ability.accounts()`; everything that lists, searches
or aggregates accounts starts there, including global search.

**Notification routing cannot see roles at all.** `notifications/routing.py`
imports nothing from the access package and takes five scalar fields, none of
which describes anyone's seniority. That is how "a senior CSM can act on any
account but is notified only on their own" is guaranteed structurally rather
than by convention. The senior reassignment rule is expressed as "the creator is
not this account's CSM or ACM", which needs no seniority flag.

**Three business rules are database invariants, not application checks.** One
open health alert per account, one active override per account, and the task
idempotency key are all partial unique indexes. A check-then-insert would leave a
window where the daily job and an event-driven recalculation both find nothing
and both insert.

## Settled decisions

| Decision | Resolution |
|---|---|
| Health Status | Three values: Healthy, Watch, At Risk. The spec's section 3 says two, contradicting its own three bands. |
| Managed account with no meeting frequency | Factor omitted, scored out of 80, weights not redistributed, and capped at Watch. |
| Timezone for day counts | Africa/Cairo. All storage stays UTC; `core/clock.py` is the only conversion point. |
| Missing or stale CSAT | Scored 50, flagged. Zero would redline the portfolio at go-live; 100 would hide real risk. |
| CSAT with several raters | The latest calendar quarter holding a rating, each contact's latest rating in it counted once, averaged: 4.00+ green, 3.00+ yellow. Several stakeholders rate each quarter, so the newest single rating let whoever was typed in last decide. Last quarter holds until the next round is collected. |
| CSAT points | Proportional, not stepped: 25 x (average - 1) from 2.00 up - so 2.00 is 25, 3.00 is 50, 4.00 is 75, 5.00 is 100 - and below 2.00 a line from 5 points at 1.00 to 25 at 2.00, so the worst average still adds 1% on a managed account (1.25% non-managed) rather than nothing. The colour stays banded and is what the red-factor cap counts. |
| Task emails | One digest per person per morning, plus an immediate standalone email for every health alert. |
| Medium on the power/interest grid | Counts as high influence, low interest. Medium/Medium is Keep satisfied. |
| An unassessed contact | Influence and interest are nullable with no default. Unassessed is not Medium. |
| A returning churned client | Clean start. Churned is an unscored stage, so re-entering a scored one resets the baseline. |
| Half the factors red | Forces At Risk whatever the score says. A share, not a count. |
| "License" in the sheet | The number of users the client bought, so it *is* `boq_users_count`. No separate field. |

The two axes are asymmetric because the cost of being wrong is. Under-reading
influence is dangerous: a decision-maker filed as Low is a blocker nobody
cultivated, and that surfaces at renewal. Over-reading interest is merely
wasteful. Treating Medium as the high half of both axes, as the first cut did,
also left Manage Closely holding four cells of nine including Medium/Medium,
and a quadrant that holds everyone advises nothing.

Nullable influence and interest matter for the same reason. They used to default
to Medium, and since Medium counted as high on both axes, every contact arrived
in Manage Closely and stayed there until hand-triaged. Unassessed contacts now
have no grid position at all and collect in a triage tray, on both the grid and
the attention list.

License was the sheet's own name for the seats a client purchased, which the
platform already held as `boq_users_count`. Keeping both would have been the
same fact in two columns, free to disagree, so the field is gone and `License`
is an accepted header for the count. A tier name in that column is now an error
rather than silently stored.

`contacts/by_attention()` ranks rather than filters: the spec asks for neglected
contacts "especially those with High Influence", which is emphasis in an
ordering, not an exclusive filter. A low-influence contact who has gone quiet
for a year still appears, below the people who matter more. The axis mapping
lives once, as `HIGH_INFLUENCE` and `HIGH_INTEREST` in `contacts/models.py`, and
a test walks all nine cells holding the Python property and the SQL ordering to
one answer. They had already drifted apart before that test existed.

The red-factor cap is the mirror image at the bottom of the scale. A neglected
non-managed account — no contact, no adoption, no tickets because nobody raised
any, and a recent good rating — lands on exactly 50.00, which the band rule reads
as Watch by a margin of nothing. Half the things measured being red is not a
Watch, and a status resting on hitting a boundary exactly is one weight change
from flipping. It is a *share* rather than a count because the two factor sets
are different sizes: two red of four is half the picture, two red of five is not,
and a flat count would send a managed account to At Risk eight days after its
last call, because weekly contact and meeting attendance turn red quickly and
together. Both caps leave the specification's weights untouched, so every score
already in history still means what it said.

A returning churned client gets a clean start rather than resuming its old day
counts. Churned is an unscored stage, so re-entering a scored one resets the
baseline by the same rule that covers Onboarding to Active. Nothing is erased —
the activity log, renewal count and score history all remain — it simply stops
being read as a year of neglect on the day somebody comes back.

The eighty-point cap needs stating precisely. Four green factors at twenty
percent each is exactly 80.00, which the band rule calls Healthy, so without the
cap an account missing a factor could still show green. `engine.band()` forces
Watch instead and records `capped_by_omission`, so the interface can explain an
otherwise puzzling Watch on an all-green account.

## Where the spec was extended

These are additions, not reinterpretations. Each closes a gap that would
otherwise have surfaced in production.

- **Tickets, CSAT responses and adoption events** have no storage in the spec,
  but the engine reads all three. They are manual entry now, with source and
  external-key columns ready for Jira and product telemetry.
- **Engagement items** exist because dashboard card 11 asks for a trend over
  time, which a scalar count cannot produce. The imported scalars remain as a
  pre-import baseline.
- **`scoring_baseline_date`** generalises the import baseline rule. The spec sets
  a baseline at import but not on the Onboarding to Active transition, where an
  account with no history in its new stage would read as months of neglect.
- **Meeting Attendance has no grace window.** Only completed Meeting activities
  in the current calendar month count (1st to today, restarting at zero on the
  1st); filling in the frequency or the account's dates
  earns nothing, so a managed account with no meeting yet is red. (Engine 1.1.0
  and earlier scored it green for 30 days after the baseline.)
- **Quarterly rollover.** If the meeting date goes stale and nobody sets a new
  one, the account would silently stop receiving reminders forever. A nudge task
  is created instead.
- **Accounts are soft-deleted.** The spec's cascade to every activity, contact
  and task is unrecoverable. User-facing behaviour is unchanged.
- **Generated tasks are cancelled, never hard-deleted.** A hard delete frees the
  idempotency key and the task reappears the next morning.
- **Unassigned tasks surface rather than vanish.** An account imported with a
  blank CSM column must not silently swallow a renewal reminder.
- **The power/interest grid renders three by three.** The spec names four
  strategies but influence and interest each have three levels. Nine cells keep
  Medium visible, each tinted by one of the four strategies, so the spec's
  vocabulary survives without flattening the data. See below for the mapping.

## Meetings, tasks and the Teams integration

Settled before the integration work starts, so it is not re-argued under
deadline. **Meetings and calls stay task kinds today, and will get their own
model when Teams lands. Neither becomes an `Activity`.**

The axis that matters is not *task versus meeting*. It is **planned versus
happened**:

- a **Task** is an obligation - one owner set, one due date, a progress somebody
  sets, an idempotency key when a generator wrote it;
- an **Activity** is an immutable record that something occurred. It is voided
  rather than deleted, so an old history row stays explainable.

**Why they must not merge.** `health/inputs.py` reads `Activity` for three of
the five scoring factors: general contact, weekly contact, and
`activities.meetings()` for meeting attendance. Fold planned work into that
table and a meeting booked for next month starts counting as contact that
happened - a health score that rises because somebody opened a calendar. That is
not a tidiness objection; it is silent inflation of the number the platform
exists to produce.

**Why a meeting outgrows `Task` anyway.** Teams needs a start *and* an end, a
join URL, an external calendar id, an organiser, many attendees on both sides
(`Task` has a single `contact`), per-attendee responses, and recurrence. That is
six or more nullable columns on `tasks`, meaningless on every generated renewal
row - and the external id needs a unique constraint for idempotent webhook sync,
which is the lesson `auto_key` already taught this codebase once.

The shape, in order:

1. **Now**: Meeting and Call are task kinds. They cost nothing, they cover "call
   them Thursday", and they block none of what follows.
2. **When Teams starts**: a small `csm/meetings/` app with a `Meeting` model -
   start and end, join URL, unique `external_id`, organiser, staff attendees,
   contact attendees, recurrence key. A `Task` may point at a `Meeting`, so the
   obligation stays in somebody's queue and the calendar entry stays a calendar
   entry. Neither pretends to be the other.
3. **When a meeting happens**: the integration writes an `Activity` through
   `core/events.py`. Scoring keeps one definition of "we spoke to them", and it
   keeps being *happened* rather than *planned*.
4. **All Graph API code lives in `csm/integrations/`**, including the dPhish
   user to Teams user mapping. Nothing else imports the SDK, so the day the API
   changes, one app changes.

**One interface, several tables.** Wanting a single place to see everything that
touches a client is right about the interface and wrong about the storage - so
that is where it was built. `/activities/account/<pk>/` reads activities and
tasks together, newest first, each line marked *Happened*, *Planned* or *Done*,
and the page says out loud that only what happened counts towards the score. The
merge is `activities/timeline.py`: a pure function over two lists, no database
and no request, which is what stops it quietly becoming a place that writes.

**Ruled out**: a single polymorphic table with a `kind` discriminator. Every
query grows a filter nobody can forget safely, and the first forgotten one is a
wrong health score rather than a wrong page.

## What is built, and what is not

Done, with tests:

- The full data model, on both SQLite and PostgreSQL
- The scoring engine, its history, and the override rule
- The dual trigger: model-level event hooks plus the daily command
- The task engine: all four types, the routing table, idempotency, the alert lifecycle
- The authorization layer: row scope, capabilities, field scope, the presenter boundary
- Notifications: routing, in-app records, digests and the email outbox
- The demo seed
- **The web interface**: sign-in, the working dashboard, account list and detail,
  the power/interest grid and triage tray, the portfolio attention list, the
  health panel with its factor breakdown and override, activity logging and
  voiding, and the task queue

The interface is server-rendered Django templates with Bootstrap 5 and HTMX from
a CDN. There is no JavaScript build step and nothing to compile.

Its tests are the point rather than a formality. `tests/test_views.py` drives the
real URLs and the real templates as each of the four access levels, because a
test that called a view function directly would not notice a template asking the
presenter for a field it may not have. Row scope and the ACM health rule are
asserted on every surface an ACM can reach: the list, the detail page, the filter
parameters, the sort parameters, the task queue and the type filter.

- **The analytics dashboard**: twelve cards behind one shared filter, with a
  per-role card registry, and charts rendered server-side as inline SVG

Charts are SVG built by pure functions in `dashboard/charts.py` — no database, no
request, no clock — so the geometry is asserted in tests rather than
screenshotted. Card visibility is declared, not checked: a card names a
capability and the registry drops it, which is how "an ACM sees every card except
Health Status" holds without any card, view or template reading a role.

The palette was validated rather than chosen by eye, and the result changed the
design. Traffic-light red against green measures 4.1 apart under deuteranopia
where 8 is the floor — so the health donut labels every segment with its status
and count and ships a table of the same numbers. Colour is a second channel
there, never the only one.

- **The importer**: upload, preview and apply, for **accounts and contacts**, as
  **.xlsx or .csv**, with a generated template for each of the four combinations

The importer is forgiving about shape and strict about meaning. Header
spelling, case, spacing and punctuation are normalised and matched against
aliases, because exports of the same sheet differ in all of those. Values are
not treated that way: an unknown country or an unrecognised CSM email is an
error on the row, never a guess.

Rows are staged in `import_rows` before anything is written, so the preview
shows exactly what will land and applying writes what was previewed rather than
re-parsing and hoping the second pass agrees. Rows with errors are skipped
individually — a sixty-row sheet with two bad rows imports fifty-eight, and the
two are shown rather than silently dropped.

Two settled rules are enforced there rather than left to the caller. The CSM and
ACM columns **never create a user** — an address or name that matches nobody is
an error, because auto-creating staff from a sales export would make the access
system's row scope depend on whatever that export happened to contain. Focal points import as contacts with **no influence or
interest**, so they arrive in the triage tray instead of being guessed onto the
grid. The same holds for the contacts file: a blank influence or interest cell
imports as unassessed rather than as Medium.

Each role has **two columns, an email and a name**, and either identifies the
person. Email wins when present, being unique. A name alone is matched after
folding case and spacing, but a name shared by two active users is an error
rather than a coin toss — attaching a portfolio to the wrong Casey hands them
somebody else's accounts and stops the real owner seeing their own. When both
are filled in and point at different people the row fails: that is what a sheet
looks like after one column was edited and the other forgotten, and picking a
winner silently would bake the stale half in.

A **contacts file never creates an account**. It matches on client code and
errors on a code it does not recognise, because an implicitly created account
would have a name and nothing else while still counting on every dashboard card.
Accounts import first.

CSV gets its own handling rather than being treated as a lesser spreadsheet.
Excel writes a byte-order mark, which would otherwise ride along on the first
header and stop `Client Code` matching anything; it emits semicolon-separated
files in locales whose decimal separator is a comma, which read as one enormous
column and validate as empty; and it does not always write UTF-8. Each of those
fails by parsing as *something* rather than by raising, so each is handled and
tested. The CSV templates carry their guidance as `#` comment lines above the
header, with the example row commented out too.

- **Engagement entry**: tickets, CSAT ratings, adoption events and the dated
  engagement items, each behind its own capability
- **Account creation and soft deletion**, task editing and deletion

Every task is editable, generated ones included. A generated task's identity is
the account plus the date its obligation derives from, not its title or due
date, and the generator never rewrites a task it already finds — so a reworded
renewal reminder survives the nightly run. Moving the contract date is what
replaces it, which is the obligation changing rather than the edit being lost.
- **In-app notifications**: a badge, a list, and mark-as-read

Every capability in `core/access/capabilities.py` now has a surface, bar two:
`lifecycle.edit`, enforced by the account form dropping the field rather than by
a page of its own, and `user.manage`, which stays in Django admin.

The engagement forms are where the score actually moves, and none of them calls
the engine: saving a ticket fires the model hook in `health/signals.py` and the
account rescores after commit. Recording two bug tickets against a Healthy
account drops it to Watch, and closing them brings it back — checked against the
running application, not only in tests.

The interface follows the operating system's light or dark preference and has a
toggle that overrides it. The chart palette ships validated steps for both, so
dark mode is a selected design rather than an inverted one.

**Jira Service Management** feeds tickets. Every 15 minutes `sync_jira` reads
the issues raised under each JSM Organization and writes them as tickets on the
client of the same name (case and spacing ignored). A Bug scores three in Open
Tickets and every other issue type one. Once Jira is configured, tickets are no
longer entered by hand, and synced ones close in Jira, not here. Organizations
that match no client, or more than one, are listed on `/integrations/` (Admin)
rather than guessed.

**Microsoft 365 calendars and Teams attendance** are built on the shape
settled above. `sync_teams` reads each active user's Outlook calendar by email
into `csm/meetings/` (one `Meeting` per occurrence, one `CalendarEntry` per
person holding it) and shows it on that person's calendar only, with an All /
Platform / Microsoft 365 filter. A Teams meeting counts as client contact only
when its attendance report lists a contact's stored email - then, and only then,
the sync writes a `MEETING` activity (`source=TEAMS`). Invited but absent counts
for nothing. An Admin connects it on **Settings → Integrations → Microsoft 365**
with an Entra ID app registration; the page lists the four Graph permissions and
the Teams access policy attendance needs.

To connect Jira, an Admin opens **Settings → Integrations**, enters the Jira site,
the account email and an API token (stored encrypted, never shown again),
presses **Test connection**, then ticks **Use Jira for tickets** and saves. Run
`manage.py sync_jira --full` once afterwards for the first complete read. The
`JIRA_*` variables in `.env.example` still work for a deployment that prefers
them, until the page is first saved.

Not built yet:

- **Outlook email, and writing to calendars.** Microsoft 365 is read-only:
  calendars in, Teams attendance in. Nothing is booked or sent from Bousla.
- **Pushing to Jira.** The sync is read-only; Internal tasks are not yet sent
  out as issues (`Task.jira_ticket_id` stays reserved).

## Open questions

None. Every question this build was carrying has been answered and is recorded
in [Settled decisions](#settled-decisions): what the sheet's License column
means, how the CSM and ACM columns resolve, where Medium sits on the
power/interest grid, what a returning churned client starts from, and whether
neglect alone can redline a non-managed account.

