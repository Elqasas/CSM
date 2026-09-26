"""Shared service helpers."""

from __future__ import annotations

from typing import Any

from csm.core.enums import AuditAction
from csm.core.models import AuditLog


def record_audit(
    *,
    actor: Any | None,
    action: AuditAction | str,
    obj: Any = None,
    object_type: str | None = None,
    object_id: int | None = None,
    changes: dict[str, Any] | None = None,
    note: str = "",
) -> AuditLog:
    """Write one audit row.

    Call this from service functions, never from views, so an operation is
    audited once regardless of how it was triggered.
    """
    if obj is not None:
        object_type = object_type or obj.__class__.__name__
        object_id = object_id if object_id is not None else getattr(obj, "pk", None)
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        action=str(action),
        object_type=object_type or "",
        object_id=object_id,
        changes=changes or {},
        note=note,
    )
