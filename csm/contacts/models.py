"""Stakeholders at the client organisation.

Contacts are people at the client. They have no login and no access to the
system whatsoever; that separation from Users is a requirement.
"""

from __future__ import annotations

from django.db import models

from csm.accounts.models import Account
from csm.core.enums import CommunicationChannel, Level, PowerInterestQuadrant
from csm.core.models import TimeStampedModel

# ---------------------------------------------------------------------------
# The axis mapping
# ---------------------------------------------------------------------------
# Influence and interest each have three levels but the grid names four
# strategies, so Medium has to fall on one side of each axis. The two axes are
# deliberately asymmetric:
#
#   Influence — Medium counts as high. Under-reading influence is dangerous. A
#   decision-maker filed as Low is a blocker nobody cultivated, and that surfaces
#   at renewal.
#
#   Interest — Medium counts as low. Over-reading interest is merely wasteful:
#   someone gets updates they do not read. Keeping Medium out of the high half
#   also keeps "Manage closely" down to two of the nine cells, and a quadrant
#   that holds everyone advises nothing.
#
# Both the ``quadrant`` property and every queryset below read these and nothing
# else. That is not tidiness: the two definitions diverged once already, with
# ``quadrant`` treating Medium as high influence while the attention ordering
# demanded a strict High, and no test caught it.

HIGH_INFLUENCE_LEVELS = (Level.HIGH, Level.MEDIUM)
HIGH_INTEREST_LEVELS = (Level.HIGH,)

HIGH_INFLUENCE = models.Q(influence__in=HIGH_INFLUENCE_LEVELS)
HIGH_INTEREST = models.Q(interest__in=HIGH_INTEREST_LEVELS)

#: A contact nobody has placed on the grid yet. Distinct from Medium, which is a
#: judgement someone actually made.
UNASSESSED = models.Q(influence__isnull=True) | models.Q(interest__isnull=True)


class ContactQuerySet(models.QuerySet):
    def active(self) -> ContactQuerySet:
        return self.filter(is_active=True)

    def search(self, term: str) -> ContactQuerySet:
        """Match a contact by their own details or by who they work for.

        Includes the account name and code deliberately. "Who do we know at
        Acme" is the question people actually arrive with, and the row scope
        applied before this runs is what stops it reaching a colleague's client
        - the search widens what you look through, never what you may see.
        """
        term = (term or "").strip()
        if not term:
            return self.none()
        return self.filter(
            models.Q(full_name__icontains=term)
            | models.Q(title__icontains=term)
            | models.Q(email__icontains=term)
            | models.Q(account__client_name__icontains=term)
            | models.Q(account__client_code__icontains=term)
        )

    def assessed(self) -> ContactQuerySet:
        """Contacts with a position on the grid."""
        return self.exclude(UNASSESSED)

    def needs_triage(self) -> ContactQuerySet:
        """Active contacts nobody has assessed, for the triage tray."""
        return self.active().filter(UNASSESSED).order_by("full_name")

    def with_last_contacted(self) -> ContactQuerySet:
        """Annotate the most recent linked activity date.

        Derived rather than stored: the cardinality is small, and denormalising
        would mean maintaining the value when an activity is voided too.
        """
        return self.annotate(
            last_contacted_on=models.Max(
                "activities__occurred_on",
                filter=models.Q(activities__is_voided=False),
            )
        )

    def by_attention(self) -> ContactQuerySet:
        """Assessed contacts ordered by who most needs hearing from.

        This ranks rather than filters. The specification asks for a way of
        spotting neglected contacts "especially those with High Influence",
        which is emphasis in an ordering, not an exclusive filter: a
        low-influence contact who has gone quiet for a year is still worth
        seeing, just below the people who matter more.

        Unassessed contacts are excluded rather than sorted somewhere arbitrary.
        Staleness cannot be judged for someone nobody has placed on the grid,
        and they have their own surface in ``needs_triage()``.
        """
        return (
            self.active()
            .assessed()
            .with_last_contacted()
            .annotate(
                _is_high_influence=models.ExpressionWrapper(
                    HIGH_INFLUENCE, output_field=models.BooleanField()
                )
            )
            .order_by(
                models.F("_is_high_influence").desc(),
                models.F("last_contacted_on").asc(nulls_first=True),
                "full_name",
            )
        )


class Contact(TimeStampedModel):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="contacts")

    full_name = models.CharField(max_length=150)
    email = models.EmailField(max_length=254, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    title = models.CharField(max_length=150, blank=True, default="", verbose_name="Title / role")

    # No default. A contact arrives unassessed, and that is a different fact
    # from a deliberate Medium. Defaulting these to Medium put every imported
    # contact in the same quadrant, which is the one outcome that makes the grid
    # useless: it advises intensive management of everybody.
    influence = models.CharField(
        max_length=8, choices=Level.choices, null=True, blank=True,
        help_text="Left unset until someone assesses this contact.",
    )
    interest = models.CharField(
        max_length=8, choices=Level.choices, null=True, blank=True,
        help_text="Left unset until someone assesses this contact.",
    )

    preferred_channel = models.CharField(
        max_length=12, choices=CommunicationChannel.choices, blank=True, default=""
    )

    is_focal_point = models.BooleanField(
        default=False,
        verbose_name="Focal point",
        help_text="Up to two per account. Rank is assigned automatically.",
    )
    focal_rank = models.SmallIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")

    objects = ContactQuerySet.as_manager()

    class Meta:
        db_table = "contacts"
        ordering = ["full_name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(focal_rank__isnull=True) | models.Q(focal_rank__in=[1, 2]),
                name="contacts_focal_rank_allowed",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_focal_point=True, focal_rank__isnull=False)
                    | models.Q(is_focal_point=False, focal_rank__isnull=True)
                ),
                name="contacts_focal_rank_matches_flag",
            ),
            models.UniqueConstraint(
                fields=["account", "focal_rank"],
                condition=models.Q(focal_rank__isnull=False),
                name="contacts_one_contact_per_focal_rank",
            ),
        ]
        indexes = [models.Index(fields=["account", "is_active"])]

    def __str__(self) -> str:
        return self.full_name

    @property
    def is_assessed(self) -> bool:
        return self.influence is not None and self.interest is not None

    @property
    def quadrant(self) -> str | None:
        """Position on the Power/Interest grid, or None if nobody has assessed them.

        Returns one of four strategies for a nine-cell grid: the interface plots
        the contact in its true cell so Medium stays visible, and tints the cell
        by the strategy this returns. See the axis mapping at the top of this
        module for why Medium falls where it does.
        """
        if not self.is_assessed:
            return None
        high_influence = self.influence in HIGH_INFLUENCE_LEVELS
        high_interest = self.interest in HIGH_INTEREST_LEVELS
        if high_influence and high_interest:
            return PowerInterestQuadrant.MANAGE_CLOSELY
        if high_influence:
            return PowerInterestQuadrant.KEEP_SATISFIED
        if high_interest:
            return PowerInterestQuadrant.KEEP_INFORMED
        return PowerInterestQuadrant.MONITOR

    @property
    def quadrant_label(self) -> str:
        quadrant = self.quadrant
        if quadrant is None:
            return "Not assessed"
        return PowerInterestQuadrant(quadrant).label

    def set_focal_point(self, flag: bool) -> None:
        """Turn focal-point status on or off, assigning the next free rank.

        Mutates the instance only - the caller saves it. Which rank is free
        depends on every other contact at the same account, so this needs
        ``self.account`` already set; raises when turning it on would exceed
        the two-per-account limit the database also enforces.
        """
        from django.core.exceptions import ValidationError

        if not flag:
            self.is_focal_point = False
            self.focal_rank = None
            return

        taken = set(
            self.account.contacts.filter(is_focal_point=True)
            .exclude(pk=self.pk)
            .values_list("focal_rank", flat=True)
        )
        free = next((rank for rank in (1, 2) if rank not in taken), None)
        if free is None:
            raise ValidationError("This account already has two focal points.")
        self.is_focal_point = True
        self.focal_rank = free

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.preferred_channel and self.pk:
            chosen = set(self.channels.values_list("channel", flat=True))
            if chosen and self.preferred_channel not in chosen:
                raise ValidationError(
                    {"preferred_channel": "The preferred channel must be one of the selected channels."}
                )


class ContactChannel(models.Model):
    """The multi-select of communication channels.

    A junction table rather than an array column, so the enum is enforced by the
    database and "who is reachable on Teams" stays a plain join for the day the
    Teams integration lands.
    """

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="channels")
    channel = models.CharField(max_length=12, choices=CommunicationChannel.choices)

    class Meta:
        db_table = "contact_channels"
        constraints = [
            models.UniqueConstraint(fields=["contact", "channel"], name="contact_channel_unique")
        ]
        indexes = [models.Index(fields=["channel"])]

    def __str__(self) -> str:
        return f"{self.contact_id}:{self.channel}"
