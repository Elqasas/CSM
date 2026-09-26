"""Health orchestration: persistence, override handling and events.

Everything that writes a health value goes through this module. The cached
columns on Account have exactly one writer, which is what keeps them honest.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from csm.accounts.models import Account
from csm.core import clock
from csm.core.enums import (
    AuditAction,
    HealthStatus,
    OverrideClearedReason,
    ScoreTrigger,
)
from csm.core.events import HealthRecalculated, publish
from csm.core.services import record_audit
from csm.health.engine import ScoreResult, score
from csm.health.inputs import gather_inputs
from csm.health.models import HealthScoreHistory, HealthStatusOverride

logger = logging.getLogger(__name__)


class OverrideNotAllowed(Exception):
    """Raised when an override is attempted on an account with no score."""


def active_override(account: Account) -> HealthStatusOverride | None:
    return (
        HealthStatusOverride.objects.filter(account=account, cleared_at__isnull=True)
        .select_related("created_by")
        .first()
    )


@transaction.atomic
def recalculate(
    account: Account,
    *,
    trigger: str = ScoreTrigger.EVENT,
    as_of: date | None = None,
) -> ScoreResult | None:
    """Recalculate one account and persist the result.

    The order below is deliberate and should not be rearranged. In particular the
    override comparison happens after the score is computed and before the
    effective status is decided, so a cleared override never leaks into the same
    history row that recorded the clearing.
    """
    as_of = as_of or clock.today()

    # 1. Lock the row so a scheduled run and an event-driven run cannot
    #    interleave and write different cached values.
    account = Account.objects.select_for_update().get(pk=account.pk)
    previous_effective = account.effective_health_status

    # 2. Not a scored stage: clear any override, record N/A, stop.
    if not account.is_scorable:
        _clear_override_if_any(
            account, reason=OverrideClearedReason.STAGE_NOT_SCORABLE, cleared_score=None
        )
        _write_cached(
            account,
            result=None,
            effective=HealthStatus.NOT_APPLICABLE.value,
            as_of=as_of,
        )
        _write_history(
            account,
            result=None,
            effective=HealthStatus.NOT_APPLICABLE.value,
            override_active=False,
            trigger=trigger,
            as_of=as_of,
        )
        _emit(account, previous_effective, HealthStatus.NOT_APPLICABLE.value,
              HealthStatus.NOT_APPLICABLE.value, None, None, trigger, as_of)
        return None

    # 3-4. Read the inputs, then score them. Step 4 is pure.
    inputs = gather_inputs(account, as_of)
    result = score(inputs, as_of)

    # 5-6. Load the active override and decide whether this score invalidates it.
    override = active_override(account)
    if override is not None and result.final_score != override.score_at_override:
        _clear(
            override,
            reason=OverrideClearedReason.SCORE_CHANGED,
            cleared_score=result.final_score,
        )
        record_audit(
            actor=None,
            action=AuditAction.OVERRIDE_CLEAR,
            obj=account,
            changes={
                "anchor": str(override.score_at_override),
                "new_score": str(result.final_score),
            },
            note="Override cleared automatically because the computed score changed.",
        )
        override = None

    # 7. The effective status is the override if one survived, else the computed one.
    effective = override.status if override else result.status

    # 8-9. Persist.
    _write_cached(account, result=result, effective=effective, as_of=as_of)
    _write_history(
        account,
        result=result,
        effective=effective,
        override_active=override is not None,
        trigger=trigger,
        as_of=as_of,
    )

    # 10. Tell the task engine.
    _emit(
        account,
        previous_effective,
        effective,
        result.status,
        result.final_score,
        result.max_score,
        trigger,
        as_of,
    )
    return result


def recalculate_all(*, as_of: date | None = None, trigger: str = ScoreTrigger.DAILY) -> int:
    """The daily pass.

    Only scorable accounts are visited, so a churned account generates no daily
    rows. Each account is recalculated in its own transaction so one bad row
    cannot abort the run.
    """
    as_of = as_of or clock.today()
    count = 0
    for account_id in Account.objects.scorable().values_list("id", flat=True):
        try:
            recalculate(Account.objects.get(pk=account_id), trigger=trigger, as_of=as_of)
            count += 1
        except Exception:  # noqa: BLE001 - one account must not stop the run
            logger.exception("Health recalculation failed for account %s", account_id)
    return count


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


@transaction.atomic
def apply_override(
    account: Account, *, status: str, reason: str, actor
) -> HealthStatusOverride:
    """Set a manual health status.

    A fresh recalculation runs first so the anchor is the score as it stands at
    this instant. Without that, an override created against a stale number would
    clear itself on the very next run.
    """
    if len((reason or "").strip()) < 10:
        raise ValidationError({"reason": "Give a reason of at least 10 characters."})
    if status == HealthStatus.NOT_APPLICABLE:
        raise ValidationError({"status": "N/A cannot be set manually."})

    result = recalculate(account, trigger=ScoreTrigger.MANUAL)
    if result is None:
        raise OverrideNotAllowed(
            "This account's lifecycle stage means no health score is calculated, "
            "so there is nothing to override."
        )

    existing = active_override(account)
    if existing is not None:
        _clear(existing, reason=OverrideClearedReason.SUPERSEDED, cleared_score=result.final_score,
               actor=actor)

    override = HealthStatusOverride.objects.create(
        account=account,
        status=status,
        reason=reason.strip(),
        score_at_override=result.final_score,
        max_at_override=result.max_score,
        created_by=actor,
    )

    account.refresh_from_db()
    _write_cached(account, result=result, effective=status, as_of=clock.today())
    _write_history(
        account,
        result=result,
        effective=status,
        override_active=True,
        trigger=ScoreTrigger.OVERRIDE_SET,
        as_of=clock.today(),
    )
    record_audit(
        actor=actor,
        action=AuditAction.OVERRIDE_SET,
        obj=account,
        changes={"status": status, "anchor": str(result.final_score)},
        note=reason.strip(),
    )
    _emit(
        account,
        result.status,
        status,
        result.status,
        result.final_score,
        result.max_score,
        ScoreTrigger.OVERRIDE_SET,
        clock.today(),
    )
    return override


@transaction.atomic
def clear_override(account: Account, *, actor) -> None:
    override = active_override(account)
    if override is None:
        return
    _clear(override, reason=OverrideClearedReason.MANUAL, cleared_score=None, actor=actor)
    record_audit(actor=actor, action=AuditAction.OVERRIDE_CLEAR, obj=account)
    recalculate(account, trigger=ScoreTrigger.MANUAL)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _clear(
    override: HealthStatusOverride,
    *,
    reason: str,
    cleared_score: Decimal | None,
    actor=None,
) -> None:
    override.cleared_at = clock.now()
    override.cleared_reason = reason
    override.cleared_score = cleared_score
    override.cleared_by = actor if getattr(actor, "pk", None) else None
    override.save(
        update_fields=["cleared_at", "cleared_reason", "cleared_score", "cleared_by"]
    )


def _clear_override_if_any(account: Account, *, reason: str, cleared_score) -> None:
    override = active_override(account)
    if override is not None:
        _clear(override, reason=reason, cleared_score=cleared_score)


def _write_cached(
    account: Account, *, result: ScoreResult | None, effective: str, as_of: date
) -> None:
    account.computed_health_score = result.final_score if result else None
    account.computed_health_max = result.max_score if result else None
    account.computed_health_status = (
        result.status if result else HealthStatus.NOT_APPLICABLE.value
    )
    account.effective_health_status = effective
    account.health_calculated_at = clock.now()

    # red_since keys the health alert episode: re-entering At Risk on a later
    # date is a new episode and earns a new alert, while an alert closed within
    # the same episode is not regenerated.
    if effective == HealthStatus.AT_RISK:
        if account.red_since is None:
            account.red_since = as_of
    else:
        account.red_since = None
        account.health_alert_suppressed_at = None

    account.save(
        update_fields=[
            "computed_health_score",
            "computed_health_max",
            "computed_health_status",
            "effective_health_status",
            "health_calculated_at",
            "red_since",
            "health_alert_suppressed_at",
            "updated_at",
        ]
    )


def _write_history(
    account: Account,
    *,
    result: ScoreResult | None,
    effective: str,
    override_active: bool,
    trigger: str,
    as_of: date,
) -> HealthScoreHistory:
    return HealthScoreHistory.objects.create(
        account=account,
        calculated_on=as_of,
        trigger=trigger,
        score=result.final_score if result else None,
        max_score=result.max_score if result else None,
        computed_status=result.status if result else HealthStatus.NOT_APPLICABLE.value,
        effective_status=effective,
        override_active=override_active,
        factor_set=result.factor_set if result else None,
        engine_version=result.engine_version if result else "",
        breakdown=result.breakdown() if result else {},
    )


def _emit(
    account: Account,
    previous_effective: str | None,
    new_effective: str,
    computed: str,
    score_value: Decimal | None,
    max_score: int | None,
    trigger: str,
    as_of: date,
) -> None:
    publish(
        HealthRecalculated(
            account_id=account.pk,
            previous_effective_status=previous_effective,
            new_effective_status=new_effective,
            computed_status=computed,
            score=score_value,
            max_score=max_score,
            trigger=str(trigger),
            as_of=as_of,
        )
    )
