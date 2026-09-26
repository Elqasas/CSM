"""In-app notifications and the outbox that carries them to email.

Rows are written inside the transaction that created the task, then drained
afterwards, so a mail server outage can never roll back a task and can never
lose a notification.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from csm.accounts.models import Account
from csm.core.enums import NotificationChannel, NotificationKind
from csm.tasks.models import Task


class NotificationQuerySet(models.QuerySet):
    def unread(self) -> NotificationQuerySet:
        return self.filter(read_at__isnull=True)

    def pending_email(self) -> NotificationQuerySet:
        return self.filter(
            channel=NotificationChannel.EMAIL, sent_at__isnull=True, failed_at__isnull=True
        )


class Notification(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    task = models.ForeignKey(
        Task, null=True, blank=True, on_delete=models.CASCADE, related_name="notifications"
    )
    account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.CASCADE, related_name="notifications"
    )

    kind = models.CharField(max_length=24, choices=NotificationKind.choices)
    channel = models.CharField(
        max_length=8, choices=NotificationChannel.choices, default=NotificationChannel.IN_APP
    )

    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="")
    url = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    dedupe_key = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        unique=True,
        help_text=(
            "Stops the daily job re-notifying the same person about the same task "
            "every morning. Contains the task, the channel and the occasion."
        ),
    )

    objects = NotificationQuerySet.as_manager()

    class Meta:
        db_table = "notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "read_at"]),
            models.Index(fields=["channel", "sent_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} to {self.user_id}"
