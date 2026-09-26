"""The upload form.

The size and extension checks are here rather than in the parser because they
are about accepting a file at all, not about what it contains. openpyxl will
happily begin reading a two-gigabyte upload before deciding it is not a
workbook.
"""

from __future__ import annotations

from django import forms

from csm.importer.models import ImportKind

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_SUFFIXES = (".xlsx", ".xlsm", ".csv")


class UploadForm(forms.Form):
    # Asked rather than sniffed from the headers. Both files carry a client-code
    # column, and guessing wrong would write to the wrong table.
    kind = forms.ChoiceField(
        choices=ImportKind.choices,
        initial=ImportKind.ACCOUNTS,
        label="What is in this file?",
        widget=forms.RadioSelect,
    )

    upload = forms.FileField(
        label="Spreadsheet",
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".xlsx,.xlsm,.csv"}
        ),
        help_text=".xlsx or .csv. Download a template if you are unsure of the headers.",
    )

    def __init__(self, *args, ability=None, **kwargs):
        super().__init__(*args, **kwargs)
        if ability is None:
            raise ValueError("UploadForm needs an Ability to decide which kinds it offers.")

        allowed = ability.importable_kinds()
        self.fields["kind"].choices = [
            (value, label) for value, label in ImportKind.choices if value in allowed
        ]
        if ImportKind.ACCOUNTS not in allowed and allowed:
            self.fields["kind"].initial = next(iter(allowed))

    def clean_upload(self):
        upload = self.cleaned_data["upload"]

        name = (upload.name or "").lower()
        if not name.endswith(ALLOWED_SUFFIXES):
            raise forms.ValidationError(
                "Save the sheet as .xlsx or .csv first. The older .xls format is "
                "not read by this importer."
            )

        if upload.size > MAX_UPLOAD_BYTES:
            megabytes = MAX_UPLOAD_BYTES // (1024 * 1024)
            raise forms.ValidationError(f"That file is larger than {megabytes}MB.")

        return upload
