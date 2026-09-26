"""The Excel importer.

The tests that matter are the ones about *not* doing something: not creating a
user from a spreadsheet, not guessing an influence level, not importing a row
whose contract ends before it starts, and not writing anything at all until
somebody has seen the preview.

The template round-trip is the other load-bearing one. The template's headers
and the parser's aliases are generated from the same table, and this proves it:
a template that drifts from the parser produces a sheet that validates as empty,
which is the failure nobody notices until sixty accounts are missing.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from csm.accounts.models import Account
from csm.core.enums import LifecycleStage
from csm.importer import services
from csm.importer.models import ImportBatch, ImportStatus, RowAction
from csm.importer.parsing import COLUMNS, normalise_header, parse
from csm.importer.template import build_template, header_label
from tests.factories import AccountFactory, UserFactory
from tests.test_views import PASSWORD, login

HEADERS = [
    "Client Code", "Client Name", "Country", "Location", "License",
    "Cloud/On-prem", "Management Service", "BOQ Users Count", "Added Users",
    "Start Contract Date", "End Contract Date",
    "CSM Email", "ACM Email", "Partner", "Class", "Lifecycle Stage",
]


def sheet(rows, headers=None) -> io.BytesIO:
    workbook = Workbook()
    ws = workbook.active
    ws.append(headers or HEADERS)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def a_row(**overrides) -> list:
    values = {
        "Client Code": "ACME01", "Client Name": "Acme Industrial",
        "Country": "Egypt", "Location": "Cairo",
        # License is the product edition; the seats are their own column.
        "License": "Complete",
        "Cloud/On-prem": "Cloud", "Management Service": "Yes",
        "BOQ Users Count": 500, "Added Users": 50,
        "Start Contract Date": "2025-01-01", "End Contract Date": "2026-12-31",
        "CSM Email": "imp-csm@dphish.com",
        "ACM Email": "imp-acm@dphish.com",
        "Partner": "Regional Reseller",
        "Class": "VIP", "Lifecycle Stage": "",
    }
    values.update(overrides)
    return [values[h] for h in HEADERS]


@pytest.fixture
def staff(db):
    users = {
        "senior": UserFactory(senior_csm=True, email="imp-senior@dphish.com"),
        "csm": UserFactory(csm=True, email="imp-csm@dphish.com"),
        "acm": UserFactory(acm=True, email="imp-acm@dphish.com"),
    }
    for user in users.values():
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return users


@pytest.fixture
def emails(staff):
    from csm.importer.services import _people

    return _people()


def make_batch(db, staff, rows, headers=None) -> ImportBatch:
    upload = SimpleUploadedFile(
        "portfolio.xlsx",
        sheet(rows, headers).getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    batch = ImportBatch.objects.create(
        upload=upload,
        original_name="portfolio.xlsx",
        uploaded_by=staff["senior"],
        baseline_date=dt.date(2026, 9, 21),
    )
    return services.validate(batch)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_headers_are_matched_regardless_of_spelling():
    assert normalise_header("Client  Code:") == normalise_header("client_code")
    assert normalise_header("CLIENT CODE *") == "client code"


def test_a_clean_row_parses_with_no_errors(emails):
    rows, unknown = parse(sheet([a_row()]), people=emails)
    assert unknown == []
    assert rows[0].errors == []
    assert rows[0].cleaned["country"] == "EG"
    assert rows[0].cleaned["is_managed_service"] is True


def test_an_empty_lifecycle_stage_defaults_to_active(emails):
    rows, _ = parse(sheet([a_row(**{"Lifecycle Stage": ""})]), people=emails)
    assert rows[0].cleaned["lifecycle_stage"] == LifecycleStage.ACTIVE
    assert any("Active" in w["message"] for w in rows[0].warnings)


def test_a_non_email_value_in_the_email_column_is_an_error(emails):
    rows, _ = parse(sheet([a_row(**{"CSM Email": "Casey Morgan"})]), people=emails)
    assert any("is not an email address" in e["message"] for e in rows[0].errors)


def test_an_unknown_email_is_an_error_and_creates_nobody(emails, db):
    before = UserFactory._meta.model.objects.count()
    rows, _ = parse(sheet([a_row(**{"CSM Email": "ghost@dphish.com"})]), people=emails)

    assert any("ghost@dphish.com" in e["message"] for e in rows[0].errors)
    assert UserFactory._meta.model.objects.count() == before


def test_a_contract_that_ends_before_it_starts_is_an_error(emails):
    rows, _ = parse(
        sheet([a_row(**{"Start Contract Date": "2026-01-01",
                        "End Contract Date": "2025-01-01"})]),
        people=emails,
    )
    assert any(e["column"] == "End contract date" for e in rows[0].errors)


def test_an_unrecognised_country_is_an_error(emails):
    rows, _ = parse(sheet([a_row(Country="Freedonia")]), people=emails)
    assert any(e["column"] == "Country" for e in rows[0].errors)


def test_a_duplicate_code_inside_one_sheet_is_flagged_on_the_second_row(emails):
    rows, _ = parse(
        sheet([a_row(), a_row(**{"Client Name": "Acme Again"})]), people=emails
    )
    assert rows[0].errors == []
    assert any("also appears on row 2" in e["message"] for e in rows[1].errors)


def test_blank_spacer_rows_are_skipped_rather_than_erroring(emails):
    workbook = Workbook()
    ws = workbook.active
    ws.append(HEADERS)
    ws.append(a_row())
    ws.append([None] * len(HEADERS))
    ws.append(a_row(**{"Client Code": "ACME02"}))
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    rows, _ = parse(buffer, people=emails)
    assert len(rows) == 2


def test_unknown_columns_are_reported_rather_than_dropped_silently(emails):
    headers = HEADERS + ["Favourite Colour"]
    rows, unknown = parse(
        sheet([a_row() + ["blue"]], headers), people=emails
    )
    assert unknown == ["Favourite Colour"]
    assert rows[0].errors == []


def test_a_sheet_without_client_code_is_refused_outright(emails):
    from csm.importer.parsing import WorkbookError

    with pytest.raises(WorkbookError, match="Client Code"):
        parse(sheet([["Acme"]], ["Client Name"]), people=emails)


def test_a_managed_account_with_no_frequency_is_warned_not_rejected(emails):
    rows, _ = parse(
        sheet([a_row(**{"Management Service": "Yes"})]), people=emails
    )
    assert rows[0].errors == []
    assert any("out of 80" in w["message"] for w in rows[0].warnings)


# ---------------------------------------------------------------------------
# The template round-trip
# ---------------------------------------------------------------------------


def test_every_template_header_is_one_the_parser_recognises(emails):
    """Otherwise the template produces a sheet that validates as empty."""
    from csm.importer.parsing import read_workbook

    header_map, body, unknown = read_workbook(io.BytesIO(build_template()))

    assert unknown == [], f"template emits headers the parser ignores: {unknown}"
    assert set(header_map) == set(COLUMNS)
    assert body == [], "the template's first sheet must contain no data rows"


def test_the_template_guidance_does_not_import_as_an_account(emails):
    """The guidance lives on a second sheet. Under the headers it would import
    as a client called 'Required. The natural key'."""
    rows, _ = parse(io.BytesIO(build_template()), people=emails)
    assert rows == []


def test_the_required_columns_are_starred_in_the_template():
    content = build_template()
    from openpyxl import load_workbook

    ws = load_workbook(io.BytesIO(content)).worksheets[0]
    headers = [c.value for c in ws[1]]
    assert f"{header_label('client_code')} *" in headers


# ---------------------------------------------------------------------------
# Validate and apply
# ---------------------------------------------------------------------------


def test_validating_writes_nothing_to_accounts(db, staff):
    batch = make_batch(db, staff, [a_row()])

    assert batch.status == ImportStatus.VALIDATED
    assert batch.rows_ready == 1
    assert Account.objects.count() == 0, "preview must not write"


def test_applying_creates_the_account_with_the_import_baseline(db, staff):
    batch = make_batch(db, staff, [a_row()])
    result = services.apply(batch, actor=staff["senior"])

    account = Account.objects.get(client_code="ACME01")
    assert result["created"] == 1
    assert account.client_name == "Acme Industrial"
    assert account.country == "EG"
    assert account.csm == staff["csm"]
    assert account.acm == staff["acm"]
    assert account.partner.name == "Regional Reseller"
    # The baseline rule: day counts start at the import, not at zero.
    assert account.scoring_baseline_date == dt.date(2026, 9, 21)
    assert account.imported_at is not None


def test_rows_with_errors_are_skipped_and_the_rest_still_import(db, staff):
    batch = make_batch(
        db, staff,
        [a_row(), a_row(**{"Client Code": "ACME02", "CSM Email": "ghost@dphish.com"})],
    )
    assert batch.rows_with_errors == 1

    result = services.apply(batch, actor=staff["senior"])
    assert result["created"] == 1
    assert Account.objects.filter(client_code="ACME01").exists()
    assert not Account.objects.filter(client_code="ACME02").exists()


def test_a_second_import_updates_rather_than_duplicating(db, staff):
    services.apply(make_batch(db, staff, [a_row()]), actor=staff["senior"])
    batch = make_batch(db, staff, [a_row(**{"Client Name": "Acme Renamed"})])

    assert batch.rows_to_update == 1
    result = services.apply(batch, actor=staff["senior"])

    assert result["updated"] == 1
    assert Account.objects.filter(client_code="ACME01").count() == 1
    assert Account.objects.get(client_code="ACME01").client_name == "Acme Renamed"


def test_reimporting_a_deleted_code_restores_it_rather_than_hiding_it(db, staff):
    """Updating a soft-deleted row without clearing deleted_at would import an
    account invisible to every list, which looks exactly like a failed import."""
    from csm.core import clock

    services.apply(make_batch(db, staff, [a_row()]), actor=staff["senior"])
    account = Account.objects.get(client_code="ACME01")
    account.deleted_at = clock.now()
    account.save(update_fields=["deleted_at"])

    batch = make_batch(db, staff, [a_row()])
    assert any("deleted account" in w["message"] for w in batch.rows.first().warnings)

    services.apply(batch, actor=staff["senior"])
    account.refresh_from_db()
    assert account.deleted_at is None
    assert Account.objects.alive().filter(client_code="ACME01").exists()


def test_applying_twice_is_refused(db, staff):
    batch = make_batch(db, staff, [a_row()])
    services.apply(batch, actor=staff["senior"])

    with pytest.raises(ValueError, match="validated"):
        services.apply(batch, actor=staff["senior"])


def test_an_unreadable_file_fails_the_batch_without_raising(db, staff):
    upload = SimpleUploadedFile("notes.xlsx", b"this is not a workbook")
    batch = ImportBatch.objects.create(
        upload=upload, original_name="notes.xlsx",
        uploaded_by=staff["senior"], baseline_date=dt.date(2026, 9, 21),
    )
    services.validate(batch)

    assert batch.status == ImportStatus.FAILED
    assert batch.parse_error
    assert batch.can_apply is False


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_a_manager_may_not_reach_the_importer(client, db):
    user = UserFactory(manager=True, email="imp-manager-only@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])

    login(client, user)
    assert client.get(reverse("importer:list")).status_code == 403


def test_an_acm_may_reach_the_importer_but_only_for_contacts(client, staff):
    """An ACM has no ``account.edit`` capability, and the importer writes
    accounts with no check of its own - so the list page, the preview, the
    apply endpoint and the blank template all have to hold that line
    themselves rather than trusting the bare ``import.run`` capability."""
    login(client, staff["acm"])
    assert client.get(reverse("importer:list")).status_code == 200
    assert client.get(reverse("importer:template")).status_code == 403
    assert (
        client.get(reverse("importer:template_for", args=["accounts", "xlsx"])).status_code
        == 403
    )
    assert (
        client.get(reverse("importer:template_for", args=["contacts", "xlsx"])).status_code
        == 200
    )

    upload = SimpleUploadedFile(
        "portfolio.xlsx", sheet([a_row()]).getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response = client.post(
        reverse("importer:upload"), {"upload": upload, "kind": "ACCOUNTS"}, follow=True
    )
    assert response.status_code == 200
    assert ImportBatch.objects.count() == 0, "an accounts batch is not even created"


def test_an_acm_may_not_preview_or_apply_someone_elses_accounts_batch(client, staff):
    login(client, staff["senior"])
    upload = SimpleUploadedFile(
        "portfolio.xlsx", sheet([a_row()]).getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    client.post(reverse("importer:upload"), {"upload": upload, "kind": "ACCOUNTS"})
    batch = ImportBatch.objects.get()

    login(client, staff["acm"])
    assert client.get(reverse("importer:preview", args=[batch.pk])).status_code == 404
    assert client.post(reverse("importer:apply", args=[batch.pk])).status_code == 404


def test_any_csm_may_reach_the_importer(client, db):
    user = UserFactory(csm=True, email="imp-csm-only@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])

    login(client, user)
    assert client.get(reverse("importer:list")).status_code == 200


def test_a_senior_csm_may_reach_the_importer(client, staff):
    login(client, staff["senior"])
    assert client.get(reverse("importer:list")).status_code == 200


def test_the_template_downloads_as_a_workbook(client, staff):
    login(client, staff["senior"])
    response = client.get(reverse("importer:template"))

    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment")
    assert response.content[:2] == b"PK", "an xlsx is a zip"


def test_the_wizard_runs_end_to_end_through_the_views(client, staff):
    login(client, staff["senior"])
    upload = SimpleUploadedFile(
        "portfolio.xlsx", sheet([a_row()]).getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    response = client.post(
        reverse("importer:upload"), {"upload": upload, "kind": "ACCOUNTS"}, follow=True
    )
    assert response.status_code == 200
    assert Account.objects.count() == 0, "upload previews, it does not write"

    batch = ImportBatch.objects.get()
    client.post(reverse("importer:apply", args=[batch.pk]))

    batch.refresh_from_db()
    assert batch.status == ImportStatus.APPLIED
    assert Account.objects.filter(client_code="ACME01").exists()


def test_an_upload_says_out_loud_that_it_has_written_nothing(client, staff):
    """The step everybody misses.

    A preview of rows that are all correct looks exactly like a finished
    import - the reader then goes looking for accounts that were never written
    and concludes the sheet did nothing. It is the one place in the application
    where success and not-yet-done look the same, so it is said three times:
    here, on the preview, and in the list of batches.
    """
    login(client, staff["senior"])
    upload = SimpleUploadedFile(
        "portfolio.xlsx", sheet([a_row()]).getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    response = client.post(
        reverse("importer:upload"), {"upload": upload, "kind": "ACCOUNTS"}, follow=True
    )
    body = response.content.decode()

    assert "Nothing has been imported yet" in body
    assert "press Import to write it" in body
    assert "Import 1 row" in body, "and the button says how many"
    assert Account.objects.count() == 0


def test_a_batch_left_unapplied_is_listed_as_unfinished(client, staff):
    """"Validated" is the truth and reads as success.

    A sheet somebody uploaded and never finished should say so in the word the
    reader is looking for, and offer the way to finish it.
    """
    login(client, staff["senior"])
    upload = SimpleUploadedFile(
        "portfolio.xlsx", sheet([a_row()]).getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    client.post(
        reverse("importer:upload"), {"upload": upload, "kind": "ACCOUNTS"}, follow=True
    )

    body = client.get(reverse("importer:list")).content.decode()
    assert "Not imported" in body
    assert "Finish import" in body

    batch = ImportBatch.objects.get()
    client.post(reverse("importer:apply", args=[batch.pk]), follow=True)

    body = client.get(reverse("importer:list")).content.decode()
    assert "Not imported" not in body
    assert "Finish import" not in body


def test_an_applied_batch_no_longer_offers_to_import(client, staff):
    """Re-pressing it would be a second write of the same sheet."""
    login(client, staff["senior"])
    upload = SimpleUploadedFile(
        "portfolio.xlsx", sheet([a_row()]).getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # follow=True so the upload's own "nothing written yet" message is consumed
    # by the preview it redirects to, rather than queueing for the next page.
    client.post(
        reverse("importer:upload"), {"upload": upload, "kind": "ACCOUNTS"}, follow=True
    )
    batch = ImportBatch.objects.get()
    client.post(reverse("importer:apply", args=[batch.pk]), follow=True)

    body = client.get(reverse("importer:preview", args=[batch.pk])).content.decode()
    assert "Nothing has been imported yet" not in body
    assert "Applied" in body


def test_the_legacy_xls_format_is_refused_by_the_upload_form(client, staff):
    login(client, staff["senior"])
    response = client.post(
        reverse("importer:upload"),
        {"upload": SimpleUploadedFile("portfolio.xls", b"old format"), "kind": "ACCOUNTS"},
    )
    assert response.status_code == 200
    assert ImportBatch.objects.count() == 0


def test_an_applied_batch_records_who_and_when(db, staff):
    batch = make_batch(db, staff, [a_row()])
    services.apply(batch, actor=staff["senior"])
    batch.refresh_from_db()

    assert batch.applied_by == staff["senior"]
    assert batch.applied_at is not None
    assert batch.rows.first().account is not None
    assert batch.rows.first().action == RowAction.CREATE


def test_an_existing_account_is_not_touched_by_a_failed_row(db, staff):
    AccountFactory(client_code="ACME01", client_name="Original")
    batch = make_batch(db, staff, [a_row(**{"Country": "Freedonia"})])

    services.apply(batch, actor=staff["senior"])
    assert Account.objects.get(client_code="ACME01").client_name == "Original"


# ---------------------------------------------------------------------------
# License means the seats the client bought
# ---------------------------------------------------------------------------


def test_the_seat_count_imports_from_its_own_column(emails):
    rows, unknown = parse(sheet([a_row(**{"BOQ Users Count": 750})]), people=emails)

    assert unknown == []
    assert rows[0].errors == []
    assert rows[0].cleaned["boq_users_count"] == 750


def test_a_word_in_the_seat_column_is_an_error(emails):
    rows, _ = parse(sheet([a_row(**{"BOQ Users Count": "lots"})]), people=emails)
    assert any(e["column"] == "BOQ users" for e in rows[0].errors)


def test_applying_writes_the_licensed_seats_and_the_total(db, staff):
    batch = make_batch(db, staff, [a_row(**{"BOQ Users Count": 750})])
    services.apply(batch, actor=staff["senior"])

    account = Account.objects.get(client_code="ACME01")
    assert account.boq_users_count == 750
    assert account.added_users_count == 50
    assert account.total_users_count == 800


# ---------------------------------------------------------------------------
# CSM and ACM: email only
# ---------------------------------------------------------------------------


def test_an_email_alone_resolves_the_person(emails, staff):
    rows, _ = parse(
        sheet([a_row(**{"CSM Email": "imp-csm@dphish.com"})]), people=emails
    )
    assert rows[0].errors == []
    assert rows[0].cleaned["csm_id"] == staff["csm"].pk


def test_an_empty_csm_column_imports_unassigned_with_a_warning(emails):
    rows, _ = parse(sheet([a_row(**{"CSM Email": ""})]), people=emails)
    assert rows[0].errors == []
    assert rows[0].cleaned["csm_id"] is None
    assert any("unassigned" in w["message"] for w in rows[0].warnings)


def test_the_template_offers_one_email_column_per_role():
    from csm.importer.parsing import COLUMNS

    assert "csm" in COLUMNS
    assert "acm" in COLUMNS
    assert "csm_name" not in COLUMNS
    assert "acm_name" not in COLUMNS
    assert header_label("csm") == "CSM Email"
    assert header_label("acm") == "ACM Email"
