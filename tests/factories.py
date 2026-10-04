"""Test factories."""

from __future__ import annotations

import datetime as dt

import factory
from factory.django import DjangoModelFactory

from csm.accounts.models import Account, Partner
from csm.activities.models import Activity
from csm.contacts.models import Contact
from csm.core.enums import (
    ActivityType,
    AdoptionEventType,
    LifecycleStage,
    MeetingFrequency,
    Role,
    TicketKind,
    TicketStatus,
)
from csm.engagement.models import AdoptionEvent, CsatResponse, Ticket
from csm.users.models import User


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("email",)

    email = factory.Sequence(lambda n: f"user{n}@dphish.com")
    full_name = factory.Sequence(lambda n: f"User {n}")
    role = Role.CSM
    is_senior = False
    is_active = True

    class Params:
        csm = factory.Trait(role=Role.CSM, is_senior=False)
        senior_csm = factory.Trait(role=Role.CSM, is_senior=True)
        acm = factory.Trait(role=Role.ACM, is_senior=False)
        manager = factory.Trait(role=Role.MANAGER, is_senior=False)
        admin = factory.Trait(
            role=Role.ADMIN, is_senior=False, is_staff=True, is_superuser=True
        )


class PartnerFactory(DjangoModelFactory):
    class Meta:
        model = Partner
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"Partner {n}")


class AccountFactory(DjangoModelFactory):
    class Meta:
        model = Account

    client_code = factory.Sequence(lambda n: f"ACC{n:04d}")
    client_name = factory.Sequence(lambda n: f"Client {n}")
    lifecycle_stage = LifecycleStage.ACTIVE
    is_managed_service = False
    csm = factory.SubFactory(UserFactory, csm=True)
    acm = factory.SubFactory(UserFactory, acm=True)
    # A year back, so day-count factors score on real data.
    scoring_baseline_date = factory.LazyFunction(
        lambda: dt.date.today() - dt.timedelta(days=365)
    )

    class Params:
        managed = factory.Trait(
            is_managed_service=True, required_meeting_frequency=MeetingFrequency.WEEKLY
        )
        managed_no_frequency = factory.Trait(
            is_managed_service=True, required_meeting_frequency=None
        )
        poc = factory.Trait(lifecycle_stage=LifecycleStage.POC)
        onboarding = factory.Trait(lifecycle_stage=LifecycleStage.ONBOARDING)
        churned = factory.Trait(lifecycle_stage=LifecycleStage.CHURNED)


class ContactFactory(DjangoModelFactory):
    class Meta:
        model = Contact

    account = factory.SubFactory(AccountFactory)
    full_name = factory.Sequence(lambda n: f"Contact {n}")


class ActivityFactory(DjangoModelFactory):
    class Meta:
        model = Activity

    account = factory.SubFactory(AccountFactory)
    activity_type = ActivityType.CALL
    occurred_on = factory.LazyFunction(dt.date.today)
    created_by = factory.SubFactory(UserFactory)


class TicketFactory(DjangoModelFactory):
    class Meta:
        model = Ticket

    account = factory.SubFactory(AccountFactory)
    ticket_kind = TicketKind.GENERAL
    status = TicketStatus.OPEN
    title = factory.Sequence(lambda n: f"Ticket {n}")
    opened_on = factory.LazyFunction(dt.date.today)
    created_by = factory.SubFactory(UserFactory)


class CsatFactory(DjangoModelFactory):
    class Meta:
        model = CsatResponse

    account = factory.SubFactory(AccountFactory)
    score = 5
    recorded_on = factory.LazyFunction(dt.date.today)
    created_by = factory.SubFactory(UserFactory)


class AdoptionEventFactory(DjangoModelFactory):
    class Meta:
        model = AdoptionEvent

    account = factory.SubFactory(AccountFactory)
    event_type = AdoptionEventType.PHISHING_CAMPAIGN
    occurred_on = factory.LazyFunction(dt.date.today)
    created_by = factory.SubFactory(UserFactory)
