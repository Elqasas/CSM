from __future__ import annotations

from django import forms

from csm.schedule.models import CalendarPreference


class CalendarPreferenceForm(forms.ModelForm):
    class Meta:
        model = CalendarPreference
        fields = [
            "first_weekday",
            "default_view",
            "show_weekends",
            "show_projected",
            "show_happened",
        ]
        labels = {
            "first_weekday": "Week starts on",
            "default_view": "Open on",
            "show_weekends": "Show the weekend",
            "show_projected": "Show repeats before they are raised",
            "show_happened": "Show what happened",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            else:
                widget.attrs.setdefault("class", "form-select")
