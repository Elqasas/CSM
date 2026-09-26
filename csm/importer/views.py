"""The import wizard - upload, preview, apply - and the exports beside it.

Every import view requires ``import.run``, held by any CSM and by an ACM. It
writes across the whole portfolio regardless of who owns which account, which
is why the capability - not row scope - is what gates it. An ACM holds no
``account.edit`` capability anywhere else, so a bare ``import.run`` would let
them write the one thing they otherwise cannot touch; ``Ability.importable_kinds()``
narrows what they may upload, preview, apply or download a blank template for
to contacts, and every view below checks it rather than trusting the coarse
capability alone.

The preview step is not a courtesy. It is what makes the import reversible in
the only sense that matters: you find out before the write, not after.

The export views below share this module because they share its column
tables, but answer to a separate ``export.run`` capability (held by every role)
and, unlike import, always start from ``ability.accounts()`` - an export is
read-only, so row scope applies exactly the way it does everywhere else, and
the health columns are appended only for a reader who holds ``health.view``.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import DetailView, FormView, ListView, TemplateView, View

from csm.contacts.models import Contact
from csm.core.access import capabilities as cap
from csm.core.clock import today
from csm.core.views import CapabilityRequiredMixin
from csm.importer import contact_services, export, services
from csm.importer.csv_template import build_accounts_csv, build_contacts_csv
from csm.importer.forms import UploadForm
from csm.importer.models import ImportBatch, ImportKind, ImportStatus
from csm.importer.parsing import active_custom_fields
from csm.importer.template import build_template


class ImportListView(CapabilityRequiredMixin, ListView):
    required_capability = cap.IMPORT_RUN
    template_name = "importer/batch_list.html"
    context_object_name = "batches"
    paginate_by = 20

    def get_queryset(self):
        return ImportBatch.objects.filter(
            kind__in=self.ability.importable_kinds()
        ).select_related("uploaded_by", "applied_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "import"
        context["form"] = UploadForm(ability=self.ability)
        return context


class ImportUploadView(CapabilityRequiredMixin, FormView):
    required_capability = cap.IMPORT_RUN
    template_name = "importer/batch_list.html"
    form_class = UploadForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["ability"] = self.ability
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "import"
        context["batches"] = ImportBatch.objects.filter(
            kind__in=self.ability.importable_kinds()
        ).select_related("uploaded_by")[:20]
        return context

    def form_valid(self, form):
        upload = form.cleaned_data["upload"]
        kind = form.cleaned_data["kind"]
        batch = ImportBatch.objects.create(
            upload=upload,
            original_name=upload.name[:255],
            kind=kind,
            uploaded_by=self.request.user,
            # Pinned now, not at apply time, so a sheet previewed today and
            # applied tomorrow does not quietly shift every day count by a day.
            baseline_date=today(),
        )
        validator_for(kind).validate(batch)

        if batch.status == ImportStatus.FAILED:
            messages.error(self.request, batch.parse_error)
        elif batch.can_apply:
            # The upload reads the file; it does not write anything. Said here
            # because an upload that ends on a page of correct-looking rows
            # reads as a finished import, and the sheet then appears to have
            # done nothing at all.
            messages.info(
                self.request,
                f"Read {batch.rows_total} row{'' if batch.rows_total == 1 else 's'}. "
                "Nothing has been imported yet - check the preview below, then "
                "press Import to write it.",
            )
        return redirect("importer:preview", pk=batch.pk)


class ImportPreviewView(CapabilityRequiredMixin, DetailView):
    required_capability = cap.IMPORT_RUN
    template_name = "importer/batch_preview.html"
    context_object_name = "batch"

    def get_queryset(self):
        return ImportBatch.objects.filter(
            kind__in=self.ability.importable_kinds()
        ).select_related("uploaded_by", "applied_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        batch = self.object
        context["nav"] = "import"
        context["rows"] = batch.rows.all()
        context["error_rows"] = [r for r in context["rows"] if r.errors]
        return context


class ImportApplyView(CapabilityRequiredMixin, View):
    required_capability = cap.IMPORT_RUN

    def post(self, request, pk):
        batch = get_object_or_404(
            ImportBatch.objects.filter(kind__in=self.ability.importable_kinds()), pk=pk
        )
        if not batch.can_apply:
            messages.error(request, "This batch cannot be applied.")
            return redirect("importer:preview", pk=batch.pk)

        result = validator_for(batch.kind).apply(batch, actor=request.user)

        if batch.kind == ImportKind.CONTACTS:
            messages.success(
                request,
                f"Imported {result['created']} new and {result['updated']} "
                "updated contact(s).",
            )
        else:
            messages.success(
                request,
                f"Imported {result['created']} new and {result['updated']} updated "
                f"account(s), and scored {result['scored']}.",
            )
        return redirect("importer:preview", pk=batch.pk)


#: Which module handles which kind of batch. One wizard, two shapes.
HANDLERS = {
    ImportKind.ACCOUNTS: services,
    ImportKind.CONTACTS: contact_services,
}


def validator_for(kind):
    return HANDLERS.get(kind, services)


class ImportTemplateView(CapabilityRequiredMixin, View):
    """Hand back a blank template with exactly the headers the parser expects.

    Four of them: accounts and contacts, each as .xlsx or .csv. All four are
    generated from the same column tables the parsers match against, so none can
    drift into emitting a header the importer no longer reads.
    """

    required_capability = cap.IMPORT_RUN

    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    CSV = "text/csv; charset=utf-8"

    def get(self, request, kind="accounts", fmt="xlsx"):
        if kind.upper() not in self.ability.importable_kinds():
            raise PermissionDenied(f"This user may not import {kind!r}.")

        if fmt == "csv":
            if kind == "contacts":
                content = build_contacts_csv()
            else:
                content = build_accounts_csv(active_custom_fields())
            content_type = self.CSV
        else:
            if kind == "contacts":
                from csm.importer.contact_parsing import (
                    CONTACT_COLUMNS,
                    CONTACT_REQUIRED_COLUMNS,
                )

                content = build_template(CONTACT_COLUMNS, CONTACT_REQUIRED_COLUMNS)
            else:
                content = build_template(custom_fields=active_custom_fields())
            content_type = self.XLSX

        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = (
            f'attachment; filename="{kind}-template.{fmt}"'
        )
        return response


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class ExportPageView(CapabilityRequiredMixin, TemplateView):
    required_capability = cap.EXPORT_RUN
    template_name = "importer/export.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "export"
        return context


class ExportDownloadView(CapabilityRequiredMixin, View):
    """The current data, not a template - one row per account or contact this
    reader may see."""

    required_capability = cap.EXPORT_RUN
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def get(self, request, kind="accounts"):
        if kind == "contacts":
            queryset = Contact.objects.filter(account__in=self.ability.accounts())
            content = export.build_contacts_xlsx(queryset)
        else:
            content = export.build_accounts_xlsx(
                self.ability.accounts(),
                include_health=self.ability.can(cap.HEALTH_VIEW),
                custom_fields=active_custom_fields(),
            )

        response = HttpResponse(content, content_type=self.XLSX)
        response["Content-Disposition"] = f'attachment; filename="{kind}-export.xlsx"'
        return response

    def get_success_url(self):
        return reverse("importer:list")
