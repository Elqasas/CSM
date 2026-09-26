"""The Power/Interest grid.

The mapping had no tests at all, which is how ``quadrant`` and the attention
ordering came to disagree about what "high influence" meant. Every one of the
nine cells is asserted here, and one test holds the property and the queryset to
the same answer so they cannot drift apart again.
"""

from __future__ import annotations

import datetime as dt

import pytest

from csm.contacts.models import Contact
from csm.core.enums import ActivityType, Level, PowerInterestQuadrant
from tests.factories import AccountFactory, ActivityFactory, ContactFactory

MC = PowerInterestQuadrant.MANAGE_CLOSELY
KS = PowerInterestQuadrant.KEEP_SATISFIED
KI = PowerInterestQuadrant.KEEP_INFORMED
MON = PowerInterestQuadrant.MONITOR

#: The whole grid. Medium counts as high influence and low interest, so
#: "Manage closely" occupies two cells of nine and Medium/Medium is Keep
#: satisfied rather than the most demanding strategy in the set.
NINE_CELLS = [
    (Level.HIGH, Level.HIGH, MC),
    (Level.HIGH, Level.MEDIUM, KS),
    (Level.HIGH, Level.LOW, KS),
    (Level.MEDIUM, Level.HIGH, MC),
    (Level.MEDIUM, Level.MEDIUM, KS),
    (Level.MEDIUM, Level.LOW, KS),
    (Level.LOW, Level.HIGH, KI),
    (Level.LOW, Level.MEDIUM, MON),
    (Level.LOW, Level.LOW, MON),
]


@pytest.mark.parametrize(("influence", "interest", "expected"), NINE_CELLS)
def test_every_cell_of_the_grid_maps_to_its_strategy(influence, interest, expected):
    contact = Contact(influence=influence, interest=interest)
    assert contact.quadrant == expected


def test_manage_closely_stays_scarce():
    """Two cells of nine. A quadrant that holds everyone advises nothing."""
    assert sum(1 for *_, quadrant in NINE_CELLS if quadrant == MC) == 2


def test_medium_on_both_axes_is_keep_satisfied_not_manage_closely():
    contact = Contact(influence=Level.MEDIUM, interest=Level.MEDIUM)
    assert contact.quadrant == KS


# ---------------------------------------------------------------------------
# Unassessed contacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("influence", "interest"),
    [(None, None), (Level.HIGH, None), (None, Level.HIGH)],
)
def test_an_unassessed_contact_has_no_position(influence, interest):
    contact = Contact(influence=influence, interest=interest)
    assert contact.quadrant is None
    assert contact.is_assessed is False
    assert contact.quadrant_label == "Not assessed"


def test_a_new_contact_is_unassessed_rather_than_medium(db):
    """The default is what collapsed the grid: every contact in one quadrant."""
    contact = Contact.objects.create(account=AccountFactory(), full_name="Nadia Salem")
    assert contact.influence is None
    assert contact.interest is None
    assert contact.quadrant is None


# ---------------------------------------------------------------------------
# The property and the queryset must agree
# ---------------------------------------------------------------------------


def test_the_queryset_and_the_property_agree_on_every_cell(db):
    """The anti-drift test.

    ``quadrant`` is Python and the ordering is SQL, so the mapping is expressible
    twice. It was, and the two answers differed. This walks all nine cells and
    holds them to one answer.
    """
    account = AccountFactory()
    for influence, interest, _ in NINE_CELLS:
        ContactFactory(account=account, influence=influence, interest=interest)

    high_influence_ids = set(
        Contact.objects.filter(account=account)
        .filter(influence__in=[Level.HIGH, Level.MEDIUM])
        .values_list("id", flat=True)
    )
    for contact in Contact.objects.filter(account=account):
        property_says_high = contact.quadrant in (MC, KS)
        assert (contact.id in high_influence_ids) == property_says_high, (
            f"{contact.influence}/{contact.interest} disagrees between "
            f"the property and the queryset"
        )


# ---------------------------------------------------------------------------
# needs_triage
# ---------------------------------------------------------------------------


def test_needs_triage_holds_only_unassessed_active_contacts(db):
    account = AccountFactory()
    unassessed = ContactFactory(account=account, influence=None, interest=None)
    half = ContactFactory(account=account, influence=Level.HIGH, interest=None)
    ContactFactory(account=account, influence=Level.LOW, interest=Level.LOW)
    ContactFactory(account=account, influence=None, interest=None, is_active=False)

    assert set(Contact.objects.needs_triage()) == {unassessed, half}


# ---------------------------------------------------------------------------
# by_attention
# ---------------------------------------------------------------------------


def _contacted(contact, days_ago, today):
    ActivityFactory(
        account=contact.account,
        contact=contact,
        activity_type=ActivityType.CALL,
        occurred_on=today - dt.timedelta(days=days_ago),
    )


def test_by_attention_puts_the_high_influence_half_first(db):
    """It ranks rather than filters, so a low-influence contact still appears."""
    today = dt.date(2026, 9, 21)
    account = AccountFactory()

    # Low influence but long neglected: without ranking this would top the list.
    stale_low = ContactFactory(
        account=account, full_name="Low Stale", influence=Level.LOW, interest=Level.LOW
    )
    _contacted(stale_low, 300, today)

    # Medium influence counts as high, so it outranks the low-influence contact
    # even though it was contacted far more recently.
    fresh_medium = ContactFactory(
        account=account, full_name="Medium Fresh", influence=Level.MEDIUM, interest=Level.LOW
    )
    _contacted(fresh_medium, 5, today)

    ranked = list(Contact.objects.filter(account=account).by_attention())
    assert ranked == [fresh_medium, stale_low]


def test_by_attention_ranks_never_contacted_first_within_a_band(db):
    today = dt.date(2026, 9, 21)
    account = AccountFactory()

    never = ContactFactory(
        account=account, full_name="Never Spoken", influence=Level.HIGH, interest=Level.HIGH
    )
    recent = ContactFactory(
        account=account, full_name="Recent", influence=Level.HIGH, interest=Level.HIGH
    )
    _contacted(recent, 3, today)

    assert list(Contact.objects.filter(account=account).by_attention()) == [never, recent]


def test_by_attention_excludes_unassessed_contacts(db):
    account = AccountFactory()
    assessed = ContactFactory(account=account, influence=Level.LOW, interest=Level.LOW)
    ContactFactory(account=account, influence=None, interest=None)

    assert list(Contact.objects.filter(account=account).by_attention()) == [assessed]


def test_by_attention_excludes_inactive_contacts(db):
    account = AccountFactory()
    active = ContactFactory(account=account, influence=Level.HIGH, interest=Level.HIGH)
    ContactFactory(
        account=account, influence=Level.HIGH, interest=Level.HIGH, is_active=False
    )

    assert list(Contact.objects.filter(account=account).by_attention()) == [active]


# ---------------------------------------------------------------------------
# The contact form
# ---------------------------------------------------------------------------
# Reached through the real view, because the form's job is partly to shape what
# a POST is allowed to do: a blank assessment must arrive as None rather than as
# the empty string, and the channel list has to survive a round trip.


@pytest.fixture
def form_people(db):
    from tests.factories import UserFactory
    from tests.test_views import PASSWORD

    user = UserFactory(csm=True, email="form-csm@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


@pytest.fixture
def form_account(form_people):
    return AccountFactory(client_code="FORM1", csm=form_people)


def _post(client, url, **overrides):
    data = {
        "full_name": "Dana Farouk", "title": "CISO", "email": "dana@acme.example",
        "phone": "", "influence": "", "interest": "", "preferred_channel": "",
        "is_active": "on", "notes": "", "channels": [],
    }
    data.update(overrides)
    return client.post(url, data)


def test_a_contact_created_with_a_blank_assessment_is_unassessed(
    client, form_account, form_people
):
    """Blank must become None, not the empty string: the grid tests isnull."""
    from django.urls import reverse

    from tests.test_views import login

    login(client, form_people)
    _post(client, reverse("contacts:create", args=[form_account.pk]))

    contact = Contact.objects.get(full_name="Dana Farouk")
    assert contact.influence is None
    assert contact.interest is None
    assert contact.quadrant is None
    assert contact in Contact.objects.needs_triage()


def test_channels_survive_the_round_trip(client, form_account, form_people):
    from django.urls import reverse

    from csm.core.enums import CommunicationChannel
    from tests.test_views import login

    login(client, form_people)
    _post(
        client,
        reverse("contacts:create", args=[form_account.pk]),
        channels=[CommunicationChannel.EMAIL, CommunicationChannel.TEAMS],
        preferred_channel=CommunicationChannel.EMAIL,
    )

    contact = Contact.objects.get(full_name="Dana Farouk")
    assert set(contact.channels.values_list("channel", flat=True)) == {
        CommunicationChannel.EMAIL, CommunicationChannel.TEAMS
    }
    assert contact.preferred_channel == CommunicationChannel.EMAIL


def test_a_preferred_channel_outside_the_chosen_ones_is_refused(
    client, form_account, form_people
):
    from django.urls import reverse

    from csm.core.enums import CommunicationChannel
    from tests.test_views import login

    login(client, form_people)
    response = _post(
        client,
        reverse("contacts:create", args=[form_account.pk]),
        channels=[CommunicationChannel.EMAIL],
        preferred_channel=CommunicationChannel.PHONE,
    )

    assert response.status_code == 200
    assert not Contact.objects.filter(full_name="Dana Farouk").exists()


def test_editing_replaces_the_channel_set_rather_than_adding_to_it(
    client, form_account, form_people
):
    """The form is a statement of how this person is reachable now.

    Merging would make removing a channel impossible to express.
    """
    from django.urls import reverse

    from csm.core.enums import CommunicationChannel
    from tests.test_views import login

    login(client, form_people)
    _post(
        client,
        reverse("contacts:create", args=[form_account.pk]),
        channels=[CommunicationChannel.EMAIL, CommunicationChannel.PHONE],
    )
    contact = Contact.objects.get(full_name="Dana Farouk")

    _post(
        client,
        reverse("contacts:edit", args=[contact.pk]),
        channels=[CommunicationChannel.TEAMS],
    )

    assert set(contact.channels.values_list("channel", flat=True)) == {
        CommunicationChannel.TEAMS
    }


def test_assessing_a_contact_through_the_form_puts_them_on_the_grid(
    client, form_account, form_people
):
    from django.urls import reverse

    from tests.test_views import login

    login(client, form_people)
    _post(
        client,
        reverse("contacts:create", args=[form_account.pk]),
        influence=Level.HIGH, interest=Level.HIGH,
    )

    contact = Contact.objects.get(full_name="Dana Farouk")
    assert contact.quadrant == MC
    assert contact not in Contact.objects.needs_triage()


# ---------------------------------------------------------------------------
# Searching contacts
# ---------------------------------------------------------------------------


@pytest.fixture
def searchable(db):
    from tests.factories import UserFactory
    from tests.test_views import PASSWORD

    user = UserFactory(csm=True, email="search-csm@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])

    acme = AccountFactory(client_code="ACME", client_name="Acme Industrial", csm=user)
    zenith = AccountFactory(client_code="ZEN", client_name="Zenith Holdings", csm=user)

    ContactFactory(
        account=acme, full_name="Dana Farouk", title="CISO",
        email="dana@acme.example", influence=Level.HIGH, interest=Level.HIGH,
    )
    ContactFactory(
        account=zenith, full_name="Omar Said", title="IT Manager",
        email="omar@zenith.example", influence=Level.LOW, interest=Level.LOW,
    )
    ContactFactory(account=acme, full_name="Untriaged Person")
    return user


def _names(response):
    return [c.full_name for c in response.context["contacts"]]


def test_searching_by_contact_name(client, searchable):
    from django.urls import reverse

    from tests.test_views import login

    login(client, searchable)
    response = client.get(reverse("contacts:attention"), {"q": "Dana"})

    assert _names(response) == ["Dana Farouk"]


def test_searching_by_title_and_email(client, searchable):
    from django.urls import reverse

    from tests.test_views import login

    login(client, searchable)
    assert _names(client.get(reverse("contacts:attention"), {"q": "CISO"})) == ["Dana Farouk"]
    assert _names(
        client.get(reverse("contacts:attention"), {"q": "omar@zenith"})
    ) == ["Omar Said"]


def test_searching_by_the_account_they_work_for(client, searchable):
    """"Who do we know at Acme" is the question people arrive with."""
    from django.urls import reverse

    from tests.test_views import login

    login(client, searchable)
    assert _names(client.get(reverse("contacts:attention"), {"q": "Acme"})) == ["Dana Farouk"]
    assert _names(client.get(reverse("contacts:attention"), {"q": "ZEN"})) == ["Omar Said"]


def test_search_never_reaches_a_colleagues_contacts(client, searchable):
    """Scoped first, searched second. The other order searches the whole table
    and then hides the rows, which is the same mistake as filtering in the
    template."""
    from django.urls import reverse

    from tests.factories import UserFactory
    from tests.test_views import login

    theirs = AccountFactory(
        client_code="OTHER", client_name="Acme Rival",
        csm=UserFactory(csm=True, email="search-other@dphish.com"),
    )
    ContactFactory(
        account=theirs, full_name="Acme Lookalike",
        influence=Level.HIGH, interest=Level.HIGH,
    )

    login(client, searchable)
    names = _names(client.get(reverse("contacts:attention"), {"q": "Acme"}))

    assert "Acme Lookalike" not in names
    assert names == ["Dana Farouk"]


def test_the_triage_tray_is_searchable_too(client, searchable):
    from django.urls import reverse

    from tests.test_views import login

    login(client, searchable)
    assert _names(client.get(reverse("contacts:triage"), {"q": "Untriaged"})) == [
        "Untriaged Person"
    ]
    assert _names(client.get(reverse("contacts:triage"), {"q": "Dana"})) == []


def test_an_empty_search_returns_everything(client, searchable):
    from django.urls import reverse

    from tests.test_views import login

    login(client, searchable)
    assert set(_names(client.get(reverse("contacts:attention"), {"q": ""}))) == {
        "Dana Farouk", "Omar Said"
    }


def test_a_search_with_no_matches_says_so(client, searchable):
    from django.urls import reverse

    from tests.test_views import login

    login(client, searchable)
    body = client.get(reverse("contacts:attention"), {"q": "nobodyhere"}).content.decode()

    assert "Nobody matches" in body
    assert "Clear the search" in body


def test_both_contact_pages_offer_the_search_box(client, searchable):
    from django.urls import reverse

    from tests.test_views import login

    login(client, searchable)
    for name in ("contacts:attention", "contacts:triage"):
        body = client.get(reverse(name)).content.decode()
        assert 'name="q"' in body, name
        assert "field-icon" in body, name
