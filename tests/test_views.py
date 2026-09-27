"""The interface, checked against the access rules rather than against itself.

Two things matter here and the rest is decoration:

* **Row scope.** Every page that lists or resolves an account must go through
  ``Ability.accounts()``. A view that forgets returns a colleague's client, and
  no amount of correct template code makes that safe.
* **The ACM health rule.** An ACM owns the commercial renewal and must never see
  a health signal, including the existence of a health alert. That is checked on
  every surface an ACM can reach, because it only takes one page to leak it.

Views are exercised through the real URL conf and the real templates: a test
that called the view function directly would not catch a template asking the
presenter for something it may not have.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from django.urls import reverse

from csm.contacts.models import Contact
from csm.core.enums import HealthStatus, Level, TaskStatus, TaskType
from csm.tasks.models import Task
from tests.factories import AccountFactory, ActivityFactory, ContactFactory, UserFactory

PASSWORD = "test-password-9876"


@pytest.fixture
def people(db):
    users = {
        "csm": UserFactory(csm=True, email="view-csm@dphish.com", full_name="Casey Morgan"),
        "senior": UserFactory(
            senior_csm=True, email="view-senior@dphish.com", full_name="Sam Rivers"
        ),
        "acm": UserFactory(acm=True, email="view-acm@dphish.com", full_name="Alex Chen"),
        "manager": UserFactory(
            manager=True, email="view-manager@dphish.com", full_name="Morgan Lee"
        ),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def portfolio(people):
    """One account this CSM and ACM share, one belonging to somebody else."""
    other_csm = UserFactory(csm=True, email="view-other@dphish.com")
    mine = AccountFactory(
        client_code="MINE",
        client_name="Acme Industrial",
        csm=people["csm"],
        acm=people["acm"],
        effective_health_status=HealthStatus.AT_RISK,
        computed_health_status=HealthStatus.AT_RISK,
        computed_health_score=42,
        computed_health_max=100,
    )
    theirs = AccountFactory(
        client_code="THEIRS",
        client_name="Zenith Holdings",
        csm=other_csm,
        acm=UserFactory(acm=True, email="view-otheracm@dphish.com"),
        effective_health_status=HealthStatus.HEALTHY,
    )
    return {"mine": mine, "theirs": theirs}


def login(client, user):
    assert client.login(username=user.email, password=PASSWORD)
    return client


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("dashboard:index", {}),
        ("accounts:list", {}),
        ("contacts:attention", {}),
        ("tasks:list", {}),
    ],
)
def test_every_page_requires_signing_in(client, db, name, kwargs):
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code == 302
    assert reverse("users:login") in response.url


# ---------------------------------------------------------------------------
# Row scope
# ---------------------------------------------------------------------------


def test_the_account_list_shows_only_this_csms_accounts(client, portfolio, people):
    login(client, people["csm"])
    response = client.get(reverse("accounts:list"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Acme Industrial" in body
    assert "Zenith Holdings" not in body


def test_a_senior_csm_sees_every_account(client, portfolio, people):
    login(client, people["senior"])
    body = client.get(reverse("accounts:list")).content.decode()
    assert "Acme Industrial" in body
    assert "Zenith Holdings" in body


def test_search_does_not_leak_a_colleagues_client(client, portfolio, people):
    """The search bar is the easiest place to defeat row scoping by accident."""
    login(client, people["csm"])
    body = client.get(reverse("accounts:list"), {"q": "Zenith"}).content.decode()
    assert "Zenith Holdings" not in body


def test_fetching_a_colleagues_account_by_id_is_a_404(client, portfolio, people):
    """404 rather than 403: a 403 would confirm the account exists."""
    login(client, people["csm"])
    response = client.get(reverse("accounts:detail", args=[portfolio["theirs"].pk]))
    assert response.status_code == 404


def test_the_attention_list_is_scoped_to_the_users_accounts(client, portfolio, people):
    ContactFactory(
        account=portfolio["mine"], full_name="Mine Contact",
        influence=Level.HIGH, interest=Level.HIGH,
    )
    ContactFactory(
        account=portfolio["theirs"], full_name="Their Contact",
        influence=Level.HIGH, interest=Level.HIGH,
    )
    login(client, people["csm"])
    body = client.get(reverse("contacts:attention")).content.decode()
    assert "Mine Contact" in body
    assert "Their Contact" not in body


def test_a_contact_on_a_colleagues_account_cannot_be_opened(client, portfolio, people):
    contact = ContactFactory(account=portfolio["theirs"], full_name="Their Contact")
    login(client, people["csm"])
    assert client.get(reverse("contacts:edit", args=[contact.pk])).status_code == 404


# ---------------------------------------------------------------------------
# The ACM health rule
# ---------------------------------------------------------------------------


def test_an_acm_sees_no_health_on_the_account_list(client, portfolio, people):
    login(client, people["acm"])
    body = client.get(reverse("accounts:list")).content.decode()
    assert "Acme Industrial" in body, "the ACM should still see their own account"
    assert "At Risk" not in body
    assert "health-dot" not in body


def test_an_acm_sees_no_health_on_the_account_page(client, portfolio, people):
    login(client, people["acm"])
    body = client.get(reverse("accounts:detail", args=[portfolio["mine"].pk])).content.decode()
    assert "Acme Industrial" in body
    assert "At Risk" not in body
    assert "health-dot" not in body


def test_an_acm_is_refused_the_health_panel_outright(client, portfolio, people):
    login(client, people["acm"])
    response = client.get(reverse("health:panel", args=[portfolio["mine"].pk]))
    assert response.status_code == 403


def test_an_acm_cannot_filter_the_list_by_health(client, portfolio, people):
    """Otherwise the result count leaks the status the filter names."""
    AccountFactory(
        client_code="SECOND", client_name="Beta Systems",
        csm=people["csm"], acm=people["acm"],
        effective_health_status=HealthStatus.HEALTHY,
    )
    login(client, people["acm"])
    body = client.get(
        reverse("accounts:list"), {"status": HealthStatus.AT_RISK}
    ).content.decode()
    # The filter is ignored rather than applied, so both accounts remain.
    assert "Acme Industrial" in body
    assert "Beta Systems" in body


def test_an_acm_cannot_sort_the_list_by_health(client, portfolio, people):
    """Otherwise the row order leaks the distribution the filter would not."""
    login(client, people["acm"])
    response = client.get(reverse("accounts:list"), {"sort": "health"})
    assert response.status_code == 200

    # The ORDER BY only: every column is in the SELECT list regardless, and the
    # question is what the rows are arranged by.
    ordering = str(response.context["object_list"].query).split("ORDER BY")[-1]
    assert "effective_health_status" not in ordering
    assert "computed_health_score" not in ordering
    assert "client_name" in ordering, "it falls back to the name, not to nothing"


def test_an_acm_never_sees_a_health_alert_task(client, portfolio, people):
    Task.objects.create(
        account=portfolio["mine"],
        task_type=TaskType.AUTO_HEALTH_ALERT,
        title="Health alert: Acme Industrial",
        due_date=dt.date(2026, 9, 21),
    )
    login(client, people["acm"])
    body = client.get(reverse("tasks:list"), {"scope": "all"}).content.decode()
    assert "Health alert" not in body


def test_an_acm_is_not_offered_the_health_alert_type_filter(client, portfolio, people):
    login(client, people["acm"])
    response = client.get(reverse("tasks:list"))
    offered = [value for value, _ in response.context["task_types"]]
    assert TaskType.AUTO_HEALTH_ALERT not in offered


def test_a_csm_does_see_health(client, portfolio, people):
    """The mirror of the ACM tests: the gate is a gate, not a blanket."""
    login(client, people["csm"])
    body = client.get(reverse("accounts:detail", args=[portfolio["mine"].pk])).content.decode()
    assert "At Risk" in body


# ---------------------------------------------------------------------------
# The contact preview, and raising a task from it
# ---------------------------------------------------------------------------


@pytest.fixture
def stakeholder(portfolio, people):
    return ContactFactory(
        account=portfolio["mine"],
        full_name="Dana Farouk",
        title="Head of Security",
        email="dana@acme.test",
        influence=Level.HIGH,
        interest=Level.HIGH,
    )


def test_a_contact_name_opens_the_preview_not_the_editor(client, stakeholder, people):
    """Most of the time the question is "who is this and when did we last
    speak", not "change their job title". The name itself still opens the
    preview; a row's own three-dot menu is where editing (and deleting) live,
    the same split the account list already uses."""
    login(client, people["csm"])
    detail = reverse("contacts:detail", args=[stakeholder.pk])

    for page in ("contacts:attention", "contacts:triage", "dashboard:index"):
        body = client.get(reverse(page)).content.decode()
        if stakeholder.full_name not in body:
            continue
        assert detail in body, page
        name_anchor = re.search(
            rf'<a (?:data-modal )?href="([^"]+)"[^>]*>\s*{re.escape(stakeholder.full_name)}', body
        )
        assert name_anchor, page
        assert name_anchor.group(1) == detail, page


def test_the_preview_shows_who_they_are_without_a_form(client, stakeholder, people):
    login(client, people["csm"])
    body = client.get(reverse("contacts:detail", args=[stakeholder.pk])).content.decode()

    assert "Dana Farouk" in body
    assert "Head of Security" in body
    assert "Manage closely" in body, "their place on the grid is the point of the page"
    assert "Acme Industrial" in body


def test_a_colleagues_contact_is_a_404_not_a_403(client, portfolio, people):
    """Same rule as accounts: a 403 would confirm the contact exists."""
    theirs = ContactFactory(account=portfolio["theirs"], full_name="Not Yours")
    login(client, people["csm"])
    assert client.get(reverse("contacts:detail", args=[theirs.pk])).status_code == 404


def test_a_task_can_be_raised_from_the_contact_page(client, stakeholder, people):
    """A link to the real task view, with this contact preselected - not a
    second, simpler form embedded on the page that would drift from it."""
    login(client, people["csm"])
    page = reverse("contacts:detail", args=[stakeholder.pk])
    body = client.get(page).content.decode()
    assert f"contact={stakeholder.pk}" in body

    response = client.post(
        reverse("tasks:create", args=[stakeholder.account_id]),
        {
            "title": "Call Dana about the renewal",
            "due_date": dt.date.today().isoformat(),
            "due_time": "", "category": "CLIENT_FACING", "priority": "HIGH", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION",
            "description": "", "contact": str(stakeholder.pk), "next": page,
        },
    )

    task = Task.objects.get(title="Call Dana about the renewal")
    assert task.contact_id == stakeholder.pk
    assert task.account_id == stakeholder.account_id
    assert response.status_code == 302
    assert response["Location"] == page, "and it comes back to the contact"


def test_the_full_task_form_preselects_the_contact_it_was_sent(client, stakeholder, people):
    login(client, people["csm"])
    response = client.get(
        reverse("tasks:create", args=[stakeholder.account_id]),
        {"contact": stakeholder.pk},
    )
    assert response.context["form"].initial["contact"] == str(stakeholder.pk)


def test_a_contact_from_another_account_cannot_be_bound_to_this_task(
    client, portfolio, stakeholder, people
):
    """The form's choices are this account's contacts, so an id from anywhere
    else does not match one - it does not quietly attach somebody else's
    stakeholder to this account's task."""
    outsider = ContactFactory(account=portfolio["theirs"], full_name="Outsider")
    login(client, people["csm"])

    response = client.post(
        reverse("tasks:create", args=[stakeholder.account_id]),
        {
            "title": "Should not bind", "due_date": dt.date.today().isoformat(),
            "due_time": "", "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION",
            "description": "", "contact": str(outsider.pk),
        },
    )
    assert response.status_code == 200, "re-rendered with an error, not saved"
    assert not Task.objects.filter(title="Should not bind").exists()


def test_the_return_address_cannot_leave_this_site(client, stakeholder, people):
    """``next`` arrives in the request, and an unchecked one is an open
    redirect: a link that looks like this application and lands on somebody
    else's login page."""
    login(client, people["csm"])
    response = client.post(
        reverse("tasks:create", args=[stakeholder.account_id]),
        {
            "title": "Somewhere else", "due_date": dt.date.today().isoformat(),
            "due_time": "", "category": "INTERNAL", "priority": "MEDIUM", "progress": "NOT_STARTED", "task_type": "MANUAL_NEXT_ACTION",
            "description": "", "contact": "", "next": "https://evil.example/harvest",
        },
    )
    assert response["Location"] == reverse("accounts:detail", args=[stakeholder.account_id])


def test_an_inactive_contact_is_not_offered_a_task_form(client, portfolio, people):
    """The form's choices are the account's *active* contacts, so the hidden
    value would be rejected on submit with nothing on screen to explain it."""
    retired = ContactFactory(
        account=portfolio["mine"], full_name="Gone Already", is_active=False
    )
    login(client, people["csm"])
    body = client.get(reverse("contacts:detail", args=[retired.pk])).content.decode()

    assert "Gone Already" in body
    assert "Add a task for" not in body


# ---------------------------------------------------------------------------
# The seat count, on the tables that list accounts
# ---------------------------------------------------------------------------


def test_the_account_tables_show_the_contracted_seats(client, people):
    """Both of them, because they are one partial.

    The total users figure is everywhere already and cannot say which part of
    it was signed for, which is the number a renewal conversation turns on.
    """
    AccountFactory(
        client_code="SEATS", client_name="Seat Counter", csm=people["csm"],
        boq_users_count=750, added_users_count=30,
    )
    login(client, people["csm"])

    for page in ("accounts:list", "dashboard:analytics"):
        body = client.get(reverse(page)).content.decode()
        assert ">BOQ<" in body, page
        assert "750" in body, page
        assert "+30" in body, page


# ---------------------------------------------------------------------------
# Who may be named as an account's CSM
# ---------------------------------------------------------------------------


def test_a_csm_can_only_put_an_account_in_their_own_name(client, portfolio, people):
    """Creating an account is taking it on, not handing it out."""
    from csm.accounts.forms import AccountForm
    from csm.core.access.abilities import Ability

    form = AccountForm(ability=Ability(people["csm"]))
    offered = [u.email for u in form.fields["csm"].queryset]

    assert offered == [people["csm"].email]


def test_a_senior_may_name_whoever_should_own_it(client, portfolio, people):
    """Anyone who can see the whole book may assign the whole book."""
    from csm.accounts.forms import AccountForm
    from csm.core.access.abilities import Ability

    form = AccountForm(ability=Ability(people["senior"]))
    offered = {u.email for u in form.fields["csm"].queryset}

    assert people["csm"].email in offered
    assert people["senior"].email in offered


def test_the_current_holder_stays_selectable(client, portfolio, people):
    """Otherwise editing an unrelated field on their account fails validation on
    a value the form did not offer and the editor never touched."""
    from csm.accounts.forms import AccountForm
    from csm.core.access.abilities import Ability

    account = portfolio["mine"]
    form = AccountForm(instance=account, ability=Ability(people["csm"]))
    offered = {u.pk for u in form.fields["csm"].queryset}

    assert account.csm_id in offered


# ---------------------------------------------------------------------------
# The row-actions menu on the account list
# ---------------------------------------------------------------------------


def menu_links(body: str, account) -> set:
    """Every link the row menus offer, unescaped."""
    import re

    return {
        href.replace("&amp;", "&")
        for href in re.findall(r'class="dropdown-item[^"]*"\s+href="([^"]+)"', body)
    } | {
        href.replace("&amp;", "&")
        for href in re.findall(r'class="dropdown-item[^"]*"\n\s+href="([^"]+)"', body)
    }


def test_a_senior_csm_is_offered_every_action_on_a_row(client, portfolio, people):
    """Eight actions, each one a route that already existed; the menu is only
    the way in."""
    from django.urls import reverse as url

    login(client, people["senior"])
    body = client.get(reverse("accounts:list")).content.decode()
    account = portfolio["mine"]

    for route in (
        url("tasks:create", args=[account.pk]),
        url("tasks:create", args=[account.pk]) + "?task_type=MEETING",
        url("engagement:ticket_create", args=[account.pk]),
        url("engagement:adoption_create", args=[account.pk]),
        url("engagement:csat_create", args=[account.pk]),
        url("contacts:create", args=[account.pk]),
        url("accounts:edit", args=[account.pk]),
        url("accounts:delete", args=[account.pk]),
    ):
        assert route in body, route


def test_the_menu_offers_only_what_the_reader_may_do(client, portfolio, people):
    """Eight separate capabilities, gated one at a time.

    An ACM holds none of ticket.edit, csat.record or adoption.record, so their
    menu is short rather than eight items with several that refuse them.
    """
    from django.urls import reverse as url

    account = portfolio["mine"]
    login(client, people["acm"])
    body = client.get(reverse("accounts:list")).content.decode()

    assert url("engagement:csat_create", args=[account.pk]) not in body
    assert url("engagement:ticket_create", args=[account.pk]) not in body
    assert url("engagement:adoption_create", args=[account.pk]) not in body
    assert url("accounts:edit", args=[account.pk]) not in body
    assert url("accounts:delete", args=[account.pk]) not in body


def test_a_reader_who_can_do_nothing_gets_no_menu_button(client, portfolio, people):
    """A manager reads everything and changes nothing. A button that opens an
    empty list promises something and then refuses."""
    login(client, people["manager"])
    body = client.get(reverse("accounts:list")).content.decode()

    assert "Acme Industrial" in body
    assert "bi-three-dots-vertical" not in body


def test_every_action_carries_the_way_back_to_the_list(client, portfolio, people):
    """With the filters and the sort on it.

    Adding a ticket from row seven of a filtered list and landing on the
    account page loses your place, and the filter has to be typed again.
    """
    login(client, people["senior"])
    listing = reverse("accounts:list") + "?stage=ACTIVE&sort=-boq"
    body = client.get(listing).content.decode()

    # The filter and the sort travel with it, encoded. Django's urlencode
    # filter leaves "/" alone by design, which is why the path is legible here.
    assert "next=/accounts/%3Fstage%3DACTIVE%26sort%3D-boq" in body


def test_an_action_returns_to_the_list_it_was_started_from(client, portfolio, people):
    """The round trip itself, through the real activity form."""
    import datetime as dt

    account = portfolio["mine"]
    listing = reverse("accounts:list") + "?stage=ACTIVE"
    login(client, people["csm"])

    response = client.post(
        reverse("activities:create", args=[account.pk]),
        {
            "activity_type": "MEETING",
            "occurred_on": dt.date.today().isoformat(),
            "subject": "Kick-off",
            "notes": "",
            "contact": "",
            "next": listing,
        },
    )
    assert response.status_code == 302
    assert response["Location"] == listing
    assert account.activities.filter(subject="Kick-off").exists()


def test_an_action_cannot_be_sent_somewhere_off_this_site(client, portfolio, people):
    import datetime as dt

    account = portfolio["mine"]
    login(client, people["csm"])
    response = client.post(
        reverse("activities:create", args=[account.pk]),
        {
            "activity_type": "MEETING",
            "occurred_on": dt.date.today().isoformat(),
            "subject": "Redirected",
            "notes": "",
            "contact": "",
            "next": "https://evil.example/harvest",
        },
    )
    assert "evil.example" not in response["Location"]


def test_deleting_from_the_menu_goes_through_the_confirmation(client, portfolio, people):
    """The menu links to the confirmation page, which says what survives.

    A one-line "are you sure?" cannot name the contacts, activities, tasks and
    score history that are kept, and is the kind of dialog people dismiss
    without reading.
    """
    account = portfolio["mine"]
    login(client, people["senior"])

    page = client.get(reverse("accounts:delete", args=[account.pk]))
    body = page.content.decode()

    assert page.status_code == 200
    assert "This hides the account everywhere" in body
    assert "can be undone" in body or "restored" in body or "kept" in body
    account.refresh_from_db()
    assert account.deleted_at is None, "opening the page deletes nothing"


def test_the_delete_confirmation_arrives_as_a_dialog(client, portfolio, people):
    """Asked by HTMX, the same view answers with the inside of a modal.

    One view serves both, so the dialog and the page cannot drift into saying
    different things about what a deletion does - which is the only part of
    this that matters.
    """
    account = portfolio["mine"]
    login(client, people["senior"])
    url = reverse("accounts:delete", args=[account.pk])

    dialog = client.get(url, headers={"hx-request": "true"}).content.decode()
    page = client.get(url).content.decode()

    assert "<html" not in dialog, "a fragment, not a page"
    assert "modal-header" in dialog and "modal-footer" in dialog
    for both in ("This hides the account everywhere", "can be undone", account.client_name):
        assert both in dialog, both
        assert both in page, both

    account.refresh_from_db()
    assert account.deleted_at is None, "opening the dialog deletes nothing"


def test_the_delete_link_still_works_without_javascript(client, portfolio, people):
    """It is a link carrying hx-get, not a button.

    If the CDN is slow the href still opens the confirmation page. Losing the
    ability to delete an account because a script did not load is not a trade
    worth making for a pop-up.
    """
    account = portfolio["mine"]
    login(client, people["senior"])
    body = client.get(reverse("accounts:list")).content.decode()

    delete_url = reverse("accounts:delete", args=[account.pk])
    assert f'href="{delete_url}' in body
    assert f'hx-get="{delete_url}' in body
    assert client.get(delete_url).status_code == 200


def test_administration_lives_on_the_settings_page(client, portfolio, people):
    """Import and Staff are occasional and deliberate.

    Beside Accounts and Tasks they carried the same weight as the pages people
    live in all day. Both are still capability-gated; only where they sit
    changed - and the gear now opens a page rather than dropping a menu, which
    made the reader choose before they could see the choices.
    """
    login(client, people["senior"])
    home = client.get(reverse("dashboard:index")).content.decode()
    assert reverse("settings") in home
    assert reverse("importer:list") not in home, "not in the bar itself"

    page = client.get(reverse("settings")).content.decode()
    assert reverse("importer:list") in page


def test_the_settings_page_shows_only_what_the_reader_can_do(client, portfolio, people):
    """A CSM imports but does not manage staff - and everybody has a calendar
    and a clock, so the page always has a "Yours" half for them."""
    login(client, people["csm"])
    page = client.get(reverse("settings")).content.decode()

    assert reverse("schedule:settings") in page
    assert reverse("users:display_settings") in page
    assert reverse("importer:list") in page
    assert reverse("importer:export") in page
    assert reverse("users:staff_list") not in page


def test_the_settings_page_holds_only_what_the_reader_may_reach(client, portfolio, people):
    """A senior CSM imports and exports but does not manage staff."""
    login(client, people["senior"])
    page = client.get(reverse("settings")).content.decode()

    assert reverse("importer:list") in page
    assert reverse("importer:export") in page
    assert reverse("users:staff_list") not in page


def test_an_acm_sees_import_and_export_on_settings(client, portfolio, people):
    login(client, people["acm"])
    page = client.get(reverse("settings")).content.decode()

    assert reverse("importer:list") in page
    assert reverse("importer:export") in page


def test_row_menus_are_positioned_against_the_viewport(client, portfolio, people):
    """Otherwise the menu is cut off by the table it sits in.

    The account table lives in a scroll container with `overflow: hidden` -
    that is what gives it rounded corners and horizontal scrolling - and a menu
    that overflows the container is clipped by it, which is what happened on a
    search result with one row. Positioning the menu against the viewport lets
    it escape without giving up either.

    Asserted because the fix is one line of configuration that looks like
    nothing and would be dropped by anyone tidying the page.
    """
    login(client, people["senior"])
    body = client.get(reverse("accounts:list")).content.decode()

    assert 'strategy: "fixed"' in body
    assert "data-bs-toggle=\"dropdown\"" in body


def test_the_page_carries_one_dialog_not_one_per_row(client, portfolio, people):
    """Twenty-five modals would mean counting every account's contacts,
    activities and tasks to answer a question nobody has asked yet."""
    login(client, people["senior"])
    body = client.get(reverse("accounts:list")).content.decode()
    assert body.count('id="confirm-modal"') == 1


def test_the_delete_confirmation_returns_to_the_list_you_came_from(
    client, portfolio, people
):
    account = portfolio["mine"]
    listing = reverse("accounts:list") + "?stage=ACTIVE"
    login(client, people["senior"])

    response = client.post(reverse("accounts:delete", args=[account.pk]), {"next": listing})

    account.refresh_from_db()
    assert account.deleted_at is not None
    assert response["Location"] == listing


def test_the_analytics_roster_has_no_row_menu(client, portfolio, people):
    """It is a report you read. A delete beside a chart is a misclick."""
    login(client, people["senior"])
    roster = client.get(reverse("dashboard:analytics")).content.decode()
    roster = roster.split("Accounts in this view")[1]

    assert "Acme Industrial" in roster
    assert "bi-three-dots-vertical" not in roster


# ---------------------------------------------------------------------------
# The renewal-quarter filter, shared with the analytics page
# ---------------------------------------------------------------------------


def test_the_account_list_filters_by_renewal_quarter(client, people):
    """The same control the analytics page offers, reading the same values.

    Both pages go through ``accounts.filters``: written twice, one page would
    end up spelling the quarter differently from the other, and a link copied
    between them would silently filter nothing.
    """
    early = AccountFactory(
        client_code="EARLY", client_name="Early Renewal", csm=people["csm"],
        contract_end_date=dt.date(2027, 2, 1),
    )
    AccountFactory(
        client_code="LATE", client_name="Late Renewal", csm=people["csm"],
        contract_end_date=dt.date(2027, 10, 1),
    )
    AccountFactory(
        client_code="NEVER", client_name="No Contract End", csm=people["csm"],
        contract_end_date=None,
    )

    login(client, people["csm"])
    response = client.get(reverse("accounts:list"), {"renewal": "2027-1"})
    assert [a.client_code for a in response.context["object_list"]] == [early.client_code]

    # Offered as concrete quarters, drawn from what this user actually holds.
    assert ("2027-1", "Q1 2027") in response.context["renewal_quarters"]
    assert ("2027-4", "Q4 2027") in response.context["renewal_quarters"]


def test_a_renewal_quarter_filter_is_scoped_like_everything_else(client, portfolio, people):
    """The options say which quarters *this* reader has renewals in."""
    AccountFactory(
        client_code="FARFUTURE", client_name="Somebody Else's",
        csm=UserFactory(csm=True, email="view-far@dphish.com"),
        contract_end_date=dt.date(2031, 7, 1),
    )
    login(client, people["csm"])
    offered = client.get(reverse("accounts:list")).context["renewal_quarters"]
    assert "2031-3" not in [value for value, _ in offered]


# ---------------------------------------------------------------------------
# The override, shown where the status is shown
# ---------------------------------------------------------------------------


@pytest.fixture
def overridden(portfolio, people):
    """Acme, overridden to Watch by the senior CSM against a computed At Risk.

    The computed status is read back rather than set up: ``apply_override``
    recalculates first, so anything written here by hand is overwritten before
    the override exists. The assert guards the premise - if the engine ever
    returns Watch for this factory the two statuses would agree and the test
    would pass while checking nothing.
    """
    from csm.health.services import apply_override

    account = portfolio["mine"]
    apply_override(
        account,
        status=HealthStatus.WATCH.value,
        reason="Escalated to their CISO, who owns it now; the number lags reality.",
        actor=people["senior"],
    )
    account.refresh_from_db()
    assert account.computed_health_status != account.effective_health_status
    return account


def test_the_account_page_says_a_status_was_set_by_hand(client, overridden, people):
    """A hand-set status and a calculated one are the same pill.

    Without this the account page showed At Risk with no hint that a person put
    it there, and the reason - the only record of why the number was set aside -
    lived one click away on the panel.
    """
    login(client, people["csm"])
    body = client.get(reverse("accounts:detail", args=[overridden.pk])).content.decode()

    computed = HealthStatus(overridden.computed_health_status).label

    assert "Manually overridden to Watch" in body
    assert "Escalated to their CISO" in body
    assert "Sam Rivers" in body, "who set it is part of the governance trail"
    assert f"The engine currently says {computed}" in body


def test_the_account_page_is_quiet_when_nothing_is_overridden(client, portfolio, people):
    login(client, people["csm"])
    body = client.get(reverse("accounts:detail", args=[portfolio["mine"].pk])).content.decode()
    assert "Manually overridden" not in body


def test_an_acm_sees_no_trace_of_an_override(client, overridden, people):
    """An override is a health status. The rule does not soften for it."""
    login(client, people["acm"])
    body = client.get(reverse("accounts:detail", args=[overridden.pk])).content.decode()

    assert "Acme Industrial" in body
    assert "Manually overridden" not in body
    assert "Overridden" not in body
    assert "CISO" not in body


def test_the_presenter_refuses_the_override_without_health_view(portfolio, acm_user):
    from django.core.exceptions import PermissionDenied

    from csm.core.access.abilities import Ability
    from csm.core.access.presenters import AccountPresenter

    presenter = AccountPresenter(portfolio["mine"], Ability(acm_user))
    with pytest.raises(PermissionDenied):
        _ = presenter.health_override


def test_only_a_user_who_may_override_is_offered_the_action(client, portfolio, people):
    login(client, people["csm"])
    url = reverse("accounts:detail", args=[portfolio["mine"].pk])
    assert client.get(url).context["can_override"] is True

    login(client, people["manager"])
    assert client.get(url).context["can_override"] is False


# ---------------------------------------------------------------------------
# Field scope
# ---------------------------------------------------------------------------


def test_an_acm_cannot_reach_the_edit_form_at_all(client, portfolio, people):
    login(client, people["acm"])
    response = client.get(reverse("accounts:edit", args=[portfolio["mine"].pk]))
    assert response.status_code == 403


def test_a_manager_cannot_reach_the_edit_form_at_all(client, portfolio, people):
    login(client, people["manager"])
    response = client.get(reverse("accounts:edit", args=[portfolio["mine"].pk]))
    assert response.status_code == 403


def test_an_acm_cannot_edit_the_account_by_posting_directly(client, portfolio, people):
    """The view is capability-gated, not merely field-gated: a direct POST
    still refuses, even with no field absent from any form to blame."""
    account = portfolio["mine"]
    login(client, people["acm"])
    response = client.post(
        reverse("accounts:edit", args=[account.pk]),
        {"renewal_count": 3, "lifecycle_stage": "CHURNED"},
    )
    assert response.status_code == 403
    account.refresh_from_db()
    assert account.lifecycle_stage != "CHURNED"


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------


def test_the_account_page_renders_the_nine_cell_grid(client, portfolio, people):
    ContactFactory(
        account=portfolio["mine"], full_name="Medium Person",
        influence=Level.MEDIUM, interest=Level.MEDIUM,
    )
    login(client, people["csm"])
    response = client.get(reverse("accounts:detail", args=[portfolio["mine"].pk]))
    rows = response.context["rows"]

    assert len(rows) == 3
    assert all(len(row.cells) == 3 for row in rows)
    # Medium/Medium is Keep satisfied, not Manage closely.
    body = response.content.decode()
    assert "Medium Person" in body
    assert "Keep satisfied" in body


def test_unassessed_contacts_are_offered_for_triage_not_plotted(client, portfolio, people):
    ContactFactory(
        account=portfolio["mine"], full_name="Unknown Person",
        influence=None, interest=None,
    )
    login(client, people["csm"])
    response = client.get(reverse("accounts:detail", args=[portfolio["mine"].pk]))

    assert response.context["triage_count"] == 1
    plotted = [c for row in response.context["rows"] for cell in row.cells for c in cell.contacts]
    assert plotted == []
    assert "Needs triage" in response.content.decode()


def test_triage_assigns_a_position_and_returns_the_new_row(client, portfolio, people):
    contact = ContactFactory(
        account=portfolio["mine"], full_name="Unknown Person",
        influence=None, interest=None,
    )
    login(client, people["csm"])
    response = client.post(
        reverse("contacts:assess", args=[contact.pk]),
        {"influence": Level.MEDIUM, "interest": Level.MEDIUM},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    contact.refresh_from_db()
    assert contact.quadrant == "KEEP_SATISFIED"
    assert "Keep satisfied" in response.content.decode()


def test_a_manager_cannot_triage(client, portfolio, people):
    contact = ContactFactory(account=portfolio["mine"], influence=None, interest=None)
    login(client, people["manager"])
    response = client.post(
        reverse("contacts:assess", args=[contact.pk]),
        {"influence": Level.HIGH, "interest": Level.HIGH},
    )
    assert response.status_code == 403
    contact.refresh_from_db()
    assert contact.influence is None


# ---------------------------------------------------------------------------
# Activity and tasks
# ---------------------------------------------------------------------------


def test_logging_an_activity_records_the_author(client, portfolio, people):
    account = portfolio["mine"]
    login(client, people["csm"])
    response = client.post(
        reverse("activities:create", args=[account.pk]),
        {
            "activity_type": "CALL",
            "occurred_on": dt.date.today().isoformat(),
            "subject": "Quarterly catch-up",
            "notes": "Went well.",
            "contact": "",
        },
    )
    assert response.status_code == 302
    activity = account.activities.get()
    assert activity.created_by == people["csm"]
    assert activity.is_voided is False


def test_an_activity_cannot_be_logged_in_the_future(client, portfolio, people):
    account = portfolio["mine"]
    login(client, people["csm"])
    response = client.post(
        reverse("activities:create", args=[account.pk]),
        {
            "activity_type": "CALL",
            "occurred_on": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
            "subject": "Time travel",
            "contact": "",
        },
    )
    assert response.status_code == 200
    assert account.activities.count() == 0


def test_voiding_keeps_the_row_and_records_why(client, portfolio, people):
    activity = ActivityFactory(account=portfolio["mine"], created_by=people["csm"])
    login(client, people["csm"])
    response = client.post(
        reverse("activities:void", args=[activity.pk]), {"void_reason": "Logged twice"}
    )

    assert response.status_code == 302
    activity.refresh_from_db()
    assert activity.is_voided is True
    assert activity.void_reason == "Logged twice"
    assert activity.voided_by == people["csm"]


def test_completing_a_task_goes_through_the_service(client, portfolio, people):
    task = Task.objects.create(
        account=portfolio["mine"],
        task_type=TaskType.MANUAL_NEXT_ACTION,
        title="Send the renewal quote",
        due_date=dt.date.today(),
    )
    login(client, people["csm"])
    client.post(reverse("tasks:complete", args=[task.pk]))

    task.refresh_from_db()
    assert task.status == TaskStatus.DONE
    # The service sets this; a view writing the status column would not.
    assert task.completed_at is not None
    assert task.completed_by == people["csm"]


def test_a_new_task_cannot_already_be_overdue(client, portfolio, people):
    login(client, people["csm"])
    response = client.post(
        reverse("tasks:create", args=[portfolio["mine"].pk]),
        {
            "title": "Late already",
            "due_date": (dt.date.today() - dt.timedelta(days=1)).isoformat(),
            "category": "INTERNAL",
            "description": "",
            "contact": "",
        },
    )
    assert response.status_code == 200
    assert not Task.objects.filter(title="Late already").exists()


# ---------------------------------------------------------------------------
# Smoke: every page renders for every role it is meant to serve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["csm", "senior", "acm", "manager"])
def test_the_dashboard_renders_for_every_role(client, portfolio, people, role):
    login(client, people[role])
    assert client.get(reverse("dashboard:index")).status_code == 200


@pytest.mark.parametrize("role", ["csm", "senior", "acm", "manager"])
def test_the_accounts_tile_on_my_day_opens_the_account_list(client, portfolio, people, role):
    """Every other tile on the row links somewhere; a count you cannot click
    through to is the one the eye keeps landing on."""
    login(client, people[role])
    body = client.get(reverse("dashboard:index")).content.decode()
    tile = body[body.index(">Accounts</div>"):]
    tile = tile[: tile.index("</div></div>")]
    assert f'class="stretched-link small" href="{reverse("accounts:list")}"' in tile


@pytest.mark.parametrize("role", ["csm", "senior", "acm", "manager"])
def test_the_account_page_renders_for_every_role(client, portfolio, people, role):
    account = portfolio["mine"] if role != "manager" else portfolio["mine"]
    login(client, people[role])
    response = client.get(reverse("accounts:detail", args=[account.pk]))
    assert response.status_code == 200


def test_the_health_panel_renders_with_no_history(client, portfolio, people):
    login(client, people["csm"])
    response = client.get(reverse("health:panel", args=[portfolio["mine"].pk]))
    assert response.status_code == 200
    assert "No score has been calculated" in response.content.decode()


def test_the_triage_page_renders(client, portfolio, people):
    Contact.objects.create(account=portfolio["mine"], full_name="Nobody Assessed")
    login(client, people["csm"])
    response = client.get(reverse("contacts:triage"))
    assert response.status_code == 200
    assert "Nobody Assessed" in response.content.decode()


# ---------------------------------------------------------------------------
# Template hygiene
# ---------------------------------------------------------------------------


def test_no_template_uses_a_multiline_hash_comment():
    """``{# #}`` is single-line only; a multi-line one renders as page text.

    This is not hypothetical. A multi-line ``{# #}`` explaining the N/A branch
    of the health panel was printing its own text, including the words it used
    as an example, onto the page. Multi-line commentary belongs in
    ``{% comment %}``.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "templates"
    offenders = []
    for path in root.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\{#(.*?)#\}", source, re.S):
            if "\n" in match.group(1):
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        "These templates use a multi-line {# #}, which Django renders as text. "
        f"Use {{% comment %}} instead: {offenders}"
    )


def test_the_search_bar_carries_a_magnifier(client, portfolio, people):
    login(client, people["csm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    assert "bi-search" in body
    assert "field-icon" in body
    # Decoration, not a control: it must not be announced, and Enter already
    # submits, so there is no second button to tab onto.
    assert '<i class="bi bi-search" aria-hidden="true"></i>' in body


def test_the_search_still_works_with_the_icon_in_place(client, portfolio, people):
    login(client, people["csm"])
    response = client.get(reverse("accounts:list"), {"q": "Acme"})

    codes = {a.client_code for a in response.context["object_list"]}
    assert codes == {"MINE"}


def test_both_search_boxes_use_the_same_icon_pattern(client, portfolio, people):
    """One class, so the bar and the filter card cannot drift apart."""
    login(client, people["csm"])
    body = client.get(reverse("accounts:list")).content.decode()

    assert body.count("field-icon") >= 2
    assert body.count('<i class="bi bi-search" aria-hidden="true"></i>') == 2


# ---------------------------------------------------------------------------
# Quick create
# ---------------------------------------------------------------------------


def test_the_quick_create_menu_offers_all_three_to_a_csm(client, portfolio, people):
    login(client, people["csm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    assert "app-create" in body
    assert reverse("tasks:create_anywhere") in body
    assert reverse("contacts:create_anywhere") in body
    assert reverse("accounts:create") in body


def test_the_menu_is_on_every_page_not_just_my_day(client, portfolio, people):
    """The moment you want to add a task is rarely the moment you are on the
    page that offers one."""
    login(client, people["csm"])
    for name in ("accounts:list", "contacts:attention", "tasks:list"):
        assert "app-create" in client.get(reverse(name)).content.decode(), name


def test_an_acm_is_offered_only_what_they_may_create(client, portfolio, people):
    """An ACM edits contacts but creates neither accounts nor, in their own
    right, anything else on the list."""
    login(client, people["acm"])
    body = client.get(reverse("dashboard:index")).content.decode()

    assert reverse("contacts:create_anywhere") in body
    assert reverse("accounts:create") not in body


def test_a_manager_gets_no_create_button_at_all(client, portfolio, people):
    """Read-only, so an empty menu would be worse than no menu."""
    login(client, people["manager"])
    body = client.get(reverse("dashboard:index")).content.decode()

    assert "app-create" not in body
    assert reverse("tasks:create_anywhere") not in body
    assert reverse("contacts:create_anywhere") not in body
    assert reverse("accounts:create") not in body


def test_the_health_panel_shows_each_quarters_csat_average(client, portfolio, people):
    """The number beside the ratings is the one the factor scores."""
    from csm.core.clock import today
    from tests.factories import CsatFactory

    account = portfolio["mine"]
    first = ContactFactory(account=account, full_name="First Rater")
    second = ContactFactory(account=account, full_name="Second Rater")
    CsatFactory(account=account, contact=first, score=5, recorded_on=today())
    CsatFactory(account=account, contact=second, score=2, recorded_on=today())

    login(client, people["csm"])
    response = client.get(reverse("health:panel", args=[account.pk]))

    (latest,) = response.context["csat_quarters"][:1]
    assert str(latest.average) == "3.50"
    assert latest.voices == 2
    assert "3.50" in response.content.decode()
