"""How one person wants their calendar drawn.

Preferences rather than settings: nothing here changes what anybody may see,
only how it is laid out. Row scope and capability are decided in
``core/access`` as always, and this model is not consulted by either.

One row per person, created on first use. The defaults are the Egyptian working
week - Sunday to Thursday - because that is the week this platform's users
actually work, not the one the calendar library happened to assume.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class Weekday(models.IntegerChoices):
    """Python's numbering, so ``date.weekday()`` compares directly."""

    MONDAY = 0, "Monday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class CalendarView(models.TextChoices):
    MONTH = "month", "Month"
    WEEK = "week", "Week"
    DAY = "day", "Day"


#: Which days are the weekend, given where the week starts. Derived rather than
#: asked for: a week that starts on Sunday is worked Sunday to Thursday, and
#: making it a third question would let somebody save a combination that means
#: nothing.
WEEKEND_FOR_START = {
    Weekday.SUNDAY: (4, 5),   # Friday, Saturday
    Weekday.MONDAY: (5, 6),   # Saturday, Sunday
    Weekday.SATURDAY: (4, 5),  # Friday, Saturday
}


class CalendarPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_preference"
    )

    first_weekday = models.IntegerField(
        choices=Weekday.choices,
        default=Weekday.SUNDAY,
        help_text="The day a week starts on. Sunday for the Egyptian working week.",
    )
    default_view = models.CharField(
        max_length=8, choices=CalendarView.choices, default=CalendarView.MONTH
    )
    show_weekends = models.BooleanField(
        default=True,
        help_text="Keep the non-working days in the grid, greyed. Off hides them entirely.",
    )
    show_projected = models.BooleanField(
        default=True,
        help_text=(
            "Draw the days a repeat will fall on. Off shows only what has "
            "actually been raised."
        ),
    )
    show_happened = models.BooleanField(
        default=True, help_text="Include the activity log beside the planned work."
    )

    class Meta:
        db_table = "calendar_preferences"

    def __str__(self) -> str:
        return f"calendar preferences for {self.user_id}"

    @property
    def weekend(self) -> tuple:
        return WEEKEND_FOR_START.get(self.first_weekday, (5, 6))

    @classmethod
    def for_user(cls, user):
        """Never None. A reader who has never opened the settings gets the
        defaults rather than a page that cannot render."""
        if not getattr(user, "is_authenticated", False):
            return cls()
        preference, _ = cls.objects.get_or_create(user=user)
        return preference
