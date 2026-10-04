"""Query-string filters and ordering that more than one page offers.

The renewal quarter is asked for on both the account list and the analytics
dashboard, which is exactly the situation where the same filter gets written
twice and the two spellings drift: one page reading ``Q4`` and the other
``2026-4``, or one treating an unknown value as "everything" and the other as
"nothing". Parsing, applying and offering it live here, and both views call
these.

The choices are always built from the *unfiltered* scope. Deriving them from
the filtered queryset would collapse the dropdown to the single quarter already
selected, leaving no way back out of it without editing the URL.
"""

from __future__ import annotations

#: The query-string key. Analytics already uses ``quarter`` and ``quarter_year``
#: for the renewals card's own selector, which is a different control with a
#: different job, so this one gets its own name.
RENEWAL_PARAM = "renewal"


def parse_renewal_quarter(raw: str | None) -> tuple[int, int] | None:
    """``"2026-4"`` to ``(2026, 4)``; anything else to None.

    A value carries its year. A bare ``Q4`` would mean something different in
    January than in December, and the reader would have no way of telling which
    reading the page took.
    """
    year_text, _, quarter_text = (raw or "").strip().partition("-")
    if not (year_text.isdigit() and quarter_text.isdigit()):
        return None
    year, quarter = int(year_text), int(quarter_text)
    if not (1 <= quarter <= 4 and 1900 <= year <= 2999):
        return None
    return year, quarter


def apply_renewal_quarter(accounts, raw: str | None):
    """Narrow to accounts whose contract ends in that quarter.

    An unparseable value is ignored rather than returning nothing. A filter the
    page cannot read should leave the page as it was, not make the portfolio
    look empty.
    """
    parsed = parse_renewal_quarter(raw)
    if parsed is None:
        return accounts
    year, quarter = parsed
    return accounts.filter(
        contract_end_date__year=year, contract_end_date__quarter=quarter
    )


def renewal_quarter_choices(accounts) -> list[tuple[str, str]]:
    """Every quarter this user actually has a renewal in, oldest first.

    Offering a fixed span of years instead would fill the control with quarters
    that match nothing, and would quietly say how far the data runs beyond this
    user's scope.
    """
    rows = (
        accounts.exclude(contract_end_date__isnull=True)
        .with_renewal_quarter()
        .values_list("renewal_year_num", "renewal_quarter_num")
        .distinct()
        .order_by("renewal_year_num", "renewal_quarter_num")
    )
    return [(f"{year}-{quarter}", f"Q{quarter} {year}") for year, quarter in rows]


# ---------------------------------------------------------------------------
# Renewal quarter and year, asked for separately
# ---------------------------------------------------------------------------
# The analytics page offers the quarter as Q1-Q4 buttons with a year beside
# them, rather than one list of "Q3 2026" pairs. Two parameters, each optional:
# a quarter alone is that quarter in any year, a year alone is the whole year.

RENEWAL_QUARTER_PARAM = "rq"
RENEWAL_YEAR_PARAM = "ry"

QUARTERS = (("1", "Q1"), ("2", "Q2"), ("3", "Q3"), ("4", "Q4"))


def parse_quarter(raw: str | None) -> int | None:
    text = (raw or "").strip().upper().removeprefix("Q")
    return int(text) if text in {"1", "2", "3", "4"} else None


def parse_year(raw: str | None) -> int | None:
    text = (raw or "").strip()
    if not text.isdigit():
        return None
    year = int(text)
    return year if 1900 <= year <= 2999 else None


def apply_renewal_parts(accounts, quarter_raw: str | None, year_raw: str | None):
    """Narrow by the contract end's quarter, its year, or both.

    Each part that cannot be read is ignored on its own, for the same reason as
    above: an unreadable filter leaves the page as it was.
    """
    quarter, year = parse_quarter(quarter_raw), parse_year(year_raw)
    if quarter is not None:
        accounts = accounts.filter(contract_end_date__quarter=quarter)
    if year is not None:
        accounts = accounts.filter(contract_end_date__year=year)
    return accounts


def renewal_year_choices(accounts) -> list[int]:
    """The years this reader actually has a renewal in, oldest first."""
    return sorted(
        {
            day.year
            for day in accounts.exclude(contract_end_date__isnull=True)
            .values_list("contract_end_date", flat=True)
            .distinct()
        }
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
# The account list and the analytics roster render one table partial, so they
# have to sort by one set of keys as well. A second map would mean a column that
# sorts on one page and does nothing on the other, with the same header.

#: ``?sort=`` key to the model field behind the column. A closed set: the value
#: arrives from the query string, and ``order_by`` would otherwise take any
#: field name on the model, including the cached health columns an ACM may not
#: see.
SORT_FIELDS = {
    "name": "client_name",
    "code": "client_code",
    "class": "account_class",
    "license": "license_type",
    "boq": "boq_users_count",
    "stage": "lifecycle_stage",
    "csm": "csm__full_name",
    "acm": "acm__full_name",
    "renewal": "contract_end_date",
}

#: Kept apart because these two are only offered to a reader who may see health.
HEALTH_SORT_FIELDS = {"health": "effective_health_status", "score": "computed_health_score"}

DEFAULT_SORT = "name"


def _health_severity():
    """Health ordered by how much it matters, not by how it is spelled.

    Sorting the stored value alphabetically gives At Risk, Healthy, N/A, Watch,
    which puts the accounts in trouble next to the ones that are fine and reads
    as an accident. Nobody asks for their portfolio in alphabetical order of
    status.
    """
    from django.db.models import Case, IntegerField, Value, When

    from csm.core.enums import HealthStatus

    order = [
        HealthStatus.AT_RISK,
        HealthStatus.WATCH,
        HealthStatus.HEALTHY,
        HealthStatus.NOT_APPLICABLE,
    ]
    return Case(
        *[When(effective_health_status=status, then=Value(rank)) for rank, status in enumerate(order)],
        default=Value(len(order)),
        output_field=IntegerField(),
    )


def sortable_keys(*, shows_health: bool) -> set:
    """The keys a heading may link to. Health only for a reader who may see it."""
    keys = set(SORT_FIELDS)
    if shows_health:
        keys |= set(HEALTH_SORT_FIELDS)
    return keys


def sort_key(raw: str | None) -> tuple[str, bool]:
    """``"-renewal"`` to ``("renewal", True)``. Unknown keys are not rejected here."""
    raw = (raw or "").strip()
    descending = raw.startswith("-")
    return raw.lstrip("-"), descending


def apply_sort(accounts, raw: str | None, *, shows_health: bool):
    """Order by the column named, falling back to the account name.

    ``shows_health`` is a boolean, not an ability: no module outside
    ``core/access`` decides what a role may see, and this one only needs to be
    told the answer. Without the guard an ACM could sort by a status they may
    not read and recover the whole distribution from the row order.

    Nulls are placed explicitly. SQLite and PostgreSQL disagree about where an
    empty renewal date or an unassigned CSM belongs, and this application
    develops on one and runs on the other.
    """
    from django.db.models import F

    key, descending = sort_key(raw)
    if key not in sortable_keys(shows_health=shows_health):
        # An unreadable key gives exactly the default order, direction included.
        # Honouring the minus sign on a key that was thrown away would answer
        # "?sort=-" with the portfolio backwards, which nobody asked for.
        key, descending = DEFAULT_SORT, False

    if key in HEALTH_SORT_FIELDS:
        expression = _health_severity() if key == "health" else F(HEALTH_SORT_FIELDS[key])
    else:
        expression = F(SORT_FIELDS[key])

    ordered = (
        expression.desc(nulls_last=True) if descending else expression.asc(nulls_last=True)
    )
    # A second key so the order is stable: without it two accounts of the same
    # class come back in whatever order the database happened to read them, and
    # the page appears to reshuffle itself between visits.
    return accounts.order_by(ordered, "client_name")
