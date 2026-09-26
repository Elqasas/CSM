"""The manual override form.

N/A is absent from the choices because ``apply_override`` refuses it: N/A means
"this lifecycle stage is not scored", which is a fact about the stage rather
than a judgement anyone can make. The ten-character minimum on the reason is
enforced in the service too; it is repeated here only so the user is told before
the round trip.
"""

from __future__ import annotations

from django import forms

from csm.core.enums import HealthStatus

OVERRIDABLE_STATUSES = [
    (value, label)
    for value, label in HealthStatus.choices
    if value != HealthStatus.NOT_APPLICABLE
]


class OverrideForm(forms.Form):
    status = forms.ChoiceField(
        choices=OVERRIDABLE_STATUSES,
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="The status to show instead of the computed one.",
    )
    reason = forms.CharField(
        min_length=10,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        help_text=(
            "Why the computed score is wrong for this account. Recorded against "
            "the override and kept after it is cleared."
        ),
    )
