"""The event-driven half of the dual trigger.

These hooks live at the model layer rather than in view handlers on purpose. A
view-level hook is exactly the design that gets bypassed six months later by an
import script, a management command or a shell session, and the score is then
quietly wrong.

Recalculation is deferred to ``transaction.on_commit`` so it never runs against
a state that is about to be rolled back, and so a single request that logs three
activities scores the account once rather than three times.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from csm.accounts.models import Account
from csm.activities.models import Activity
from csm.core import clock
from csm.core.enums import SCORABLE_STAGES, ScoreTrigger
from csm.engagement.models import AdoptionEvent, CsatResponse, Ticket

logger = logging.getLogger(__name__)

#: Account fields that change which factors apply or how they read. The raw
#: inputs are untouched by these, but the score is just as wrong without a
#: recalculation.
TRACKED_ACCOUNT_FIELDS = (
    "lifecycle_stage",
    "is_managed_service",
    "required_meeting_frequency",
    "scoring_baseline_date",
)

_pending: set[int] = set()


def schedule_recalculation(account_id: int, trigger: str = ScoreTrigger.EVENT) -> None:
    """Queue one recalculation per account per transaction."""
    key = account_id
    if key in _pending:
        return
    _pending.add(key)

    def _run():
        _pending.discard(key)
        from csm.health.services import recalculate

        try:
            account = Account.objects.filter(pk=account_id).first()
            if account is not None:
                recalculate(account, trigger=trigger)
        except Exception:  # noqa: BLE001 - never break the request that caused it
            logger.exception("Event-driven recalculation failed for account %s", account_id)

    transaction.on_commit(_run)


# ---------------------------------------------------------------------------
# Inputs: an insert, an update and a delete all change the score equally
# ---------------------------------------------------------------------------


def _account_id_of(instance) -> int | None:
    return getattr(instance, "account_id", None)


@receiver(post_save, sender=Activity)
@receiver(post_save, sender=Ticket)
@receiver(post_save, sender=CsatResponse)
@receiver(post_save, sender=AdoptionEvent)
def _input_saved(sender, instance, **kwargs):
    account_id = _account_id_of(instance)
    if account_id:
        schedule_recalculation(account_id)


@receiver(post_delete, sender=Activity)
@receiver(post_delete, sender=Ticket)
@receiver(post_delete, sender=CsatResponse)
@receiver(post_delete, sender=AdoptionEvent)
def _input_deleted(sender, instance, **kwargs):
    account_id = _account_id_of(instance)
    if account_id:
        schedule_recalculation(account_id)


# ---------------------------------------------------------------------------
# Account fields that change the shape of the calculation
# ---------------------------------------------------------------------------


@receiver(pre_save, sender=Account)
def _remember_previous_account_values(sender, instance: Account, **kwargs):
    if not instance.pk:
        instance._previous_tracked = None
        return

    update_fields = kwargs.get("update_fields")
    if update_fields and not set(update_fields) & set(TRACKED_ACCOUNT_FIELDS):
        # The health service writes its cached columns with an explicit
        # update_fields that excludes every tracked field, so this is also what
        # stops a recalculation from triggering itself.
        instance._previous_tracked = None
        return

    previous = (
        Account.objects.filter(pk=instance.pk)
        .values(*TRACKED_ACCOUNT_FIELDS)
        .first()
    )
    instance._previous_tracked = previous


@receiver(post_save, sender=Account)
def _account_saved(sender, instance: Account, created: bool, **kwargs):
    if created:
        # Never leave an account without a score.
        schedule_recalculation(instance.pk, trigger=ScoreTrigger.EVENT)
        return

    previous = getattr(instance, "_previous_tracked", None)
    if not previous:
        return

    changed = {
        field
        for field in TRACKED_ACCOUNT_FIELDS
        if previous.get(field) != getattr(instance, field)
    }
    if not changed:
        return

    if "lifecycle_stage" in changed:
        _handle_stage_change(instance, previous["lifecycle_stage"])
        schedule_recalculation(instance.pk, trigger=ScoreTrigger.STAGE_CHANGE)
    else:
        schedule_recalculation(instance.pk, trigger=ScoreTrigger.EVENT)


def _handle_stage_change(account: Account, previous_stage: str) -> None:
    """Give an account entering a scored stage a clean start.

    The specification sets a baseline at import but says nothing about the
    Onboarding to Active transition, where the same problem appears: an account
    that has just begun its life in a scored stage has no activity history, and
    would read as months of neglect on its first calculation. Resetting the
    baseline on that transition is the same rule applied to the same situation.
    """
    entering_scored = (
        account.lifecycle_stage in SCORABLE_STAGES and previous_stage not in SCORABLE_STAGES
    )
    today = clock.today()
    fields = ["stage_changed_at"]
    account.stage_changed_at = clock.now()
    if entering_scored:
        account.scoring_baseline_date = today
        fields.append("scoring_baseline_date")

    Account.objects.filter(pk=account.pk).update(
        **{f: getattr(account, f) for f in fields}
    )
