"""The single source of "now" for the whole platform.

Every health factor is a day count, so the score flips at midnight in one
business timezone. Reading ``date.today()`` anywhere else would make that
boundary depend on the machine running the job. All storage stays UTC; this
module is the only place the conversion happens.

Note for Africa/Cairo: Egypt reinstated daylight saving in 2023, so the offset
moves between +02:00 and +03:00 during the year. Converting through the tz
database here, rather than adding a fixed offset anywhere, is what keeps the
day boundary correct across those transitions.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def business_tz() -> ZoneInfo:
    """The timezone in which every scoring day boundary is evaluated."""
    return ZoneInfo(settings.APP_TIME_ZONE)


def now() -> _dt.datetime:
    """Current instant, timezone-aware, in UTC. Use for stored timestamps."""
    return timezone.now()


def today() -> _dt.date:
    """Today's date in the business timezone.

    This is the ``as_of`` value every scoring run should be given.
    """
    return timezone.now().astimezone(business_tz()).date()


def local_time() -> _dt.time:
    """The current clock time in the business timezone.

    The companion to ``today()``. A task due at 09:00 is overdue at 09:01, and
    which 09:01 that is depends on the same timezone every day boundary here
    depends on - so it is read through this module and nowhere else.
    """
    return timezone.now().astimezone(business_tz()).time()


def to_business_date(moment: _dt.datetime) -> _dt.date:
    """Convert a stored instant to the business-local calendar date."""
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, _dt.UTC)
    return moment.astimezone(business_tz()).date()


def business_instant(day: _dt.date, moment: _dt.time) -> _dt.datetime:
    """The UTC instant of a business-local date and clock time.

    A task due at 09:00 is due at 09:00 in Cairo, which is a different UTC
    instant in July than in January. Anything comparing a due time to "now" has
    to come through here or it drifts by an hour twice a year.
    """
    local = _dt.datetime.combine(day, moment, tzinfo=business_tz())
    return local.astimezone(_dt.UTC)


def start_of_business_day(day: _dt.date) -> _dt.datetime:
    """The UTC instant at which ``day`` begins in the business timezone."""
    local = _dt.datetime.combine(day, _dt.time.min, tzinfo=business_tz())
    return local.astimezone(_dt.UTC)
