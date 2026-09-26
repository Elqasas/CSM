"""The dashboard clock, and the line it must not cross.

A per-person timezone is a display preference. The moment one could reach the
scoring engine, two people would disagree about whether an account had gone a
week without contact - so the tests below check both halves: that the clock
follows the reader, and that nothing else does.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
import time_machine
from django.conf import settings
from django.urls import reverse

from csm.users.models import DisplayPreference
from tests.factories import UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def person(db):
    user = UserFactory(csm=True, email="clock@dphish.com", full_name="Casey Morgan")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


def test_a_reader_who_has_never_chosen_follows_the_business_timezone(person):
    """Never None, and never blank on screen: a dashboard that cannot render
    because somebody has no preference row is a worse failure than a clock
    nobody has set."""
    preference = DisplayPreference.for_user(person)

    assert preference.timezone == ""
    assert preference.effective_timezone == settings.APP_TIME_ZONE


def test_the_clock_reads_in_the_readers_own_zone(client, person):
    """13:00 UTC is 16:00 in Cairo and 09:00 in New York."""
    DisplayPreference.objects.update_or_create(
        user=person, defaults={"timezone": "America/New_York"}
    )
    login(client, person)

    with time_machine.travel("2026-06-15 13:00:00+00:00", tick=False):
        response = client.get(reverse("dashboard:index"))

    assert response.context["clock_now"] == "09:00"
    assert response.context["clock_timezone"] == "America/New_York"


def test_the_date_stays_the_business_date_whatever_the_clock_says(client, person):
    """The line this must not cross.

    At 22:30 in Cairo it is already the next day in Auckland. The heading may
    show a reader's own clock; the date every count on the page is made against
    is the one the platform counts in.
    """
    DisplayPreference.objects.update_or_create(
        user=person, defaults={"timezone": "Pacific/Auckland"}
    )
    login(client, person)

    # 19:30 UTC on 15 June: 22:30 in Cairo, 07:30 on the 16th in Auckland.
    with time_machine.travel("2026-06-15 19:30:00+00:00", tick=False):
        response = client.get(reverse("dashboard:index"))

    assert response.context["today"] == dt.date(2026, 6, 15), "Cairo's day"
    assert response.context["clock_now"] == "07:30", "their clock"


def test_an_unknown_zone_falls_back_rather_than_breaking_the_page(client, person):
    """A wrong clock is a nuisance; a broken dashboard is not."""
    DisplayPreference.objects.update_or_create(
        user=person, defaults={"timezone": "Middle/Earth"}
    )
    login(client, person)

    with time_machine.travel("2026-06-15 13:00:00+00:00", tick=False):
        response = client.get(reverse("dashboard:index"))

    # Computed inside the same frozen instant as the request: reading the real
    # clock afterwards compares two different moments.
    with time_machine.travel("2026-06-15 13:00:00+00:00", tick=False):
        business = dt.datetime.now(tz=ZoneInfo(settings.APP_TIME_ZONE)).strftime("%H:%M")

    assert response.status_code == 200
    assert response.context["clock_now"] == business, "Cairo, not a crash"


def test_the_page_says_which_zone_the_clock_is_in(client, person):
    """A time with no zone beside it is the sort of thing people plan meetings
    around wrongly."""
    DisplayPreference.objects.update_or_create(
        user=person, defaults={"timezone": "Europe/London", "country": "GB"}
    )
    login(client, person)
    body = client.get(reverse("dashboard:index")).content.decode()

    assert "Europe/London" in body
    assert "United Kingdom" in body
    assert f"dates counted in {settings.APP_TIME_ZONE}" in body


def test_the_zone_is_not_repeated_when_it_is_the_business_one(client, person):
    login(client, person)
    body = client.get(reverse("dashboard:index")).content.decode()

    assert "dates counted in" not in body, "nothing to warn about"


def test_the_settings_page_saves_and_is_the_readers_own(client, person):
    other = UserFactory(csm=True, email="clock-other@dphish.com")
    login(client, person)

    response = client.post(
        reverse("users:display_settings"),
        {"timezone": "Asia/Dubai", "country": "AE"},
    )
    assert response.status_code == 302

    assert DisplayPreference.for_user(person).timezone == "Asia/Dubai"
    assert DisplayPreference.for_user(other).timezone == ""


def test_the_settings_are_reachable_from_the_gear(client, person):
    """The gear opens the settings page; that page carries the link."""
    login(client, person)

    home = client.get(reverse("dashboard:index")).content.decode()
    assert reverse("settings") in home

    page = client.get(reverse("settings")).content.decode()
    assert reverse("users:display_settings") in page


def test_nothing_about_a_timezone_reaches_the_business_clock():
    """Structural, not incidental: ``core.clock`` is the single source of the
    day boundary and it takes no argument that could carry a person's zone."""
    import inspect

    from csm.core import clock

    source = inspect.getsource(clock)
    assert "DisplayPreference" not in source
    assert "display_preference" not in source
