"""Forms that open in a dialog on the page they were started from.

The server half of csm/core/modal.py: a request aimed at the dialog gets the
page without its chrome, a save becomes "refresh the page behind", and an
error re-renders inside the dialog. Everything else is left exactly as it was.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from csm.contacts.models import Contact
from csm.core.clock import today
from csm.core.modal import MODAL_TARGET
from csm.tasks.models import Task
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login

MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": MODAL_TARGET}


@pytest.fixture
def csm(db):
    user = UserFactory(csm=True, email="modal-csm@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


@pytest.fixture
def account(csm):
    return AccountFactory(client_code="MD01", client_name="Modal Co", csm=csm)


def _task_post(**extra):
    return {
        "task_type": "CALL",
        "title": "Ring the sponsor",
        "due_date": today().isoformat(),
        "category": "CLIENT_FACING",
        "priority": "MEDIUM",
        "progress": "NOT_STARTED",
        **extra,
    }


def test_the_dialog_gets_the_form_without_the_page_around_it(client, csm, account):
    login(client, csm)
    url = reverse("tasks:create", args=[account.pk]) + "?task_type=CALL"
    body = client.get(url, **MODAL).content.decode()

    assert "<html" not in body and "navbar" not in body
    assert "modal-body" in body and 'name="title"' in body
    assert f'data-modal-url="{url}"' in body


def test_the_same_url_is_still_a_whole_page(client, csm, account):
    login(client, csm)
    body = client.get(reverse("tasks:create", args=[account.pk])).content.decode()
    assert "<html" in body and "modal-page" not in body


def test_a_save_in_the_dialog_refreshes_the_page_behind(client, csm, account):
    login(client, csm)
    url = reverse("tasks:create", args=[account.pk])
    response = client.post(url, _task_post(), **MODAL)

    assert response.status_code == 204
    assert response["HX-Refresh"] == "true"
    assert Task.objects.filter(account=account, title="Ring the sponsor").exists()


def test_an_error_in_the_dialog_stays_in_the_dialog(client, csm, account):
    login(client, csm)
    url = reverse("tasks:create", args=[account.pk])
    response = client.post(url, _task_post(title=""), **MODAL)

    assert response.status_code == 200
    body = response.content.decode()
    assert "modal-body" in body and "<html" not in body
    assert "text-danger" in body
    assert not Task.objects.filter(account=account).exists()


def test_other_htmx_requests_are_left_alone(client, csm, account):
    """The delete confirmations and the contact-options fragment also send
    HX-Request; only a request aimed at the dialog is changed."""
    login(client, csm)
    url = reverse("tasks:create", args=[account.pk])
    response = client.post(url, _task_post(), HTTP_HX_REQUEST="true")
    assert response.status_code == 302


def test_a_contact_edit_opens_in_the_dialog(client, csm, account):
    contact = Contact.objects.create(account=account, full_name="Dana Farouk")
    login(client, csm)
    body = client.get(reverse("contacts:edit", args=[contact.pk]), **MODAL).content.decode()
    assert "<html" not in body and "Dana Farouk" in body


@pytest.mark.parametrize(
    "name", ["engagement:csat_create", "engagement:ticket_create", "engagement:adoption_create"]
)
def test_engagement_forms_open_in_the_dialog(client, csm, account, name):
    login(client, csm)
    body = client.get(reverse(name, args=[account.pk]), **MODAL).content.decode()
    assert "<html" not in body and "modal-body" in body


def test_the_links_ask_for_the_dialog(client, csm, account):
    Contact.objects.create(account=account, full_name="Dana Farouk", influence="HIGH", interest="HIGH")
    login(client, csm)
    body = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    for name in ("tasks:create", "engagement:csat_create", "engagement:ticket_create"):
        at = body.index(f'href="{reverse(name, args=[account.pk])}')
        tag = body[body.rindex("<a ", 0, at) : at]
        assert "data-modal" in tag, name


def test_a_contact_opens_in_the_dialog(client, csm, account):
    contact = Contact.objects.create(account=account, full_name="Dana Farouk")
    login(client, csm)
    body = client.get(reverse("contacts:detail", args=[contact.pk]), **MODAL).content.decode()

    assert "<html" not in body and "Dana Farouk" in body
    # Its delete asks in the same dialog rather than stacking a second one.
    delete = reverse("contacts:delete", args=[contact.pk])
    tag = body[body.rindex("<a ", 0, body.index(f'href="{delete}"')) : body.index(f'href="{delete}"') + 200]
    assert "data-modal" in tag and "#confirm-modal" not in tag


def test_the_contact_page_on_its_own_keeps_its_confirmation_dialog(client, csm, account):
    contact = Contact.objects.create(account=account, full_name="Dana Farouk")
    login(client, csm)
    body = client.get(reverse("contacts:detail", args=[contact.pk])).content.decode()
    assert 'data-bs-target="#confirm-modal"' in body


def test_adding_a_contact_happens_in_the_dialog(client, csm, account):
    login(client, csm)
    url = reverse("contacts:create", args=[account.pk])
    assert "<html" not in client.get(url, **MODAL).content.decode()

    response = client.post(url, {"full_name": "New Person", "email": "new.person@client.example"}, **MODAL)
    assert response.status_code == 204
    assert Contact.objects.filter(account=account, full_name="New Person").exists()


def test_contact_rows_and_add_buttons_ask_for_the_dialog(client, csm, account):
    contact = Contact.objects.create(
        account=account, full_name="Dana Farouk", influence="HIGH", interest="HIGH"
    )
    login(client, csm)
    home = client.get(reverse("dashboard:index")).content.decode()
    assert f'class="row-link" data-modal data-href="{reverse("contacts:detail", args=[contact.pk])}"' in home

    page = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    at = page.index(f'href="{reverse("contacts:create", args=[account.pk])}"')
    assert "data-modal" in page[page.rindex("<a ", 0, at) : at]


def test_the_contact_view_offers_task_call_meeting_csat(client, csm, account):
    contact = Contact.objects.create(account=account, full_name="Dana Farouk")
    login(client, csm)
    body = client.get(reverse("contacts:detail", args=[contact.pk]), **MODAL).content.decode()
    menu = body[body.index('aria-label="Add something for Dana Farouk"'):]
    menu = " ".join(menu[: menu.index("</ul>")].split())

    words = [f"</i>{word} </a>" for word in ("Task", "Call", "Meeting", "CSAT")]
    positions = [menu.index(word) for word in words]
    assert positions == sorted(positions)
    assert f"{reverse('engagement:csat_create', args=[account.pk])}?contact={contact.pk}" in menu
