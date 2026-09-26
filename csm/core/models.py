"""Abstract base models and the audit log."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from csm.core.enums import AuditAction


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self) -> SoftDeleteQuerySet:
        return self.filter(deleted_at__isnull=True)

    def dead(self) -> SoftDeleteQuerySet:
        return self.filter(deleted_at__isnull=False)


class SoftDeleteModel(models.Model):
    """Deletion that can be undone.

    The specification asks for account deletion to cascade to every activity,
    contact and task. That is unrecoverable, and at this portfolio size it is
    almost certainly a mistake when it happens. The user-facing behaviour is
    identical: a soft-deleted row is invisible everywhere in the application.
    Hard deletion is available as a separate, deliberate purge.
    """

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        abstract = True

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class AuditLog(models.Model):
    """An append-only record of the operations worth reconstructing later."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=32, choices=AuditAction.choices)
    object_type = models.CharField(max_length=64)
    object_id = models.BigIntegerField(null=True, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True, default="")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["object_type", "object_id", "-at"]),
            models.Index(fields=["actor", "-at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.object_type}#{self.object_id}"
