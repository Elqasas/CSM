"""Account custom fields: admin names one, any editor fills it in.

Two access levels on purpose - defining a field is schema-level and Admin
only, filling one in rides the ordinary account.edit capability everyone with
an editable account already has.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from csm.accounts.models import AccountCustomFieldDefinition, AccountCustomFieldValue
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


@pytest.fixture
def staff(db):
    users = {
        "admin": UserFactory(admin=True, email="cf-admin@dphish.com"),
        "csm": UserFactory(csm=True, email="cf-csm@dphish.com"),
        "acm": UserFactory(acm=True, email="cf-acm@dphish.com"),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def account(staff):
    return AccountFactory(client_code="CF01", client_name="Custom Co", csm=staff["csm"])


def test_only_an_admin_may_reach_the_definitions_page(client, staff):
    for who in ("csm", "acm"):
        login(client, staff[who])
        assert client.get(reverse("accounts:custom_fields")).status_code == 403

    login(client, staff["admin"])
    assert client.get(reverse("accounts:custom_fields")).status_code == 200


def test_an_admin_can_add_a_field(client, staff):
    login(client, staff["admin"])
    response = client.post(
        reverse("accounts:custom_fields"), {"name": "Account Type", "field_type": "TEXT"}
    )
    assert response.status_code == 302
    definition = AccountCustomFieldDefinition.objects.get(name="Account Type")
    assert definition.is_active is True
    assert definition.created_by == staff["admin"]
    assert definition.field_type == "TEXT"


def test_a_duplicate_name_is_refused(client, staff):
    AccountCustomFieldDefinition.objects.create(name="Account Type")
    login(client, staff["admin"])
    response = client.post(
        reverse("accounts:custom_fields"), {"name": "account type", "field_type": "TEXT"}
    )
    assert response.status_code == 200
    assert AccountCustomFieldDefinition.objects.count() == 1


def test_a_dropdown_field_requires_at_least_one_option(client, staff):
    login(client, staff["admin"])
    response = client.post(
        reverse("accounts:custom_fields"),
        {"name": "Account Type", "field_type": "DROPDOWN", "choices_text": ""},
    )
    assert response.status_code == 200
    assert AccountCustomFieldDefinition.objects.count() == 0


def test_a_dropdown_field_renders_its_options_and_saves_the_choice(client, staff, account):
    definition = AccountCustomFieldDefinition.objects.create(
        name="Account Type", field_type="DROPDOWN", choices_text="Direct\nPartner\nReseller"
    )
    login(client, staff["csm"])

    page = client.get(reverse("accounts:edit", args=[account.pk])).content.decode()
    assert f'name="custom_{definition.pk}"' in page
    assert "Partner" in page

    data = {
        "client_code": account.client_code, "client_name": account.client_name,
        "deployment": "CLOUD", "account_class": "B", "size": "MID",
        "lifecycle_stage": "ACTIVE", "boq_users_count": 10, "added_users_count": 0,
        "renewal_count": 0, "events_count": 0, "digital_content_count": 0,
        "g2_review_status": "NONE", "gartner_review_status": "NONE",
        f"custom_{definition.pk}": "Reseller",
    }
    client.post(reverse("accounts:edit", args=[account.pk]), data)

    value = AccountCustomFieldValue.objects.get(account=account, definition=definition)
    assert value.value == "Reseller"


def test_a_datetime_field_round_trips_through_the_form(client, staff, account):
    definition = AccountCustomFieldDefinition.objects.create(
        name="Last audit", field_type="DATETIME"
    )
    login(client, staff["csm"])

    data = {
        "client_code": account.client_code, "client_name": account.client_name,
        "deployment": "CLOUD", "account_class": "B", "size": "MID",
        "lifecycle_stage": "ACTIVE", "boq_users_count": 10, "added_users_count": 0,
        "renewal_count": 0, "events_count": 0, "digital_content_count": 0,
        "g2_review_status": "NONE", "gartner_review_status": "NONE",
        "csm": staff["csm"].pk,
        f"custom_{definition.pk}": "2026-01-15T09:30",
    }
    client.post(reverse("accounts:edit", args=[account.pk]), data)

    value = AccountCustomFieldValue.objects.get(account=account, definition=definition)
    assert value.value.startswith("2026-01-15T09:30:00")

    detail_page = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    assert "15 Jan 2026 09:30" in detail_page


def test_the_field_appears_on_the_account_form_and_saves_a_value(client, staff, account):
    definition = AccountCustomFieldDefinition.objects.create(name="Account Type")
    login(client, staff["csm"])

    page = client.get(reverse("accounts:edit", args=[account.pk])).content.decode()
    assert f'name="custom_{definition.pk}"' in page

    data = {
        "client_code": account.client_code, "client_name": account.client_name,
        "deployment": "CLOUD", "account_class": "B", "size": "MID",
        "lifecycle_stage": "ACTIVE", "boq_users_count": 10, "added_users_count": 0,
        "renewal_count": 0, "events_count": 0, "digital_content_count": 0,
        "g2_review_status": "NONE", "gartner_review_status": "NONE",
        f"custom_{definition.pk}": "Direct",
    }
    response = client.post(reverse("accounts:edit", args=[account.pk]), data)
    assert response.status_code == 302

    value = AccountCustomFieldValue.objects.get(account=account, definition=definition)
    assert value.value == "Direct"


def test_a_saved_value_shows_on_the_account_detail_page(client, staff, account):
    definition = AccountCustomFieldDefinition.objects.create(name="Account Type")
    AccountCustomFieldValue.objects.create(account=account, definition=definition, value="ChannelType")

    login(client, staff["csm"])
    page = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    assert "Account Type" in page
    assert "ChannelType" in page


def test_deactivating_hides_the_field_without_losing_its_value(client, staff, account):
    definition = AccountCustomFieldDefinition.objects.create(name="Account Type")
    AccountCustomFieldValue.objects.create(account=account, definition=definition, value="ChannelType")

    login(client, staff["admin"])
    client.post(reverse("accounts:custom_field_toggle", args=[definition.pk]))
    definition.refresh_from_db()
    assert definition.is_active is False

    login(client, staff["csm"])
    edit_page = client.get(reverse("accounts:edit", args=[account.pk])).content.decode()
    assert f'name="custom_{definition.pk}"' not in edit_page

    detail_page = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    assert "ChannelType" not in detail_page

    # Reactivating brings the historical value back rather than losing it.
    login(client, staff["admin"])
    client.post(reverse("accounts:custom_field_toggle", args=[definition.pk]))

    login(client, staff["csm"])
    detail_page = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    assert "ChannelType" in detail_page


def test_an_acm_sees_no_custom_field_input_because_they_hold_no_account_edit(
    client, staff, account
):
    AccountCustomFieldDefinition.objects.create(name="Account Type")
    account.acm = staff["acm"]
    account.save(update_fields=["acm"])

    login(client, staff["acm"])
    # An ACM has no accounts:edit route reachable at all - account.edit is
    # denied at the capability gate before the form is even built.
    assert client.get(reverse("accounts:edit", args=[account.pk])).status_code == 403
