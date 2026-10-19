"""The Reports page and its downloads.

One tab per report, switched in place, each with its own period and options and
its own button. The button is a GET: a report is a read, so the URL of a download
is also a link somebody can keep. Row scope is not decided here - every builder
starts from the reader's ``Ability`` - and neither is who may run what, which the
registry declares.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views import View
from django.views.generic import TemplateView

from csm.core import clock
from csm.core.access import capabilities as cap
from csm.core.views import AbilityMixin
from csm.reports import periods, registry
from csm.reports.builders import ReportContext
from csm.reports.workbook import Sheet, build_workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ReportsAccessMixin(AbilityMixin):
    """Open to anybody with at least one report, and to nobody else."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not registry.visible_reports(self.ability):
            raise PermissionDenied("This user may run no report.")
        return super().dispatch(request, *args, **kwargs)


def _page(ability, today, *, active="", submitted=None, error=""):
    """Every visible report, grouped into tabs, with the values each form opens on.

    ``submitted`` is the form that just failed, so its tab reopens holding what
    the reader chose and saying what was wrong, rather than starting over.
    """
    # Each preset carries its dates so the form can show the actual range a
    # choice means - "Last quarter" is easy to misread at the turn of a year.
    presets = []
    for choice, label in periods.CHOICES:
        if choice != periods.CUSTOM:
            span = periods.preset(choice, today)
            presets.append((choice, label, span.start, span.end))

    tabs: dict[str, dict] = {}
    for report in registry.visible_reports(ability):
        mine = submitted if submitted and submitted["key"] == report.key else None
        card = {"report": report, "error": error if mine else ""}
        if report.default_period:
            default = periods.preset(report.default_period, today)
            card.update(period=report.default_period, start=default.start, end=default.end)
            if mine:
                card.update(period=mine["period"], start=mine["start"], end=mine["end"])
        card["options"] = [
            {
                "option": option,
                "value": (mine or {}).get("options", {}).get(option.name) or option.default,
            }
            for option in report.options
        ]

        if report.tab_key not in tabs:
            title, icon = registry.TAB_TITLES.get(report.tab_key, (report.title, report.icon))
            tabs[report.tab_key] = {"key": report.tab_key, "title": title, "icon": icon, "cards": []}
        tabs[report.tab_key]["cards"].append(card)
        if mine:
            active = report.tab_key

    if active not in tabs:
        found = registry.get(active)
        active = found.tab_key if found and found.tab_key in tabs else next(iter(tabs))

    return {
        "tabs": list(tabs.values()),
        "active_tab": active,
        "presets": presets,
        "custom": periods.CUSTOM,
        "accounts_in_scope": ability.accounts().count(),
        "shows_health": ability.can(cap.HEALTH_VIEW),
        # Reached from Settings, so the gear stays lit rather than a nav link.
        "nav": "settings",
    }


class ReportsView(ReportsAccessMixin, TemplateView):
    template_name = "reports/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            _page(self.ability, clock.today(), active=self.request.GET.get("report", ""))
        )
        return context


class ReportDownloadView(ReportsAccessMixin, View):
    def get(self, request, key):
        report = registry.get(key)
        if report is None:
            raise Http404("No such report.")
        if not self.ability.can_all(report.capabilities):
            raise PermissionDenied(f"This user may not run {key!r}.")

        today = clock.today()
        period = None
        chosen = request.GET.get("period", report.default_period or "")
        start_text = request.GET.get("from", "")
        end_text = request.GET.get("to", "")
        raw_options = {option.name: request.GET.get(option.name, "") for option in report.options}
        try:
            options = {
                option.name: option.clean(raw_options[option.name]) for option in report.options
            }
            if report.default_period:
                period = periods.resolve(chosen, start_text, end_text, today=today)
        except ValueError as error:
            # PeriodError is a ValueError, as is an unknown option.
            context = {"ability": self.ability}
            context.update(
                _page(
                    self.ability,
                    today,
                    submitted={
                        "key": key,
                        "period": chosen,
                        "start": start_text,
                        "end": end_text,
                        "options": raw_options,
                    },
                    error=str(error),
                )
            )
            return render(request, "reports/index.html", context, status=400)

        ctx = ReportContext(
            ability=self.ability,
            period=period,
            today=today,
            generated_at=clock.now(),
            options=options,
        )
        built = report.build(ctx)
        if isinstance(built, bytes):
            content = built
        else:
            content = build_workbook(list(built) + [_about(report, ctx, request.user)])

        stamp = period.slug if period else today.isoformat()
        response = HttpResponse(content, content_type=XLSX)
        response["Content-Disposition"] = f'attachment; filename="{report.key}_{stamp}.xlsx"'
        return response


def _about(report, ctx: ReportContext, user) -> Sheet:
    """What the file is, for whoever opens it next week without the page."""
    period = ctx.period
    rows = [
        ["Report", report.title],
        ["Period", period.label],
        ["From", period.start],
        ["To", period.end],
    ]
    for option in report.options:
        rows.append([option.label, dict(option.choices)[ctx.options[option.name]]])
    rows += [
        ["Generated", ctx.generated_at],
        ["Generated by", user.full_name or user.email],
        ["Accounts in scope", ctx.ability.accounts().count()],
        ["Health information", "Included" if ctx.shows_health else "Not included"],
        ["Time zone", f"Dates and times are in {clock.business_tz().key}"],
    ]
    return Sheet("About", ["About this report", ""], rows)
