"""Downloadable CSV templates for the accounts and contacts uploads.

Headers come from the same column tables the parsers match against, so a
template cannot drift into emitting a header the importer no longer reads.

A CSV has nowhere to put an instructions sheet, so the guidance goes in comment
lines above the header, each prefixed with ``#``. The reader skips them: a row
whose first cell starts with ``#`` is not data. Putting the notes below the
header instead would import them as a client called "Required. The natural key",
which is the trap the Excel template already had to avoid.

One example row is included and is also commented out, so the file opens with
something to copy without importing a fictional client on the first upload.
"""

from __future__ import annotations

import csv
import io

from csm.importer.contact_parsing import CONTACT_COLUMNS, CONTACT_REQUIRED_COLUMNS
from csm.importer.parsing import REQUIRED_COLUMNS, account_columns
from csm.importer.template import account_guidance, header_label

CONTACT_GUIDANCE = {
    "client_code": "Required. Must match an account that already exists.",
    "full_name": "Required. Rows match on account plus name.",
    "influence": "Optional: High / Medium / Low. Blank means not assessed.",
    "interest": "Optional: High / Medium / Low. Blank means not assessed.",
    "channels": "Any of Email, Phone, WhatsApp, Teams, In person. Separate with commas.",
    "preferred_channel": "Must be one of the channels listed above.",
    "focal_rank": "1 or 2 for the focal points, blank for everyone else.",
    "title": "Job title or role.",
}

ACCOUNT_EXAMPLE = {
    "client_code": "ACME01",
    "client_name": "Acme Industrial",
    "country": "Egypt",
    "location": "Cairo",
    "deployment": "Cloud",
    "is_managed_service": "Yes",
    "license_type": "Complete",
    "boq_users_count": "500",
    "added_users_count": "50",
    "contract_start_date": "2026-01-01",
    "contract_end_date": "2027-12-31",
    "csm": "casey.morgan@dphish.com",
    "acm": "alex.chen@dphish.com",
    "partner": "Regional Reseller",
    "account_class": "VIP",
    "size": "Enterprise",
    "lifecycle_stage": "Active",
    "required_meeting_frequency": "Weekly",
    "renewal_count": "1",
    "website_url": "https://acme.example",
    "linkedin_url": "https://www.linkedin.com/company/acme",
    "description": "Regional manufacturer; renewal handled through the partner.",
}

CONTACT_EXAMPLE = {
    "client_code": "ACME01",
    "full_name": "Dana Farouk",
    "title": "CISO",
    "email": "dana.farouk@acme.example",
    "phone": "+20 100 000 0000",
    "influence": "High",
    "interest": "Medium",
    "channels": "Email, Teams",
    "preferred_channel": "Email",
    "focal_rank": "1",
}


def _render(columns, required, guidance, example, title: str,
            custom_fields=()) -> bytes:
    def label_of(field_name):
        return header_label(field_name, columns, custom_fields)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    writer.writerow([f"# {title}"])
    writer.writerow(["# Lines starting with a hash are ignored by the importer."])
    writer.writerow(["# Columns marked * are required."])
    writer.writerow(["#"])
    for field_name in columns:
        note = guidance.get(field_name, "")
        if note:
            star = " *" if field_name in required else ""
            writer.writerow([f"# {label_of(field_name)}{star}: {note}"])
    writer.writerow(["#"])

    header = []
    for field_name in columns:
        label = label_of(field_name)
        header.append(f"{label} *" if field_name in required else label)
    writer.writerow(header)

    writer.writerow([f"# {example.get(name, '')}" if index == 0 else example.get(name, "")
                     for index, name in enumerate(columns)])

    # A UTF-8 BOM so Excel opens accented names correctly instead of as mojibake.
    return "﻿".encode() + buffer.getvalue().encode("utf-8")


def build_accounts_csv(custom_fields=()) -> bytes:
    return _render(
        account_columns(custom_fields), REQUIRED_COLUMNS,
        account_guidance(custom_fields), ACCOUNT_EXAMPLE,
        "Accounts template", custom_fields,
    )


def build_contacts_csv() -> bytes:
    return _render(
        CONTACT_COLUMNS, CONTACT_REQUIRED_COLUMNS, CONTACT_GUIDANCE, CONTACT_EXAMPLE,
        "Contacts template",
    )
