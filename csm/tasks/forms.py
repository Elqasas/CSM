"""Task forms.

Only the manual task type is offered. The three automatic types are created by
generators against an idempotency key, and a hand-made one would either collide
with that key or, worse, not collide and duplicate the obligation.
"""

from __future__ import annotations

from django import forms
from django.urls import reverse

from csm.contacts.models import Contact
from csm.core.clock import today
from csm.core.enums import (
    DEFAULT_TASK_PRIORITY,
    MANUAL_TASK_TYPES,
    SELECTABLE_TASK_PROGRESS,
    RepeatEnd,
    RepeatFrequency,
    TaskCategory,
    TaskPriority,
    TaskProgress,
    TaskType,
)


class TaskForm(forms.Form):
    task_type = forms.ChoiceField(
        choices=[(value, value.label) for value in MANUAL_TASK_TYPES],
        initial=TaskType.MANUAL_NEXT_ACTION,
        label="Kind",
        help_text=(
            "A next action, a meeting to arrange, or a call to make. The "
            "generated kinds are not on this list: they are written against an "
            "idempotency key, and a hand-made copy would duplicate the "
            "obligation rather than replace it."
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    title = forms.CharField(
        max_length=255, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    due_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}, format="%Y-%m-%d")
    )
    due_time = forms.TimeField(
        required=False,
        label="Time (optional)",
        widget=forms.TimeInput(attrs={"type": "time", "class": "form-control"}, format="%H:%M"),
        help_text="Leave empty for a task due any time that day.",
    )
    category = forms.ChoiceField(
        choices=TaskCategory.choices,
        # Client-facing by default: most of what somebody types in here is work
        # with the client, and this is the field that decides whether completing
        # it counts as contact towards the health score. Getting the common case
        # right matters more than the safe-looking one.
        initial=TaskCategory.CLIENT_FACING,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    progress = forms.TypedChoiceField(
        choices=[(value, value.label) for value in SELECTABLE_TASK_PROGRESS],
        initial=TaskProgress.NOT_STARTED,
        label="Status",
        # A short closed list reads better as one row of choices than as a
        # dropdown that hides three of its four options behind a click.
        widget=forms.RadioSelect(attrs={"class": "btn-check", "autocomplete": "off"}),
        help_text=(
            "Choose Completed for something that has already happened - it "
            "closes the task and writes it to the account's log."
        ),
    )
    priority = forms.TypedChoiceField(
        choices=TaskPriority.choices,
        initial=DEFAULT_TASK_PRIORITY,
        help_text="How much this matters, as distinct from when it is due.",
        widget=forms.RadioSelect(attrs={"class": "btn-check", "autocomplete": "off"}),
    )
    contact = forms.ModelChoiceField(
        queryset=Contact.objects.none(),
        required=False,
        empty_label="Nobody in particular",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    # -- Repeat ----------------------------------------------------------
    # A repeat is a rule, not a hundred rows written in advance: see
    # ``TaskSeries``. These four describe the rule; the first occurrence is the
    # task being created here.
    repeat = forms.BooleanField(
        required=False,
        label="Repeat",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "role": "switch"}),
    )
    repeat_frequency = forms.ChoiceField(
        required=False,
        choices=RepeatFrequency.choices,
        initial=RepeatFrequency.WEEKLY,
        label="Repeat type",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    repeat_ends = forms.ChoiceField(
        required=False,
        choices=RepeatEnd.choices,
        initial=RepeatEnd.NEVER,
        label="Ends",
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )
    repeat_count = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=365,
        label="Times",
        widget=forms.NumberInput(attrs={"class": "form-control form-control-sm"}),
    )
    repeat_until = forms.DateField(
        required=False,
        label="Until",
        widget=forms.DateInput(
            attrs={"type": "date", "class": "form-control form-control-sm"},
            format="%Y-%m-%d",
        ),
    )

    def __init__(self, *args, account=None, editing=False, generated=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.editing = editing
        # A generated task keeps its kind. The idempotency key is built from it,
        # so changing it here would orphan the task from the generator that owns
        # it and the next run would raise a second one beside it.
        if generated:
            del self.fields["task_type"]
        if account is not None:
            self.fields["contact"].queryset = Contact.objects.filter(
                account=account
            ).active()

    def clean(self):
        """A repeat that cannot be read is refused, not guessed at.

        An end condition with nothing behind it - "after" with no number, "on"
        with no date - would otherwise become "never", which is the one answer
        nobody can undo by waiting.
        """
        cleaned = super().clean()
        if not cleaned.get("repeat"):
            return cleaned

        if not cleaned.get("repeat_frequency"):
            self.add_error("repeat_frequency", "Choose how often this repeats.")

        ends = cleaned.get("repeat_ends") or RepeatEnd.NEVER
        cleaned["repeat_ends"] = ends

        if ends == RepeatEnd.AFTER and not cleaned.get("repeat_count"):
            self.add_error("repeat_count", "How many times? Or choose Never.")
        if ends == RepeatEnd.ON:
            until = cleaned.get("repeat_until")
            due = cleaned.get("due_date")
            if not until:
                self.add_error("repeat_until", "Give the date it stops on.")
            elif due and until <= due:
                self.add_error(
                    "repeat_until",
                    "That is on or before the first one, so it would never "
                    "repeat at all.",
                )
        return cleaned

    def clean_due_date(self):
        """A new task cannot start life overdue. An existing one already is.

        Enforcing this on edit too would make an overdue task uneditable: you
        could not correct its title without also moving its date, which is the
        opposite of what someone chasing an overdue task wants.
        """
        due_date = self.cleaned_data["due_date"]
        if not self.editing and due_date < today():
            raise forms.ValidationError("A new task cannot already be overdue.")
        return due_date


class TaskAnywhereForm(TaskForm):
    """Create a task without starting from an account page.

    The account is a field, and the contact list spans every account the user
    may see - so the pair has to be checked: a contact at one client attached to
    a task at another would be nonsense the database has no opinion about.
    """

    account = forms.ModelChoiceField(
        queryset=None,
        empty_label="Choose an account",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, accounts=None, **kwargs):
        super().__init__(*args, **kwargs)
        if accounts is None:
            raise ValueError("TaskAnywhereForm needs the accounts this user may see.")
        self._accounts = accounts
        self.fields["account"].queryset = accounts.order_by("client_name")

        # Only the chosen account's stakeholders. Offering every contact in the
        # portfolio made the commonest choice a search through hundreds of names
        # that mostly work for other clients, and every one of them was a
        # validation error waiting to happen - ``clean`` rejects a contact from
        # another account, so the field was full of options that could not be
        # submitted.
        chosen = self.data.get("account") or self.initial.get("account") or ""
        chosen = getattr(chosen, "pk", chosen)
        if str(chosen).isdigit():
            self.fields["contact"].queryset = Contact.objects.filter(
                account__in=accounts, account_id=int(chosen)
            ).active()
        else:
            self.fields["contact"].queryset = Contact.objects.none()
            self.fields["contact"].empty_label = "Choose an account first"

        # The account select asks for the matching contacts as soon as it
        # changes. Declared here rather than in the template so the field and
        # the endpoint that answers it cannot drift apart.
        self.fields["account"].widget.attrs.update(
            {
                "hx-get": reverse("tasks:contact_options"),
                "hx-target": "#contact-field",
                "hx-swap": "innerHTML",
                "hx-trigger": "change",
            }
        )
        # due_time belongs immediately under due_date. order_fields() moves the
        # names it is given to the front and leaves everything else after them,
        # so omitting a field here does not keep it in place - it exiles it to
        # the bottom of the form, which is where the time input had ended up.
        self.order_fields(
            [
                "account", "task_type", "title", "due_date", "due_time", "priority",
                "progress", "category", "contact", "description", "repeat",
                "repeat_frequency", "repeat_ends", "repeat_count", "repeat_until",
            ]
        )

    def clean(self):
        """Say *why* a contact does not belong, rather than "invalid choice".

        The field's choices are already narrowed to the chosen account, so a
        contact from elsewhere fails as an unrecognised option - which is true
        and useless. Anyone who hits it has a stale page or a hand-built post,
        and the answer they need is which client that person actually works for.
        """
        cleaned = super().clean()
        account = cleaned.get("account")
        raw = (self.data.get("contact") or "").strip()

        if account and raw.isdigit() and "contact" in self.errors:
            elsewhere = (
                Contact.objects.filter(pk=int(raw), account__in=self._accounts)
                .exclude(account=account)
                .select_related("account")
                .first()
            )
            if elsewhere is not None:
                del self.errors["contact"]
                self.add_error(
                    "contact",
                    f"{elsewhere.full_name} works for {elsewhere.account.client_name}, "
                    f"not {account.client_name}.",
                )
        return cleaned
