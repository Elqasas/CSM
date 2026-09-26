"""The account logo.

Shown at 50×50 and nowhere else, so the constraints are about what reaches the
browser rather than about storage. PNG only, and *proved* to be PNG: an
extension check alone is worthless, because anything can be renamed, and what
matters is what the browser will be asked to render.
"""

from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from csm.accounts.forms import LOGO_BOX
from csm.accounts.models import Account
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


def image_bytes(fmt="PNG", size=(120, 120), colour=(40, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" else "RGB"
    Image.new(mode, size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


def upload(name="logo.png", data=None, fmt="PNG"):
    return SimpleUploadedFile(name, data if data is not None else image_bytes(fmt), "image/png")


@pytest.fixture(autouse=True)
def _isolated_media(tmp_path, settings):
    """Write uploads to a temporary directory, not the project's media folder.

    Otherwise a test run leaves real files behind, and on Windows the storage
    backend cannot delete one the test client still holds open.
    """
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


@pytest.fixture
def senior(db):
    user = UserFactory(senior_csm=True, email="logo-senior@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    return user


@pytest.fixture
def account(senior):
    return AccountFactory(client_code="LOGO1", client_name="Acme Industrial", csm=senior)


def post(client, account, **overrides):
    data = {
        "client_code": account.client_code, "client_name": account.client_name,
        "deployment": "CLOUD", "account_class": "B", "size": "MID",
        "lifecycle_stage": "ACTIVE", "boq_users_count": 10, "added_users_count": 0,
        "renewal_count": 0, "events_count": 0, "digital_content_count": 0,
        "g2_review_status": "NONE", "gartner_review_status": "NONE",
    }
    data.update(overrides)
    return client.post(reverse("accounts:edit", args=[account.pk]), data)


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------


def test_a_real_png_is_accepted(client, account, senior):
    login(client, senior)
    response = post(client, account, logo=upload())

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.logo


def test_a_jpeg_is_refused_even_when_named_png(client, account, senior):
    """Renaming a file does not change what the browser has to render."""
    login(client, senior)
    response = post(client, account, logo=upload(name="sneaky.png", data=image_bytes("JPEG")))

    assert response.status_code == 200
    assert "actually JPEG" in response.content.decode()
    account.refresh_from_db()
    assert not account.logo


def test_a_jpeg_named_jpeg_is_refused_on_the_extension(client, account, senior):
    login(client, senior)
    response = post(client, account, logo=upload(name="logo.jpg", data=image_bytes("JPEG")))

    assert response.status_code == 200
    assert "must be a PNG" in response.content.decode()


def test_something_that_is_not_an_image_at_all_is_refused(client, account, senior):
    login(client, senior)
    response = post(client, account, logo=upload(name="logo.png", data=b"this is not an image"))

    assert response.status_code == 200
    assert "not a readable image" in response.content.decode()
    account.refresh_from_db()
    assert not account.logo


def test_an_oversized_png_is_refused_with_the_reason(client, account, senior):
    login(client, senior)
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)
    response = post(client, account, logo=upload(name="huge.png", data=big))

    assert response.status_code == 200
    body = response.content.decode()
    assert "larger than 2MB" in body
    assert "50 by 50" in body, "the reason it need not be big"


def test_editing_without_touching_the_logo_keeps_it(client, account, senior):
    """Django hands back the stored file, which must not be re-validated."""
    login(client, senior)
    post(client, account, logo=upload())
    account.refresh_from_db()
    stored = account.logo.name

    post(client, account, client_name="Acme Renamed")
    account.refresh_from_db()

    assert account.client_name == "Acme Renamed"
    assert account.logo.name == stored


# ---------------------------------------------------------------------------
# What the interface says and shows
# ---------------------------------------------------------------------------


def test_the_form_states_the_constraints(client, account, senior):
    login(client, senior)
    body = client.get(reverse("accounts:edit", args=[account.pk])).content.decode()

    assert "PNG only" in body
    assert f"{LOGO_BOX}×{LOGO_BOX}" in body
    assert 'accept="image/png"' in body


def test_an_account_without_a_logo_shows_its_initials(client, account, senior):
    login(client, senior)
    body = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()

    assert "acct-logo-initials" in body
    assert "AC" in body, "the first two letters of Acme Industrial"


def test_an_account_with_a_logo_shows_the_image_at_the_fixed_size(client, account, senior):
    login(client, senior)
    post(client, account, logo=upload())

    body = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    assert 'class="acct-logo"' in body
    assert f'width="{LOGO_BOX}" height="{LOGO_BOX}"' in body

    account.refresh_from_db()


def test_the_account_list_shows_a_mark_for_every_row(client, senior):
    AccountFactory(client_code="L1", client_name="Beta Systems", csm=senior)
    AccountFactory(client_code="L2", client_name="Gamma Group", csm=senior)
    login(client, senior)

    body = client.get(reverse("accounts:list")).content.decode()
    assert body.count("acct-logo") >= 2


def test_an_acm_cannot_change_the_logo(client, account, senior):
    """An ACM holds no account.edit capability at all, so the form is
    unreachable - not merely missing this one field."""
    acm = UserFactory(acm=True, email="logo-acm@dphish.com")
    acm.set_password(PASSWORD)
    acm.save(update_fields=["password"])
    Account.objects.filter(pk=account.pk).update(acm=acm)

    login(client, acm)
    response = client.get(reverse("accounts:edit", args=[account.pk]))

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# The mark as a link
# ---------------------------------------------------------------------------


def test_the_logo_links_to_the_account_in_the_list(client, senior):
    """In a list the mark is the first thing the eye lands on, so it should
    behave like the name beside it."""
    account = AccountFactory(client_code="LNK1", client_name="Linked Co", csm=senior)
    login(client, senior)

    body = client.get(reverse("accounts:list")).content.decode()
    target = reverse("accounts:detail", args=[account.pk])

    assert f'href="{target}" class="acct-logo-link"' in body
    assert f'aria-label="Open {account.client_name}"' in body


def test_the_logo_links_on_the_analytics_roster_too(client, senior):
    AccountFactory(client_code="LNK2", client_name="Roster Co", csm=senior)
    login(client, senior)

    roster = client.get(reverse("dashboard:analytics")).content.decode()
    roster = roster.split("Accounts in this view")[1]

    assert "acct-logo-link" in roster


def test_an_uploaded_logo_is_linked_the_same_way(client, senior):
    account = AccountFactory(client_code="LNK3", client_name="Image Co", csm=senior)
    login(client, senior)
    post(client, account, logo=upload())

    body = client.get(reverse("accounts:list")).content.decode()
    assert "acct-logo-link" in body
    assert 'class="acct-logo"' in body, "the image, not the initials"


def test_the_account_page_does_not_link_its_own_mark(client, senior):
    """A link back to the page you are on is a dead end that looks live."""
    account = AccountFactory(client_code="LNK4", client_name="Self Co", csm=senior)
    login(client, senior)

    body = client.get(reverse("accounts:detail", args=[account.pk])).content.decode()
    header = body.split("<h1")[0]

    assert "acct-logo" in body
    assert "acct-logo-link" not in header
