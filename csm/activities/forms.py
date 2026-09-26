"""Activity forms.

The contact field is deliberately prominent rather than buried at the bottom:
the specification kept it optional at the database level but asked that the form
encourage it, because a contact-less activity log cannot answer "when did anyone
last speak to this person".
"""

from __future__ import annotations

from django import forms

from csm.activities.models import Activity
from csm.contacts.models import Contact
from csm.core.clock import today


class ActivityForm(forms.ModelForm):
    class Meta:
        model = Activity
        fields = ["activity_type", "contact", "occurred_on", "subject", "notes"]
        widgets = {
            "occurred_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, account=None, **kwargs):
        super().__init__(*args, **kwargs)

        if account is not None:
            self.fields["contact"].queryset = Contact.objects.filter(
                account=account
            ).active()
        else:
            self.fields["contact"].queryset = Contact.objects.none()
        self.fields["contact"].empty_label = "Nobody in particular"
        self.fields["contact"].required = False

        if not self.initial.get("occurred_on"):
            self.fields["occurred_on"].initial = today()

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")
            if name == "notes":
                widget.attrs.setdefault("rows", 4)

    def clean_occurred_on(self):
        occurred_on = self.cleaned_data["occurred_on"]
        if occurred_on > today():
            raise forms.ValidationError("An activity cannot be logged in the future.")
        return occurred_on


class ActivityVoidForm(forms.Form):
    """Voiding is an edit to the record of what happened, so it takes a reason."""

    void_reason = forms.CharField(
        max_length=255,
        label="Reason",
        widget=forms.TextInput(attrs={"class": "form-control"}),
        help_text="Why this entry is wrong. Kept on the row, which is not deleted.",
    )
