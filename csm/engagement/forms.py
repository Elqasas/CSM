"""Forms for the three scoring inputs and the engagement trend.

Everything here feeds the health engine. Saving one of these rows fires the
model-level hook in ``health/signals.py`` and the account rescores on commit;
none of these forms calls the engine itself, which is the point of putting the
hook at the model layer rather than in a view.
"""

from __future__ import annotations

from django import forms

from csm.contacts.models import Contact
from csm.core.clock import today
from csm.core.enums import TicketStatus
from csm.engagement.models import AdoptionEvent, CsatResponse, EngagementItem, Ticket


def _style(form) -> None:
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.Select):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")
        if name in ("description", "notes", "comment"):
            widget.attrs.setdefault("rows", 3)


class TicketForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = ["ticket_kind", "status", "title", "description", "opened_on", "closed_on"]
        widgets = {
            "opened_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "closed_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.initial.get("opened_on"):
            self.fields["opened_on"].initial = today()
        self.fields["closed_on"].help_text = (
            "Required when the status is Closed, and must be empty otherwise."
        )
        self.fields["ticket_kind"].help_text = (
            "Bugs weigh three times a general ticket: one bug scores yellow, "
            "two score red."
        )
        _style(self)

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("status")
        closed_on = cleaned.get("closed_on")
        opened_on = cleaned.get("opened_on")

        # The model carries this as a check constraint. Saying it here means the
        # user is told which field to fix rather than shown an IntegrityError.
        if status == TicketStatus.CLOSED and not closed_on:
            self.add_error("closed_on", "A closed ticket needs a closing date.")
        if status != TicketStatus.CLOSED and closed_on:
            self.add_error("closed_on", "Only a closed ticket has a closing date.")
        if opened_on and closed_on and closed_on < opened_on:
            self.add_error("closed_on", "A ticket cannot close before it opened.")
        if opened_on and opened_on > today():
            self.add_error("opened_on", "A ticket cannot be opened in the future.")
        return cleaned


class CsatForm(forms.ModelForm):
    class Meta:
        model = CsatResponse
        fields = ["score", "recorded_on", "contact", "comment"]
        widgets = {
            "recorded_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "score": forms.RadioSelect(choices=[(n, str(n)) for n in range(1, 6)]),
        }

    def __init__(self, *args, account=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = (
            Contact.objects.filter(account=account).active()
            if account is not None
            else Contact.objects.none()
        )
        self.fields["contact"].required = False
        self.fields["contact"].empty_label = "Not attributed to one person"
        if not self.instance.pk and not self.initial.get("recorded_on"):
            self.fields["recorded_on"].initial = today()
        self.fields["score"].help_text = (
            "Four or five is green, three is yellow, one or two is red. A rating "
            "older than its validity window scores fifty, the same as no rating."
        )
        _style(self)
        # A radio group is not a form-select; the styling loop would fight it.
        self.fields["score"].widget.attrs.pop("class", None)

    def clean_recorded_on(self):
        recorded_on = self.cleaned_data["recorded_on"]
        if recorded_on > today():
            raise forms.ValidationError("A rating cannot be recorded in the future.")
        return recorded_on


class AdoptionEventForm(forms.ModelForm):
    class Meta:
        model = AdoptionEvent
        fields = ["event_type", "occurred_on", "title", "notes"]
        widgets = {
            "occurred_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.initial.get("occurred_on"):
            self.fields["occurred_on"].initial = today()
        self.fields["event_type"].help_text = (
            "Monthly Adoption measures days since the most recent of either kind."
        )
        _style(self)

    def clean_occurred_on(self):
        occurred_on = self.cleaned_data["occurred_on"]
        if occurred_on > today():
            raise forms.ValidationError("An adoption event cannot be in the future.")
        return occurred_on


class EngagementItemForm(forms.ModelForm):
    class Meta:
        model = EngagementItem
        fields = ["item_type", "occurred_on", "title", "location", "quantity"]
        widgets = {
            "occurred_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.initial.get("occurred_on"):
            self.fields["occurred_on"].initial = today()
        self.fields["item_type"].help_text = (
            "Plotted on the dashboard trend. Unlike the imported scalar counts, "
            "these carry a date and can sit on a time axis."
        )
        _style(self)

    def clean_occurred_on(self):
        occurred_on = self.cleaned_data["occurred_on"]
        if occurred_on > today():
            raise forms.ValidationError("This cannot be dated in the future.")
        return occurred_on
