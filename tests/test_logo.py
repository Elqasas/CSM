"""The account logo.

Shown at 50×50 and nowhere else, so the constraints are about what reaches the
browser rather than about storage. PNG or JPEG under one size limit, and
*proved* to be what the name says: an extension check alone is worthless, because anything can be renamed, and what
matters is what the browser will be asked to render.
"""

from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from PIL import Image

from csm.accounts.forms import LOGO_BOX
from csm.accounts.logos import DARK_TILE, LOGO_FILL, LOGO_STORED_PX
from csm.accounts.models import Account
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login


def image_bytes(fmt="PNG", size=(120, 120), colour=(40, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" else "RGB"
    Image.new(mode, size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


def upload(name="logo.png", data=None, fmt="PNG"):
    content_type = "image/png" if fmt == "PNG" else "image/jpeg"
    return SimpleUploadedFile(name, data if data is not None else image_bytes(fmt), content_type)


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


@pytest.mark.parametrize("name", ["logo.jpg", "logo.jpeg", "LOGO.JPG"])
def test_a_real_jpeg_is_accepted(client, account, senior, name):
    login(client, senior)
    response = post(client, account, logo=upload(name=name, fmt="JPEG"))

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.logo


def test_a_jpeg_is_refused_when_named_png(client, account, senior):
    """Renaming a file does not change what the browser has to render."""
    login(client, senior)
    response = post(client, account, logo=upload(name="sneaky.png", data=image_bytes("JPEG")))

    assert response.status_code == 200
    assert "actually JPEG" in response.content.decode()
    account.refresh_from_db()
    assert not account.logo


def test_a_png_is_refused_when_named_jpeg(client, account, senior):
    login(client, senior)
    response = post(client, account, logo=upload(name="sneaky.jpg", data=image_bytes("PNG")))

    assert response.status_code == 200
    assert "actually PNG" in response.content.decode()


@pytest.mark.parametrize("name", ["logo.gif", "logo.webp", "logo.svg", "logo"])
def test_other_extensions_are_refused(client, account, senior, name):
    login(client, senior)
    response = post(client, account, logo=upload(name=name, data=image_bytes("GIF")))

    assert response.status_code == 200
    assert "must be a PNG or JPEG" in response.content.decode()


def test_something_that_is_not_an_image_at_all_is_refused(client, account, senior):
    login(client, senior)
    response = post(client, account, logo=upload(name="logo.png", data=b"this is not an image"))

    assert response.status_code == 200
    assert "not a readable image" in response.content.decode()
    account.refresh_from_db()
    assert not account.logo


@pytest.mark.parametrize(
    ("name", "magic"),
    [("huge.png", b"\x89PNG\r\n\x1a\n"), ("huge.jpg", b"\xff\xd8\xff\xe0")],
)
def test_an_oversized_logo_is_refused_with_the_reason(client, account, senior, name, magic):
    """One limit for both formats."""
    login(client, senior)
    big = magic + b"0" * (3 * 1024 * 1024)
    response = post(client, account, logo=upload(name=name, data=big))

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

    assert "PNG or JPEG" in body
    assert f"{LOGO_BOX}×{LOGO_BOX}" in body
    assert 'accept="image/png,image/jpeg"' in body


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



# ---------------------------------------------------------------------------
# Refitting to the tile it is shown in
# ---------------------------------------------------------------------------


def stored_image(account):
    account.refresh_from_db()
    with account.logo.open("rb") as handle:
        image = Image.open(handle)
        image.load()
    return image


def mark_on(background, canvas=(400, 200), box=(100, 50, 300, 150), ink=(20, 40, 120), fmt="PNG"):
    """A rectangle of ``ink`` inside ``canvas``, on ``background`` (RGBA or RGB)."""
    mode = "RGBA" if len(background) == 4 else "RGB"
    fill = ink + (255,) if mode == "RGBA" else ink
    image = Image.new(mode, canvas, background)
    image.paste(Image.new(mode, (box[2] - box[0], box[3] - box[1]), fill), box[:2])
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def mark_box(image, background):
    """Where the stored image differs from its tile colour."""
    from PIL import ImageChops

    tile = Image.new("RGB", image.size, background)
    difference = ImageChops.difference(image.convert("RGB"), tile)
    return difference.convert("L").point(lambda v: 255 if v > 24 else 0).getbbox()


@pytest.mark.parametrize(
    ("fmt", "name", "data"),
    [
        ("PNG", "wide.png", mark_on((0, 0, 0, 0), canvas=(800, 200), box=(0, 0, 800, 200))),
        ("PNG", "tall.png", mark_on((0, 0, 0, 0), canvas=(60, 300), box=(0, 0, 60, 300))),
        ("PNG", "tiny.png", mark_on((0, 0, 0, 0), canvas=(16, 16), box=(2, 2, 14, 14))),
        ("JPEG", "photo.jpg", mark_on((255, 255, 255), canvas=(1200, 900), fmt="JPEG")),
    ],
    ids=["wide", "tall", "tiny", "jpeg"],
)
def test_every_upload_is_stored_as_an_opaque_square_tile(client, account, senior, fmt, name, data):
    """Opaque, so a logo looks the same on the light and the dark theme."""
    login(client, senior)
    post(client, account, logo=upload(name=name, data=data, fmt=fmt))

    image = stored_image(account)
    assert image.format == "PNG"
    assert image.mode == "RGB", "no transparency for the dark page to show through"
    assert image.size == (LOGO_STORED_PX, LOGO_STORED_PX)
    assert account.logo.name.endswith(".png")


def test_empty_space_is_cropped_and_the_mark_centred_with_an_even_margin(client, account, senior):
    """A file with a wide empty border must not shrink the mark to a speck."""
    login(client, senior)
    data = mark_on((255, 255, 255), canvas=(2000, 2000), box=(900, 950, 1300, 1050), fmt="JPEG")
    post(client, account, logo=upload(name="mostly-empty.jpg", data=data, fmt="JPEG"))

    image = stored_image(account)
    left, top, right, bottom = mark_box(image, (255, 255, 255))

    assert right - left == pytest.approx(round(LOGO_STORED_PX * LOGO_FILL), abs=2)
    assert round((right - left) / (bottom - top)) == 4, "4:1 in, 4:1 out"
    assert abs(left - (LOGO_STORED_PX - right)) <= 2
    assert abs(top - (LOGO_STORED_PX - bottom)) <= 2
    assert image.getpixel((0, 0)) == (255, 255, 255), "the margin is plain white"


def test_a_transparent_dark_mark_is_put_on_white(client, account, senior):
    login(client, senior)
    post(client, account, logo=upload(data=mark_on((0, 0, 0, 0))))

    assert stored_image(account).getpixel((0, 0)) == (255, 255, 255)


def test_a_transparent_white_mark_is_put_on_a_dark_tile(client, account, senior):
    """A white logo on white is no logo at all."""
    login(client, senior)
    post(client, account, logo=upload(data=mark_on((0, 0, 0, 0), ink=(255, 255, 255))))

    assert stored_image(account).getpixel((0, 0)) == DARK_TILE


def test_a_white_box_with_dark_lettering_stays_on_white(client, account, senior):
    """Mostly white on average, but it has ink - it is not a white mark."""
    image = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (300, 100), (255, 255, 255, 255)), (50, 150))
    image.paste(Image.new("RGBA", (200, 30), (10, 10, 10, 255)), (100, 185))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    login(client, senior)
    post(client, account, logo=upload(data=buffer.getvalue()))

    assert stored_image(account).getpixel((0, 0)) == (255, 255, 255)


def test_a_logo_on_a_coloured_square_stays_a_coloured_tile(client, account, senior):
    """The margin takes the logo's own background, not a white frame."""
    teal = (0, 150, 140)
    data = mark_on(teal, canvas=(300, 300), box=(60, 120, 240, 180), ink=(255, 255, 255))
    login(client, senior)
    post(client, account, logo=upload(data=data))

    image = stored_image(account)
    for corner in [(0, 0), (LOGO_STORED_PX - 1, LOGO_STORED_PX - 1)]:
        assert image.getpixel(corner) == teal


def test_a_sideways_phone_photo_is_stored_upright(client, account, senior):
    """The browser honours an EXIF rotation; a resize would silently drop it."""
    image = Image.open(io.BytesIO(mark_on((255, 255, 255), fmt="JPEG")))
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated: display turned 90 degrees clockwise
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    login(client, senior)
    post(client, account, logo=upload(name="phone.jpg", data=buffer.getvalue(), fmt="JPEG"))

    left, top, right, bottom = mark_box(stored_image(account), (255, 255, 255))
    assert bottom - top > right - left, "a wide mark turned upright is tall"


def test_an_image_with_an_absurd_pixel_count_is_refused(client, account, senior):
    """Small in bytes, enormous in memory once decoded."""
    buffer = io.BytesIO()
    Image.new("1", (6000, 6000)).save(buffer, format="PNG")
    assert len(buffer.getvalue()) < 2 * 1024 * 1024

    login(client, senior)
    response = post(client, account, logo=upload(name="bomb.png", data=buffer.getvalue()))

    assert response.status_code == 200
    assert "6000 by 6000 pixels" in response.content.decode()
    account.refresh_from_db()
    assert not account.logo


def test_the_refit_command_fits_stored_logos_once(account):
    """Logos uploaded before the refit existed. Running it twice must not
    resample twice."""
    from django.core.files.base import ContentFile

    account.logo.save("legacy.png", ContentFile(mark_on((0, 0, 0, 0), canvas=(600, 300))))
    old_name = account.logo.name
    storage = account.logo.storage

    out = io.StringIO()
    call_command("refit_logos", stdout=out)
    assert "refitted 1" in out.getvalue()
    assert not storage.exists(old_name), "the original is removed once replaced"

    first = stored_image(account)
    assert first.size == (LOGO_STORED_PX, LOGO_STORED_PX)
    first_name = account.logo.name

    out = io.StringIO()
    call_command("refit_logos", stdout=out)
    assert "refitted 0, already fitted 1" in out.getvalue()
    account.refresh_from_db()
    assert account.logo.name == first_name


def test_the_refit_command_redoes_a_transparent_tile_from_the_first_version(account):
    """The first refit stored 150x150 with a transparent margin - the right
    size, the wrong thing. Size alone must not mark it as done."""
    from django.core.files.base import ContentFile

    square = (LOGO_STORED_PX, LOGO_STORED_PX)
    data = mark_on((0, 0, 0, 0), canvas=square, box=(7, 40, 143, 110))
    account.logo.save("first-refit.png", ContentFile(data))

    out = io.StringIO()
    call_command("refit_logos", stdout=out)

    assert "refitted 1" in out.getvalue()
    assert stored_image(account).mode == "RGB"


def test_the_refit_command_dry_run_writes_nothing(account):
    from django.core.files.base import ContentFile

    account.logo.save("legacy.png", ContentFile(mark_on((0, 0, 0, 0), canvas=(600, 300))))
    name = account.logo.name

    out = io.StringIO()
    call_command("refit_logos", "--dry-run", stdout=out)

    assert "would refit 1" in out.getvalue()
    account.refresh_from_db()
    assert account.logo.name == name
    assert stored_image(account).size == (600, 300)
