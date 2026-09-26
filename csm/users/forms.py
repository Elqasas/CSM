"""Forms for administering staff accounts.

The access levels come from two fields, so the form offers a role and a
seniority flag rather than a list of five - and refuses the combinations the
database would refuse anyway, with an explanation instead of an IntegrityError.

Administrator is one of the roles. It used to be a separate flag on top of
another role, which made "who may create a user" a question about a Django
field rather than about this application's access levels. Only an Admin holds
``user.manage``, and the form's job is to stop the last one being demoted.
"""

from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm

from csm.core.enums import Role
from csm.users.models import DisplayPreference, User


def _style(form) -> None:
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.Select):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


class DisplayPreferenceForm(forms.ModelForm):
    """Where you are sitting, for your own clock.

    The timezone list is every IANA zone rather than a curated dozen: a curated
    list is always missing somebody's, and the one thing worse than six hundred
    options is not being able to pick the right one at all.
    """

    class Meta:
        model = DisplayPreference
        fields = ["timezone", "country", "twelve_hour_clock"]
        labels = {
            "timezone": "Your timezone",
            "country": "Your country",
            "twelve_hour_clock": "12-hour clock",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from zoneinfo import available_timezones

        business = settings.APP_TIME_ZONE
        zones = sorted(available_timezones())
        self.fields["timezone"] = forms.ChoiceField(
            required=False,
            label="Your timezone",
            help_text=(
                f"For the clock on your dashboard only. Everything the platform "
                f"counts in days - due dates, health factors, the morning "
                f"digest - stays on {business}."
            ),
            choices=[("", f"Follow the business timezone ({business})")]
            + [(zone, zone.replace("_", " ")) for zone in zones],
        )
        _style(self)


class StaffUserForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["email", "full_name", "phone", "role", "is_senior", "is_active"]
        labels = {"is_senior": "Senior CSM", "is_active": "Active"}
        help_texts = {
            "is_senior": "A senior CSM may view and act on every account.",
            "is_active": "An inactive account cannot sign in and sees nothing.",
            "role": (
                "Admin holds every privilege and is the only role that may add "
                "or remove a user. A CSM sees their own accounts; a senior CSM "
                "sees every account; an ACM sees their own and no health; a "
                "manager reads everything and changes nothing."
            ),
        }

    def __init__(self, *args, editing_self=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.editing_self = editing_self
        _style(self)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        clash = User.objects.filter(email__iexact=email)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Another user already has this address.")
        return email

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        senior = cleaned.get("is_senior")

        if senior and role != Role.CSM:
            self.add_error(
                "is_senior",
                "Seniority applies to a CSM only. It is not something an ACM or "
                "a Manager has, which is what keeps the access levels at four.",
            )

        # Locking yourself out is easy to do and tedious to undo, so the two
        # ways of doing it are refused here rather than discovered afterwards.
        if self.editing_self:
            if not cleaned.get("is_active", True):
                self.add_error("is_active", "You cannot deactivate your own account.")
            if role != Role.ADMIN:
                self.add_error(
                    "role",
                    "You cannot demote yourself out of Admin. Ask another "
                    "administrator to do it.",
                )
        elif role != Role.ADMIN and self.instance.pk:
            self._refuse_removing_the_last_admin()

        return cleaned

    def _refuse_removing_the_last_admin(self) -> None:
        """Nobody left to administer is a state the database cannot recover from.

        Not a theoretical worry: with Admin as the only source of user.manage,
        demoting the last one leaves the staff page unreachable by anybody, and
        the only way back is a shell on the server.
        """
        others = User.objects.filter(role=Role.ADMIN, is_active=True).exclude(
            pk=self.instance.pk
        )
        if self.instance.role == Role.ADMIN and not others.exists():
            self.add_error(
                "role",
                "This is the only administrator left. Promote somebody else first, "
                "or nobody will be able to manage staff accounts.",
            )

    def save(self, commit=True):
        user = super().save(commit=False)
        # Django's own admin site is a different surface from this one, and its
        # flags follow the role rather than being set by hand: an administrator
        # who can reach one and not the other is a puzzle nobody enjoys.
        is_admin = user.role == Role.ADMIN
        user.is_superuser = is_admin
        user.is_staff = is_admin
        if commit:
            user.save()
        return user


class StaffUserCreateForm(StaffUserForm):
    """Creating a user, with the password handled separately.

    No password field: the administrator does not choose somebody else's
    password. One is generated, shown once, and handed over out of band.
    """

    class Meta(StaffUserForm.Meta):
        pass


class AdminSetPasswordForm(SetPasswordForm):
    """Set another user's password, without needing to know the old one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class SelfPasswordChangeForm(PasswordChangeForm):
    """Change your own password, which requires the current one.

    Requiring it is what stops an unattended browser being enough to take over
    the account.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class ForgotPasswordForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autocomplete": "username", "autofocus": True}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class ResetCodeForm(forms.Form):
    code = forms.CharField(
        label="Code",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]{6}",
                "autofocus": True,
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class ResetPasswordForm(SetPasswordForm):
    """Django's own, so the password validators apply exactly as elsewhere."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)
