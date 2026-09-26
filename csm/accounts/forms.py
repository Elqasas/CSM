"""Account forms, narrowed to what the user may actually change.

The field list is decided by ``Ability.editable_account_fields`` rather than
declared here. That inverts the usual arrangement on purpose: a form that
declares its own fields and then hides some is one edit away from exposing a
field again, whereas a form built from the ability cannot show a field the
access layer does not name.
"""

from __future__ import annotations

import datetime as dt

from django import forms
from django.conf import settings
from django.db import models

from csm.accounts.models import Account, AccountCustomFieldDefinition, AccountCustomFieldValue
from csm.core.access import capabilities as cap
from csm.core.enums import CustomFieldType

#: Everything a form could ever offer, before the ability narrows it. System
#: columns are absent: they are written by the health service, the importer or
#: the database, never by a person.
CANDIDATE_FIELDS = [
    "client_code",
    "client_name",
    "country",
    "location",
    "license_type",
    "deployment",
    "is_managed_service",
    "boq_users_count",
    "added_users_count",
    "contract_start_date",
    "contract_end_date",
    "renewal_count",
    "lifecycle_stage",
    "csm",
    "acm",
    "partner",
    "quarterly_meeting_date",
    "required_meeting_frequency",
    "events_count",
    "digital_content_count",
    "event_location",
    "g2_review_status",
    "g2_review_url",
    "gartner_review_status",
    "gartner_review_url",
    "account_class",
    "size",
    "website_url",
    "tenant_url",
    "drive_url",
    "linkedin_url",
    "description",
    "logo",
]

DATE_FIELDS = (
    "contract_start_date",
    "contract_end_date",
    "quarterly_meeting_date",
)


#: The logo is shown at 50x50 everywhere it appears, so anything larger is
#: bytes nobody sees. PNG only: it is the one common format that keeps a
#: transparent background, and a brand mark on a white square inside a dark
#: interface looks broken.
LOGO_BOX = 50


def validate_png_logo(upload):
    """Accept a PNG, and prove it is one rather than believing the filename.

    An extension check alone is worthless here - anything can be renamed - and
    what matters is what the browser will be asked to render. Pillow is already
    a dependency because ImageField needs it, so verifying costs nothing.
    """
    from PIL import Image, UnidentifiedImageError

    name = (getattr(upload, "name", "") or "").lower()
    if not name.endswith(".png"):
        raise forms.ValidationError("The logo must be a PNG file.")

    limit = getattr(settings, "ACCOUNT_LOGO_MAX_BYTES", 2 * 1024 * 1024)
    if getattr(upload, "size", 0) > limit:
        raise forms.ValidationError(
            f"That image is larger than {limit // (1024 * 1024)}MB. It is shown "
            f"at {LOGO_BOX} by {LOGO_BOX} pixels, so it need not be big."
        )

    try:
        upload.seek(0)
        image = Image.open(upload)
        image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise forms.ValidationError("That file is not a readable image.") from exc
    finally:
        upload.seek(0)

    if (image.format or "").upper() != "PNG":
        raise forms.ValidationError(
            f"That file is named .png but is actually {image.format or 'unknown'}. "
            "Re-save it as a PNG."
        )

    return upload


def _url_field(label: str) -> forms.URLField:
    """A URL field that states its scheme assumption.

    Django 6 changes the default from http to https and warns until the
    assumption is explicit. Saying https here rather than setting the
    transitional ``FORMS_URLFIELD_ASSUME_HTTPS`` flag means nothing has to be
    unpicked when that flag is itself removed.
    """
    return forms.URLField(
        required=False, max_length=500, label=label, assume_scheme="https"
    )


class AccountForm(forms.ModelForm):
    # A FileField, not an ImageField, so this form's validator runs first and
    # owns every message. Django's ImageField verifies the image before any
    # custom clean, so a corrupt file would be rejected with "Upload a valid
    # image" and an oversized one would never reach the size check at all.
    logo = forms.FileField(
        required=False,
        label="Logo",
        widget=forms.ClearableFileInput(attrs={"accept": "image/png"}),
        help_text=(
            f"PNG only, up to 2MB. Shown at {LOGO_BOX}×{LOGO_BOX} pixels, so a "
            "square mark with a transparent background works best. Accounts "
            "without one get their initials instead."
        ),
    )

    website_url = _url_field("Website")
    tenant_url = _url_field("Tenant URL")
    drive_url = _url_field("Drive URL")
    linkedin_url = _url_field("LinkedIn")
    g2_review_url = _url_field("G2 review URL")
    gartner_review_url = _url_field("Gartner review URL")

    class Meta:
        model = Account
        fields = CANDIDATE_FIELDS

    def __init__(self, *args, ability=None, **kwargs):
        super().__init__(*args, **kwargs)
        if ability is None:
            raise ValueError("AccountForm needs an Ability to decide its fields.")

        allowed = ability.editable_account_fields(CANDIDATE_FIELDS)
        for name in list(self.fields):
            if name not in allowed:
                del self.fields[name]

        # Who may own an account. The ability decides: an administrator sees
        # every account and is the CSM of record on none, a manager reads rather
        # than owns, and a plain CSM may only take an account on themselves.
        from csm.users.models import User

        for name, eligible in (
            ("csm", ability.assignable_csms()),
            ("acm", ability.assignable_acms()),
        ):
            if name not in self.fields:
                continue
            # Whoever holds the account now stays selectable even if they no
            # longer qualify - somebody who has moved role, or left. Otherwise
            # editing an unrelated field on their account fails validation on a
            # value the form did not offer and the editor never touched.
            held_by = getattr(self.instance, f"{name}_id", None)
            if held_by:
                eligible = User.objects.filter(
                    models.Q(pk__in=eligible.values("pk")) | models.Q(pk=held_by)
                )
            self.fields[name].queryset = eligible.order_by("full_name")

        for name in DATE_FIELDS:
            if name in self.fields:
                self.fields[name].widget = forms.DateInput(
                    attrs={"type": "date"}, format="%Y-%m-%d"
                )

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            elif isinstance(widget, forms.FileInput):
                widget.attrs.setdefault("class", "form-control")
            else:
                widget.attrs.setdefault("class", "form-control")
            if name == "description":
                widget.attrs.setdefault("rows", 3)

        # Admin-defined, one input per active definition - added here rather
        # than in CANDIDATE_FIELDS because they aren't columns on the model at
        # all. Gated on the ability rather than assumed from the view, for the
        # same reason every other field on this form is: a form built from the
        # ability cannot show a field access does not name.
        self._custom_field_defs = []
        if ability.can(cap.ACCOUNT_EDIT):
            self._custom_field_defs = list(
                AccountCustomFieldDefinition.objects.filter(is_active=True)
            )
            existing = {}
            if self.instance.pk:
                existing = dict(
                    self.instance.custom_field_values.values_list(
                        "definition_id", "value"
                    )
                )
            custom_names = []
            for definition in self._custom_field_defs:
                name = f"custom_{definition.pk}"
                custom_names.append(name)
                self.fields[name] = self._custom_field(
                    definition, existing.get(definition.pk, "")
                )

            # Placed just before "description", which is where the request for
            # this feature asked new fields to show up - not wherever
            # CANDIDATE_FIELDS happens to end.
            if custom_names and "description" in self.fields:
                order = [f for f in self.fields if f not in custom_names]
                order[order.index("description") : order.index("description")] = (
                    custom_names
                )
                self.order_fields(order)

    @staticmethod
    def _custom_field(definition, raw_value: str):
        if definition.field_type == CustomFieldType.DATETIME:
            initial = None
            if raw_value:
                try:
                    initial = dt.datetime.fromisoformat(raw_value)
                except ValueError:
                    initial = None
            field = forms.DateTimeField(
                required=False,
                label=definition.name,
                initial=initial,
                widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
            )
        elif definition.field_type == CustomFieldType.DROPDOWN:
            options = definition.choice_list()
            field = forms.ChoiceField(
                required=False,
                label=definition.name,
                choices=[("", "—")] + [(o, o) for o in options],
                initial=raw_value,
            )
        else:
            field = forms.CharField(
                required=False, label=definition.name, initial=raw_value
            )
        field.widget.attrs.setdefault(
            "class", "form-select" if isinstance(field, forms.ChoiceField) else "form-control"
        )
        return field

    def clean_logo(self):
        upload = self.cleaned_data.get("logo")
        # Unchanged on an edit: Django hands back the stored file, which has
        # already been through this once and has no `size` to re-read.
        if not upload or not hasattr(upload, "file"):
            return upload
        return validate_png_logo(upload)

    def save(self, commit=True):
        account = super().save(commit=commit)
        if commit:
            self._save_custom_fields(account)
        return account

    def _save_custom_fields(self, account) -> None:
        for definition in self._custom_field_defs:
            value = self.cleaned_data.get(f"custom_{definition.pk}", "")
            # The database column is text regardless of rendering, so a
            # datetime is written the one way it can be read back exactly.
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            AccountCustomFieldValue.objects.update_or_create(
                account=account, definition=definition, defaults={"value": value or ""}
            )


class CustomFieldDefinitionForm(forms.ModelForm):
    """Naming a new custom field and choosing what it renders as."""

    choices_text = forms.CharField(
        required=False,
        label="Dropdown options",
        help_text="One option per line. Only used when Type is Dropdown list.",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )

    class Meta:
        model = AccountCustomFieldDefinition
        fields = ["name", "field_type", "choices_text"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "field_type": forms.Select(attrs={"class": "form-select"}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("This field is required.")
        clash = AccountCustomFieldDefinition.objects.filter(name__iexact=name)
        if clash.exists():
            raise forms.ValidationError("A field with this name already exists.")

        # Each field is a column in the import and export sheets, matched by
        # its name folded the way the importer folds headers. A name that folds
        # onto a built-in column, or onto another field, would make two columns
        # indistinguishable - "Go-Live" and "Go Live" are one header there.
        from csm.importer.parsing import builtin_aliases, normalise_header

        folded = normalise_header(name)
        if folded in builtin_aliases():
            raise forms.ValidationError(
                "This name is already a column in the account import sheet."
            )
        for existing in AccountCustomFieldDefinition.objects.values_list("name", flat=True):
            if normalise_header(existing) == folded:
                raise forms.ValidationError(
                    f"Too close to the existing field {existing!r}; the import "
                    "sheet could not tell the two apart."
                )
        return name

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("field_type") == CustomFieldType.DROPDOWN and not (
            cleaned.get("choices_text") or ""
        ).strip():
            self.add_error(
                "choices_text", "List at least one option for a dropdown field."
            )
        return cleaned
