"""Every threshold, weight and boundary in the scoring engine.

These live in code rather than in a database table on purpose. Changing a
threshold retroactively changes the meaning of every score already stored in
history, so it belongs in version control with a commit message and a test run,
not in an admin form.

Bump ENGINE_VERSION whenever a number here changes. Each history row records the
version that produced it, so an old score stays interpretable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from csm.core.enums import FactorId, FactorSetId

ENGINE_VERSION = "1.3.0"

# ---------------------------------------------------------------------------
# Colour to points
# ---------------------------------------------------------------------------

GREEN_POINTS = 100
YELLOW_POINTS = 50
RED_POINTS = 0

# ---------------------------------------------------------------------------
# Final bands
# ---------------------------------------------------------------------------
# Compared against the exact Decimal, never a rounded display value.

BAND_HEALTHY_MIN = Decimal("80")
BAND_WATCH_MIN = Decimal("50")

# What share of the scored factors must be red to force At Risk.
#
# Without this a neglected non-managed account lands on exactly 50.00 - no
# contact and no adoption are both red, tickets are green because nobody raised
# any, and a recent good rating is green - which the band rule reads as Watch by
# a margin of nothing at all. Half the things measured being red is not a Watch,
# and a status that depends on landing precisely on a boundary is one weight
# change away from flipping.
#
# A share rather than a count, because the two factor sets are different sizes.
# Two red of four is half the picture; two red of five is not, and an absolute
# threshold would send a managed account to At Risk eight days after its last
# call - weekly contact and meeting attendance both turn red quickly and
# together, so it would fire constantly and teach everyone to ignore it.
#
# A cap rather than a reweighting, for the same reason the eighty-point cap is
# one: the weights are the specification's, and changing them would silently
# restate every score already in history.
RED_SHARE_FOR_AT_RISK = Decimal("0.5")

# ---------------------------------------------------------------------------
# Open Tickets
# ---------------------------------------------------------------------------
# Weighted score = bugs x 3 + general x 1. Note the consequence: one bug scores
# 3 and is yellow, two bugs score 6 and are red. A single bug never turns an
# account red on its own.

TICKET_BUG_WEIGHT = 3
TICKET_GENERAL_WEIGHT = 1
TICKET_GREEN_MAX = 0  # weighted == 0
TICKET_YELLOW_MAX = 3  # 1..3 yellow, 4 and above red

# ---------------------------------------------------------------------------
# Day-count factors: Monthly Adoption and General Contact
# ---------------------------------------------------------------------------
# Green is strictly under 15 days. Yellow is 15 to 30 inclusive. Red is over 30.
# So exactly 15 is yellow, exactly 30 is yellow, and 31 is red.

DAYS_GREEN_MAX_EXCLUSIVE = 15
DAYS_YELLOW_MAX_INCLUSIVE = 30

# ---------------------------------------------------------------------------
# Weekly Contact (binary, Managed Service only)
# ---------------------------------------------------------------------------
# "Within the last 7 days" read as inclusive: exactly 7 days ago is green,
# 8 days ago is red. Meetings are excluded from this factor and counted only
# under Meeting Attendance, which is the specification's guard against a single
# event scoring twice.

WEEKLY_CONTACT_MAX_DAYS = 7

# ---------------------------------------------------------------------------
# Meeting Attendance (Managed Service only)
# ---------------------------------------------------------------------------
# Compliance is compared as integers, never as a float percentage:
#   green  m >= r          (100% or better; over-achievement is never penalised)
#   yellow 2m >= r         (50% to 99%)
#   red    otherwise       (under 50%)
# The window is the calendar month of the as-of date, from its 1st to as_of
# inclusive: on 30 September, meetings from 1 to 30 September count and one on
# 31 August does not. On the 1st the count starts again from zero, so a managed
# account is red on that factor until the month's first meeting is completed.
#
# There is no grace window after the baseline date (removed in 1.2.0): only a
# completed Meeting activity moves this factor, never filling in dates.

# ---------------------------------------------------------------------------
# CSAT
# ---------------------------------------------------------------------------

# Scored on the latest calendar quarter that holds a rating (1.3.0; earlier
# versions read the single newest rating). Within it, each contact's latest
# rating counts once - one person asked twice is still one voice - and a rating
# naming no contact counts on its own. The average of those is banded:
#   green  average >= 4.00
#   yellow average >= 3.00
#   red    otherwise
# A quarter stays in force until the next one has a rating, so early in a new
# quarter, before the round is collected, last quarter's result holds.
#
# Unlike every other factor, CSAT's points are proportional to the average
# rather than stepped by its colour, so each tenth of a point moves the score:
#   from 2.00 up:  points = 25 x (average - 1)   2.00 is 25, 3.00 is 50,
#                                                4.00 is 75, 5.00 is 100
#   below 2.00:    a straight line from 5 at 1.00 up to 25 at 2.00
# The lowest possible average is not zero: 1.00 earns 5 points, which is 1% of
# the final score on a managed account (weight 20%) and 1.25% on a non-managed
# one (weight 25%). The colour is still banded below; it is what the red-factor
# cap counts.
CSAT_POINTS_PER_STEP = Decimal("25")
CSAT_POINTS_AT_ONE = Decimal("5")
CSAT_GREEN_MIN = Decimal("4.00")
CSAT_YELLOW_MIN = Decimal("3.00")
# A quarter whose newest rating is older than this is treated as absent. Set to
# None to disable expiry.
CSAT_VALIDITY_DAYS = 180
# No rating on record scores yellow: zero would redline the whole portfolio at
# go-live before anyone has collected a rating, and 100 would hide real risk.
CSAT_MISSING_POINTS = YELLOW_POINTS


# ---------------------------------------------------------------------------
# Factor sets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorSet:
    id: str
    factors: tuple[str, ...]
    weight_per_factor: Decimal

    @property
    def total_weight(self) -> Decimal:
        return self.weight_per_factor * len(self.factors)


MANAGED = FactorSet(
    id=FactorSetId.MANAGED.value,
    factors=(
        FactorId.OPEN_TICKETS.value,
        FactorId.MONTHLY_ADOPTION.value,
        FactorId.CSAT.value,
        FactorId.WEEKLY_CONTACT.value,
        FactorId.MEETING_ATTENDANCE.value,
    ),
    weight_per_factor=Decimal("0.20"),
)

NON_MANAGED = FactorSet(
    id=FactorSetId.NON_MANAGED.value,
    factors=(
        FactorId.OPEN_TICKETS.value,
        FactorId.MONTHLY_ADOPTION.value,
        FactorId.CSAT.value,
        FactorId.GENERAL_CONTACT.value,
    ),
    weight_per_factor=Decimal("0.25"),
)

# A module-level invariant, also asserted as a unit test.
for _fs in (MANAGED, NON_MANAGED):
    assert _fs.total_weight == Decimal("1.00"), f"{_fs.id} weights do not sum to 1"


def select_factor_set(is_managed_service: bool) -> FactorSet:
    """Choose the factor set.

    Depends on whether the account is Managed Service and on nothing else. In
    particular the account's Class is not even carried on ScoringInput, which is
    a stronger guarantee of independence than a comment would be.
    """
    return MANAGED if is_managed_service else NON_MANAGED
