"""dPhish staff accounts.

Users are internal staff with login credentials. They are a completely separate
table from Contacts, who are people at the client organisation and have no
access to the system at all. Keeping them apart is a requirement, not an
implementation detail.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django_countries.fields import CountryField

from csm.core.enums import Role


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("A user must have an email address.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("role", Role.CSM)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("role", Role.MANAGER)
        extra["is_staff"] = True
        extra["is_superuser"] = True
        extra["is_active"] = True
        return self._create(email, password, **extra)

    def active(self):
        return self.get_queryset().filter(is_active=True)


class DisplayPreference(models.Model):
    """Where one person is sitting, for the clock on their own screen.

    **This changes nothing about when work is due.** Every day count in the
    scoring engine, every due date and every generated task is evaluated in
    ``settings.APP_TIME_ZONE`` through ``core.clock``, and nothing here is
    consulted by any of it. A CSM in London who sets Europe/London sees their
    own clock; a task due today is still due on Cairo's today, because that is
    the day the whole platform counts in.

    Kept apart from the business timezone for exactly that reason: the moment a
    per-person timezone could reach the engine, two people would disagree about
    whether an account had gone a week without contact.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="display_preference"
    )
    timezone = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="For the clock on your dashboard. Blank follows the business timezone.",
    )
    country = CountryField(
        blank=True,
        default="",
        help_text="Shown beside the clock. It does not change any date or due time.",
    )
    twelve_hour_clock = models.BooleanField(
        default=False,
        help_text="Show the clock as 2:30 PM rather than 14:30.",
    )

    class Meta:
        db_table = "display_preferences"

    def __str__(self) -> str:
        return f"display preferences for {self.user_id}"

    @property
    def effective_timezone(self) -> str:
        """Theirs, or the one the platform counts in."""
        return self.timezone or settings.APP_TIME_ZONE

    @classmethod
    def for_user(cls, user):
        """Never None: a reader who has never opened the settings still has a
        clock, rather than a page that cannot render."""
        if not getattr(user, "is_authenticated", False):
            return cls()
        preference, _ = cls.objects.get_or_create(user=user)
        return preference


class User(AbstractBaseUser, PermissionsMixin):
    """A dPhish staff member.

    Five effective access levels come from two fields: ``role`` gives CSM, ACM,
    Manager and Admin, and ``is_senior`` promotes a CSM to CSM Senior. A database
    constraint keeps ``is_senior`` meaningful only on CSM, which is what makes
    the set of levels closed rather than merely conventional.

    Admin is a role rather than a flag on another role. Administering who exists
    is a job of its own, it is the only role that may create a user, and as a
    flag the question "who can add staff" was answered by a Django field instead
    of by this application's access levels.

    Interpretation of these two fields belongs exclusively to
    ``csm.core.access``. Nothing else in the codebase may read them.
    """

    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32, blank=True, default="")

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.CSM)
    is_senior = models.BooleanField(
        default=False,
        help_text="A senior CSM may view and act on every account. Only meaningful for CSM.",
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "users"
        ordering = ["full_name"]
        constraints = [
            # is_senior is only meaningful on a CSM. This is what closes the set
            # of effective levels rather than leaving it to convention.
            models.CheckConstraint(
                condition=models.Q(is_senior=False) | models.Q(role=Role.CSM),
                name="users_is_senior_only_for_csm",
            ),
        ]
        indexes = [
            models.Index(fields=["role"]),
            models.Index(fields=["is_active"]),
            models.Index(Lower("email"), name="users_email_lower_idx"),
        ]

    def __str__(self) -> str:
        return self.full_name or self.email

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        if self.role != Role.CSM:
            self.is_senior = False
        super().save(*args, **kwargs)

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.email

    @property
    def effective_level_label(self) -> str:
        """Display only. Never branch on this; use csm.core.access instead."""
        if self.role == Role.CSM and self.is_senior:
            return "CSM Senior"
        return Role(self.role).label
