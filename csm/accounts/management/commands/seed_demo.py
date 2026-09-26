"""Build a deterministic demo portfolio.

Roughly sixty accounts, one user at each of the four access levels, and enough
activity, tickets, ratings and adoption events that the scoring engine produces
a realistic spread of colours. Used for manual walkthroughs.
"""

from __future__ import annotations

import datetime as dt
import random

from django.core.management.base import BaseCommand
from django.db import transaction

from csm.accounts.models import Account, Partner
from csm.activities.models import Activity
from csm.contacts.models import Contact, ContactChannel
from csm.core import clock
from csm.core.enums import (
    AccountClass,
    AccountSize,
    ActivityType,
    AdoptionEventType,
    CommunicationChannel,
    Deployment,
    EngagementItemType,
    Level,
    LicenseType,
    LifecycleStage,
    MeetingFrequency,
    Role,
    TicketKind,
    TicketStatus,
)
from csm.engagement.models import AdoptionEvent, CsatResponse, EngagementItem, Ticket
from csm.health.services import recalculate_all
from csm.tasks.generators import generate_quarterly_tasks, generate_renewal_tasks
from csm.users.models import User

COUNTRIES = ["EG", "AE", "SA", "JO", "QA", "KW", "BH", "OM"]
CITIES = ["Cairo", "Dubai", "Riyadh", "Amman", "Doha", "Kuwait City", "Manama", "Muscat"]
NAME_PARTS_A = ["Nile", "Delta", "Atlas", "Cedar", "Falcon", "Oasis", "Pyramid", "Gulf",
                "Sahara", "Levant", "Arabia", "Horizon", "Summit", "Harbour", "Meridian"]
NAME_PARTS_B = ["Bank", "Holdings", "Logistics", "Energy", "Telecom", "Insurance",
                "Industries", "Group", "Systems", "Airways"]


class Command(BaseCommand):
    help = "Create a deterministic demo portfolio for manual testing."

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--accounts", type=int, default=60)
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing demo data before seeding.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(options["seed"])
        today = clock.today()

        if options["reset"]:
            Account.objects.all().delete()
            User.objects.filter(email__endswith="@demo.dphish.com").delete()
            self.stdout.write("Cleared existing demo data.")

        users = self._create_users()
        partners = [
            Partner.objects.get_or_create(name=name)[0]
            for name in ["Direct", "Regional Reseller", "Global SI"]
        ]

        csms = [users["csm_a"], users["csm_b"], users["senior"]]
        acms = [users["acm_a"], users["acm_b"]]
        accounts = []
        for index in range(options["accounts"]):
            accounts.append(
                self._create_account(rng, index, today, csms, acms, partners)
            )

        for account in accounts:
            self._populate(rng, account, today)

        self.stdout.write("Scoring the portfolio...")
        scored = recalculate_all(as_of=today)
        renewals = generate_renewal_tasks(as_of=today)
        quarterly = generate_quarterly_tasks(as_of=today)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(accounts)} accounts, scored {scored}, "
                f"generated {renewals} renewal and {quarterly} quarterly tasks."
            )
        )
        self.stdout.write("Sign in with any of these, password 'demo-password-123':")
        for user in users.values():
            self.stdout.write(f"  {user.email:34} {user.effective_level_label}")

    # -- Helpers ----------------------------------------------------------

    def _create_users(self) -> dict[str, User]:
        spec = {
            "csm_a": ("casey.morgan@demo.dphish.com", "Casey Morgan", Role.CSM, False),
            "csm_b": ("robin.vale@demo.dphish.com", "Robin Vale", Role.CSM, False),
            "senior": ("sam.rivers@demo.dphish.com", "Sam Rivers", Role.CSM, True),
            "acm_a": ("alex.chen@demo.dphish.com", "Alex Chen", Role.ACM, False),
            "acm_b": ("noor.haddad@demo.dphish.com", "Noor Haddad", Role.ACM, False),
            "manager": ("morgan.lee@demo.dphish.com", "Morgan Lee", Role.MANAGER, False),
        }
        users = {}
        for key, (email, name, role, senior) in spec.items():
            user, created = User.objects.get_or_create(
                email=email,
                defaults={"full_name": name, "role": role, "is_senior": senior},
            )
            if created:
                user.set_password("demo-password-123")
                user.save()
            users[key] = user
        return users

    def _create_account(self, rng, index, today, csms, acms, partners) -> Account:
        managed = rng.random() < 0.45
        stage = rng.choices(
            [
                LifecycleStage.ACTIVE,
                LifecycleStage.ADOPTION,
                LifecycleStage.RENEWAL,
                LifecycleStage.EXPANSION,
                LifecycleStage.ONBOARDING,
                LifecycleStage.POC,
                LifecycleStage.CHURNED,
                LifecycleStage.CLOSED_LOST,
            ],
            weights=[34, 20, 14, 8, 8, 8, 5, 3],
        )[0]

        start = today - dt.timedelta(days=rng.randint(200, 1400))
        end = start + dt.timedelta(days=365 * rng.randint(1, 3))
        country_index = rng.randrange(len(COUNTRIES))

        account = Account.objects.create(
            client_code=f"DEMO{index:03d}",
            client_name=f"{rng.choice(NAME_PARTS_A)} {rng.choice(NAME_PARTS_B)} {index}",
            country=COUNTRIES[country_index],
            # One in seven left blank, so the "Not set" bar on the license
            # card is exercised rather than only theorised about.
            license_type=rng.choice([c for c, _ in LicenseType.choices] + [""]),
            location=CITIES[country_index],
            deployment=rng.choice([Deployment.CLOUD, Deployment.CLOUD, Deployment.ON_PREM]),
            is_managed_service=managed,
            boq_users_count=rng.choice([50, 100, 250, 500, 1000, 2500]),
            added_users_count=rng.choice([0, 0, 10, 50, 120]),
            contract_start_date=start,
            contract_end_date=end,
            lifecycle_stage=stage,
            # A few managed accounts deliberately lack a frequency, so the
            # eighty-point cap is visible in the demo data.
            required_meeting_frequency=(
                rng.choice([MeetingFrequency.WEEKLY, MeetingFrequency.BIWEEKLY, None])
                if managed
                else None
            ),
            quarterly_meeting_date=today + dt.timedelta(days=rng.randint(-40, 80)),
            account_class=rng.choices(
                [AccountClass.VIP, AccountClass.A, AccountClass.B, AccountClass.C, AccountClass.D],
                weights=[6, 18, 30, 30, 16],
            )[0],
            size=rng.choice([AccountSize.ENTERPRISE, AccountSize.MID, AccountSize.SMB]),
            csm=rng.choice(csms),
            acm=rng.choice(acms),
            partner=rng.choice(partners),
            scoring_baseline_date=today - dt.timedelta(days=rng.randint(60, 400)),
            description="Seeded demo account.",
            events_count=rng.randint(0, 4),
            digital_content_count=rng.randint(0, 6),
        )

        self._seed_contacts(rng, account, index)
        return account

    def _seed_contacts(self, rng, account, index) -> None:
        """Two focal points plus a spread of others across the grid.

        The spread is the point. Contacts that are all High/High and Medium/Low
        land in the same two quadrants under any mapping, which would let the
        grid look right in a demo while the interesting cells — and the triage
        tray — went entirely unexercised.
        """
        # The two focal points always have an assessment.
        people = [
            ("Primary Contact", Level.HIGH, Level.HIGH, 1),
            ("Secondary Contact", Level.MEDIUM, Level.LOW, 2),
        ]

        # Everyone else is drawn from the full nine cells, with roughly one in
        # five left unassessed so the triage tray is never empty across the
        # portfolio.
        for n in range(rng.randint(1, 4)):
            if rng.random() < 0.2:
                influence, interest = None, None
            else:
                influence = rng.choice([Level.HIGH, Level.MEDIUM, Level.LOW])
                interest = rng.choice([Level.HIGH, Level.MEDIUM, Level.LOW])
            people.append((f"Contact {n + 3}", influence, interest, None))

        for rank, (name, influence, interest, focal_rank) in enumerate(people, start=1):
            contact = Contact.objects.create(
                account=account,
                full_name=f"{name} {index}",
                email=f"contact{rank}.{index}@example.com",
                title=rng.choice(["CISO", "IT Manager", "Head of Security", "CTO"]),
                influence=influence,
                interest=interest,
                is_focal_point=focal_rank is not None,
                focal_rank=focal_rank,
                preferred_channel=CommunicationChannel.EMAIL,
            )
            for channel in (CommunicationChannel.EMAIL, CommunicationChannel.PHONE):
                ContactChannel.objects.create(contact=contact, channel=channel)

    def _populate(self, rng, account, today) -> None:
        """Give the account enough history for a believable score."""
        if account.lifecycle_stage in (LifecycleStage.CLOSED_LOST, LifecycleStage.POC):
            return

        creator = account.csm or account.acm
        if creator is None:
            return

        contacts = list(account.contacts.all())

        # How well looked after this account is. A third are neglected on
        # purpose, so the demo dashboard is not uniformly green.
        neglect = rng.choices([0, 1, 2], weights=[55, 25, 20])[0]
        recency = [rng.randint(0, 6), rng.randint(16, 28), rng.randint(35, 70)][neglect]

        for offset in range(recency, recency + 90, rng.randint(6, 14)):
            Activity.objects.create(
                account=account,
                contact=rng.choice(contacts) if contacts and rng.random() < 0.7 else None,
                activity_type=rng.choices(
                    [ActivityType.CALL, ActivityType.EMAIL, ActivityType.MEETING],
                    weights=[35, 45, 20],
                )[0],
                occurred_on=today - dt.timedelta(days=offset),
                subject="Check-in",
                created_by=creator,
            )

        AdoptionEvent.objects.create(
            account=account,
            event_type=rng.choice(
                [AdoptionEventType.PHISHING_CAMPAIGN, AdoptionEventType.COURSE_ASSIGNED]
            ),
            occurred_on=today - dt.timedelta(days=recency + rng.randint(0, 8)),
            title="Quarterly campaign",
            created_by=creator,
        )

        # Dated engagement items, spread across the last twelve months. Dashboard
        # card 11 plots these; the scalar counts on Account carry no date and
        # cannot go on a time axis, which is the reason this table exists at all.
        # Without them the one card that justifies EngagementItem renders empty.
        for _ in range(rng.randint(0, 5)):
            days_ago = rng.randint(0, 360)
            EngagementItem.objects.create(
                account=account,
                item_type=rng.choices(
                    [EngagementItemType.EVENT, EngagementItemType.DIGITAL_CONTENT],
                    weights=[40, 60],
                )[0],
                occurred_on=today - dt.timedelta(days=days_ago),
                title=rng.choice(
                    ["Webinar", "Case study", "Roundtable", "Whitepaper", "User group"]
                ),
                created_by=creator,
            )

        if rng.random() < 0.75:
            CsatResponse.objects.create(
                account=account,
                score=rng.choices([5, 4, 3, 2, 1], weights=[30, 30, 20, 12, 8])[0],
                recorded_on=today - dt.timedelta(days=rng.randint(5, 120)),
                created_by=creator,
            )

        for _ in range(rng.choices([0, 1, 2, 3], weights=[45, 30, 15, 10])[0]):
            Ticket.objects.create(
                account=account,
                ticket_kind=rng.choices([TicketKind.GENERAL, TicketKind.BUG], weights=[70, 30])[0],
                status=TicketStatus.OPEN,
                title="Reported issue",
                opened_on=today - dt.timedelta(days=rng.randint(1, 45)),
                created_by=creator,
            )
