"""Staging tables for the Excel import.

The import is a three-step wizard - upload, preview, apply - and the middle step
is the reason these tables exist. Parsing straight into accounts would mean
either applying a sheet nobody has looked at, or parsing it twice and hoping the
second pass agrees with the first. Rows are staged instead, so the preview shows
exactly what will be written, and applying writes what was previewed.

A batch is kept after it is applied. It is the record of where sixty accounts
came from on the day someone asks.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from csm.core.models import TimeStampedModel


class ImportStatus(models.TextChoices):
    UPLOADED = "UPLOADED", "Uploaded"
    VALIDATED = "VALIDATED", "Validated"
    APPLIED = "APPLIED", "Applied"
    FAILED = "FAILED", "Failed"


class ImportKind(models.TextChoices):
    """What a batch carries.

    One wizard, two shapes. Kept on the batch rather than sniffed from the
    headers because a contacts file and an accounts file can share a client-code
    column, and guessing wrong would write the wrong table.
    """

    ACCOUNTS = "ACCOUNTS", "Accounts"
    CONTACTS = "CONTACTS", "Contacts"


class RowAction(models.TextChoices):
    CREATE = "CREATE", "Create"
    UPDATE = "UPDATE", "Update"
    SKIP = "SKIP", "Skip"


class ImportBatch(TimeStampedModel):
    upload = models.FileField(upload_to="imports/%Y/%m/")
    original_name = models.CharField(max_length=255, blank=True, default="")

    kind = models.CharField(
        max_length=12, choices=ImportKind.choices, default=ImportKind.ACCOUNTS
    )

    status = models.CharField(
        max_length=12, choices=ImportStatus.choices, default=ImportStatus.UPLOADED
    )

    #: Every account this batch touches gets this as its scoring baseline. Held
    #: on the batch rather than recomputed at apply time so a preview built on
    #: Monday and applied on Tuesday cannot silently shift every day count.
    baseline_date = models.DateField()

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="import_batches"
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    parse_error = models.TextField(
        blank=True,
        default="",
        help_text="Set when the workbook itself could not be read at all.",
    )

    #: Headers the importer did not recognise. Reported rather than dropped
    #: silently: a skipped column is data somebody typed and expects to see.
    unknown_columns = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "import_batches"
        ordering = ["-created_at"]
        verbose_name_plural = "import batches"

    def __str__(self) -> str:
        return f"{self.original_name or self.upload.name} ({self.get_status_display()})"

    # -- Counts, for the preview header -----------------------------------

    @property
    def rows_total(self) -> int:
        return self.rows.count()

    @property
    def rows_with_errors(self) -> int:
        return self.rows.exclude(errors=[]).count()

    @property
    def rows_ready(self) -> int:
        return self.rows.filter(errors=[]).exclude(action=RowAction.SKIP).count()

    @property
    def rows_to_create(self) -> int:
        return self.rows.filter(errors=[], action=RowAction.CREATE).count()

    @property
    def rows_to_update(self) -> int:
        return self.rows.filter(errors=[], action=RowAction.UPDATE).count()

    @property
    def can_apply(self) -> bool:
        """A batch applies only if it validated and has something to write.

        Rows carrying errors are skipped rather than blocking the batch: a
        sixty-row sheet with two bad rows should import fifty-eight, and the two
        should be visible rather than silently dropped.
        """
        return self.status == ImportStatus.VALIDATED and self.rows_ready > 0


class ImportRow(models.Model):
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")

    #: The spreadsheet's own row number, so an error message names the row the
    #: person is looking at rather than an offset into our staging table.
    row_number = models.PositiveIntegerField()

    client_code = models.CharField(max_length=64, blank=True, default="")
    client_name = models.CharField(max_length=255, blank=True, default="")

    action = models.CharField(
        max_length=8, choices=RowAction.choices, default=RowAction.CREATE
    )

    #: Cleaned values, ready to write onto an Account. Names match model fields.
    cleaned = models.JSONField(default=dict, blank=True)
    #: ``[{"column": ..., "message": ...}]``. Empty means the row is importable.
    errors = models.JSONField(default=list, blank=True)
    #: Non-blocking remarks: a defaulted stage, an ignored column.
    warnings = models.JSONField(default=list, blank=True)

    account = models.ForeignKey(
        "accounts.Account",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Filled in once the batch is applied.",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Filled in once a contacts batch is applied.",
    )

    class Meta:
        db_table = "import_rows"
        ordering = ["row_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"], name="import_row_unique_per_batch"
            )
        ]
        indexes = [models.Index(fields=["batch", "action"])]

    def __str__(self) -> str:
        return f"Row {self.row_number}: {self.client_code or '(no code)'}"

    @property
    def is_importable(self) -> bool:
        return not self.errors and self.action != RowAction.SKIP
