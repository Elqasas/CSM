"""Contact forms.

Influence and interest offer a blank option that means "not assessed", not a
default. That blank is the whole point of making the columns nullable: a form
that quietly pre-selects Medium would put the contact on the grid without
anybody having decided anything.
"""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from csm.contacts.models import Contact, ContactChannel
from csm.core.enums import CommunicationChannel, Level

LEVEL_CHOICES = [("", "Not assessed")] + list(Level.choices)


class ContactAssessmentForm(forms.Form):
    """Just the two axes, for inline triage."""

    influence = forms.ChoiceField(
        choices=Level.choices,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    interest = forms.ChoiceField(
        choices=Level.choices,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )


class ContactForm(forms.ModelForm):
    channels = forms.MultipleChoiceField(
        choices=CommunicationChannel.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="How this person can be reached.",
    )

    class Meta:
        model = Contact
        fields = [
            "full_name",
            "title",
            "email",
            "phone",
            "influence",
            "interest",
            "preferred_channel",
            "is_focal_point",
            "is_active",
            "notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["email"].required = True

        self.fields["influence"].choices = LEVEL_CHOICES
        self.fields["interest"].choices = LEVEL_CHOICES
        self.fields["influence"].required = False
        self.fields["interest"].required = False

        self.fields["preferred_channel"].choices = [("", "No preference")] + list(
            CommunicationChannel.choices
        )

        if self.instance.pk:
            self.fields["channels"].initial = list(
                self.instance.channels.values_list("channel", flat=True)
            )

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.CheckboxSelectMultiple):
                continue
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")
            if name == "notes":
                widget.attrs.setdefault("rows", 3)

    def clean_influence(self):
        return self.cleaned_data.get("influence") or None

    def clean_interest(self):
        return self.cleaned_data.get("interest") or None

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if not email:
            raise forms.ValidationError("This field is required.")
        return email

    def clean(self):
        cleaned = super().clean()
        preferred = cleaned.get("preferred_channel")
        chosen = set(cleaned.get("channels") or [])
        if preferred and chosen and preferred not in chosen:
            self.add_error(
                "preferred_channel",
                "The preferred channel must be one of the selected channels.",
            )

        # Focal rank isn't a form field - it's bookkeeping the database needs
        # to keep at most two per account, not something anyone chooses - so
        # it's assigned on the instance directly rather than through
        # cleaned_data, which construct_instance only reads back for fields
        # the form declares.
        if "is_focal_point" in cleaned:
            if not self.instance.account_id:
                account = self.cleaned_data.get("account")
                if account:
                    self.instance.account = account
            try:
                self.instance.set_focal_point(cleaned["is_focal_point"])
            except ValidationError as exc:
                self.add_error("is_focal_point", exc)

        return cleaned

    def save(self, commit=True):
        contact = super().save(commit=commit)
        if commit:
            self.sync_channels(contact)
        return contact

    def sync_channels(self, contact) -> None:
        """Write the channel rows. Called explicitly after a deferred save.

        Public, and not hooked to ``save_m2m``, because ``save(commit=False)``
        makes Django bind its own ``_save_m2m`` onto the instance under that
        name - which shadows any override the form declares. A create view then
        calls ``form.save_m2m()``, gets Django's version, and the channels are
        dropped without a word. Editing still worked, which is exactly why it
        went unnoticed.
        """
        if contact.pk is None:
            return
        wanted = set(self.cleaned_data.get("channels") or [])
        existing = set(contact.channels.values_list("channel", flat=True))

        contact.channels.filter(channel__in=existing - wanted).delete()
        ContactChannel.objects.bulk_create(
            [ContactChannel(contact=contact, channel=channel) for channel in wanted - existing]
        )


class ContactAnywhereForm(ContactForm):
    """Add a contact without starting from an account page.

    The account becomes a field rather than a URL segment. Its queryset is
    passed in already scoped to what the user may see, so the picker cannot
    offer somebody else's client - the same rule as everywhere else, expressed
    as the contents of a dropdown.
    """

    account = forms.ModelChoiceField(
        queryset=None,
        empty_label="Choose an account",
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Which client this person works for.",
    )

    field_order = ["account", "full_name", "title", "email", "phone"]

    class Meta(ContactForm.Meta):
        pass

    def __init__(self, *args, accounts=None, **kwargs):
        super().__init__(*args, **kwargs)
        if accounts is None:
            raise ValueError("ContactAnywhereForm needs the accounts this user may see.")
        self.fields["account"].queryset = accounts.order_by("client_name")
        self.order_fields(self.field_order)

    def save(self, commit=True):
        self.instance.account = self.cleaned_data["account"]
        return super().save(commit=commit)
