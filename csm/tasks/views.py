"""Tasks: the queue, creation and completion.

Every queryset starts from ``ability.visible_tasks()``, which already excludes
health alerts for anyone without ``health.view``. That exclusion is not
cosmetic: an ACM seeing that an alert exists on their account has been told
something about its health, which is the one thing the access rules keep from
them.

Completion goes through ``tasks.services.complete_task`` rather than setting the
status here, because completing a task emits a domain event and closes the alert
episode, and a view that wrote the column directly would skip both.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import FormView, ListView, TemplateView, View

from csm.core.access import capabilities as cap
from csm.core.clock import local_time, today
from csm.core.enums import RepeatEnd, TaskProgress, TaskStatus, TaskType
from csm.core.views import AccountScopedMixin, CapabilityRequiredMixin, safe_next
from csm.tasks.forms import TaskAnywhereForm, TaskForm
from csm.tasks.services import complete_task, create_task, delete_task

#: Where "do not ask me again today" lives. The session rather than the
#: database: it is a preference about this browser's next few clicks, not a
#: fact about the person, and it should not follow them to another machine.
SKIP_DELETE_CONFIRM_KEY = "skip_delete_confirm_on"


def kind_label(task_type: str) -> str:
    """What to call the thing being created.

    "New task" on a page that is making a call reads as though the kind field
    were decoration. The stored value is untouched - it is in every history row
    and idempotency key ever written - only the word changes.
    """
    from csm.core.enums import TaskType

    try:
        return TaskType(task_type).label
    except ValueError:
        return TaskType.MANUAL_NEXT_ACTION.label


def _kind_from_query(request) -> dict:
    """Preselect the kind when a link asked for one - Meeting, Call, or a task.

    Checked against the kinds a person may create rather than against the enum:
    a link carrying ``AUTO_RENEWAL`` would otherwise put a hand-made renewal in
    front of somebody, and the generator would raise its own beside it.
    """
    from csm.core.enums import MANUAL_TASK_TYPES

    wanted = request.GET.get("task_type", "")
    if wanted in {str(kind) for kind in MANUAL_TASK_TYPES}:
        return {"task_type": wanted}
    return {}


class TaskListView(CapabilityRequiredMixin, ListView):
    template_name = "tasks/task_list.html"
    context_object_name = "tasks"
    paginate_by = 50

    def get_queryset(self):
        qs = self.ability.visible_tasks().select_related("account", "contact")

        # Everything the user may see, by default. "Mine" means assigned to
        # you, and a senior CSM creating a task on somebody else's account is
        # helping out rather than taking it over - so the task routes to that
        # account's CSM and would not appear under "mine" at all. Defaulting to
        # "mine" made a task you just created look like it had vanished.
        scope = self.request.GET.get("scope", "all")
        if scope == "mine":
            qs = qs.for_user(self.request.user)
        elif scope == "unassigned":
            qs = qs.unassigned()

        state = self.request.GET.get("state", "open")
        if state == "open":
            qs = qs.filter(status=TaskStatus.PENDING)
        elif state == "overdue":
            qs = qs.overdue(today(), local_time())
        elif state == "done":
            qs = qs.filter(status=TaskStatus.DONE)

        task_type = self.request.GET.get("type", "").strip()
        if task_type in TaskType.values:
            qs = qs.filter(task_type=task_type)

        return qs.order_by("due_date", "-id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        visible = self.ability.visible_tasks()
        context["nav"] = "tasks"
        context["today"] = today()
        context["scope"] = self.request.GET.get("scope", "all")
        context["state"] = self.request.GET.get("state", "open")
        context["type_filter"] = self.request.GET.get("type", "")
        context["task_types"] = [
            (value, label)
            for value, label in TaskType.choices
            if value != TaskType.AUTO_HEALTH_ALERT
            or self.ability.can(cap.HEALTH_VIEW)
        ]
        context["counts"] = {
            "mine": visible.for_user(self.request.user)
            .filter(status=TaskStatus.PENDING)
            .count(),
            "overdue": visible.overdue(today(), local_time()).count(),
            "unassigned": visible.unassigned().count(),
        }
        return context


class TaskCompleteView(CapabilityRequiredMixin, FormView):
    required_capability = cap.TASK_COMPLETE
    form_class = TaskForm

    def get_task(self):
        return get_object_or_404(self.ability.visible_tasks(), pk=self.kwargs["pk"])

    def post(self, request, *args, **kwargs):
        task = self.get_task()
        if task.status == TaskStatus.PENDING:
            complete_task(task, actor=request.user)
            messages.success(request, f"Completed: {task.title}")
        return redirect(safe_next(request) or reverse("tasks:list"))


def log_after_the_fact(task, form, *, actor):
    """Close a task created as Completed, logging it on the day it happened.

    The date on the form is when it took place - the form allows a past one
    only for a completed task - so the log entry carries it rather than today.
    A future date is clamped to today by the log itself.
    """
    complete_task(task, actor=actor, occurred_on=form.cleaned_data["due_date"])


class SeriesStartMixin:
    """Turning the repeat block on a form into a series and its first occurrence.

    Shared by the two create views because the block is the same on both; the
    only difference is where the account comes from, which is what
    ``series_account`` answers.
    """

    def _start_series(self, form):
        """The rule, and the first occurrence of it."""
        from csm.tasks import recurrence
        from csm.tasks.models import TaskSeries

        data = form.cleaned_data
        ends = data["repeat_ends"] or RepeatEnd.NEVER
        series = TaskSeries.objects.create(
            account=self.series_account(form),
            contact=data["contact"],
            task_type=data["task_type"],
            category=data["category"],
            priority=data["priority"],
            title=data["title"],
            description=data["description"],
            due_time=data.get("due_time"),
            frequency=data["repeat_frequency"],
            ends=ends,
            occurrences=data.get("repeat_count") if ends == RepeatEnd.AFTER else None,
            until=data.get("repeat_until") if ends == RepeatEnd.ON else None,
            created_by=self.request.user,
        )
        return recurrence.start(series, first_due=data["due_date"])

    def series_account(self, form):
        return self.account


class TaskCreateView(SeriesStartMixin, AccountScopedMixin, FormView):
    required_capability = cap.TASK_CREATE
    template_name = "tasks/task_form.html"
    form_class = TaskForm
    account_url_kwarg = "account_pk"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["account"] = self.account
        return kwargs

    def get_initial(self):
        """Preselect a contact when one was named in the query string.

        Not trusted: the form's own queryset is this account's active contacts,
        so a contact id from anywhere else simply does not match a choice and
        the field comes up empty rather than binding somebody else's
        stakeholder to this account's task.
        """
        initial = super().get_initial()
        contact = self.request.GET.get("contact", "")
        if contact.isdigit():
            initial["contact"] = contact
        initial.update(_kind_from_query(self.request))
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "accounts"
        context["next"] = self._safe_next()
        context["kind_label"] = kind_label(
            self.request.GET.get("task_type", "MANUAL_NEXT_ACTION")
        )
        return context

    def _safe_next(self) -> str:
        return safe_next(self.request)

    def form_valid(self, form):
        # A repeat *replaces* the single task: the series stamps out the first
        # occurrence itself. Creating both made two tasks from one form, which
        # is what the duplicate rows in the queue were.
        if form.cleaned_data.get("repeat"):
            task = self._start_series(form)
        else:
            task = create_task(
                account=self.account,
                task_type=form.cleaned_data["task_type"],
                title=form.cleaned_data["title"],
                description=form.cleaned_data["description"],
                due_date=form.cleaned_data["due_date"],
                due_time=form.cleaned_data.get("due_time"),
                category=form.cleaned_data["category"],
                priority=form.cleaned_data["priority"],
                progress=form.cleaned_data["progress"],
                contact=form.cleaned_data["contact"],
                created_by=self.request.user,
            )

        # None means an idempotency key or the one-open-alert index already
        # covers this obligation. A manual task has neither, so it should not
        # happen, but saying nothing would be worse than saying so.
        if task is None:
            messages.warning(self.request, "That task already exists.")
        elif form.cleaned_data["progress"] == TaskProgress.COMPLETED:
            # Recorded after the fact. Closing it through the service rather
            # than saving the label is what writes the log entry and moves the
            # score - setting the column by hand would leave a task that reads
            # Completed and never happened as far as the account is concerned.
            log_after_the_fact(task, form, actor=self.request.user)
            messages.success(self.request, f"Logged: {task.title}")
        else:
            messages.success(self.request, f"Created: {task.title}")
        return redirect(self._safe_next() or reverse("accounts:detail", args=[self.account.pk]))


class TaskContactOptionsView(CapabilityRequiredMixin, View):
    """The contact field, rebuilt for whichever account was just chosen.

    Answers the account select on the standalone task form. It builds the real
    form rather than assembling a ``<select>`` by hand, so the options, the
    empty label and the scoping are the form's own - there is no second
    definition here to fall out of step with the one that validates the post.
    """

    required_capability = cap.TASK_CREATE

    def get(self, request):
        form = TaskAnywhereForm(
            data={"account": request.GET.get("account", "")},
            accounts=self.ability.accounts(),
        )
        # Bound purely to read the account; the rest of the form has not been
        # filled in yet and its errors are none of this fragment's business.
        return render(
            request, "tasks/_contact_field.html", {"field": form["contact"]}
        )


class TaskUpdateView(CapabilityRequiredMixin, FormView):
    """Edit any task, generated or manual.

    A generated task is editable and the edit sticks. Its ``auto_key`` is built
    from the account and the date the obligation derives from - not from the
    title or the due date - and the generator creates through ``create_task``,
    which the unique index turns into a no-op when the key already exists. It
    never updates a task it finds, so a reworded or rescheduled reminder
    survives the next run untouched.

    What does replace it is the underlying date moving: a new contract end date
    produces a new key, a fresh task, and the stale one cancelled. That is the
    intended behaviour rather than an edit being lost, because the obligation
    itself has changed.
    """

    required_capability = cap.TASK_EDIT
    template_name = "tasks/task_form.html"
    form_class = TaskForm

    def get_task(self):
        return get_object_or_404(self.ability.visible_tasks(), pk=self.kwargs["pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        task = self.get_task()
        kwargs["account"] = task.account
        kwargs["editing"] = True
        kwargs["generated"] = task.is_system_generated
        kwargs.setdefault("initial", {}).update(
            {
                "title": task.title,
                "due_date": task.due_date,
                "due_time": task.due_time,
                "task_type": task.task_type,
                "category": task.category,
                "priority": task.priority,
                "progress": task.progress,
                "contact": task.contact_id,
                "description": task.description,
            }
        )
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task = self.get_task()
        context["nav"] = "tasks"
        context["account"] = task.account
        context["task"] = task
        context["is_edit"] = True
        context["kind_label"] = kind_label(task.task_type)
        # Carried through the form, so Edit from a list returns to that list
        # rather than dropping the reader on the account page.
        context["next"] = safe_next(self.request)
        return context

    def form_valid(self, form):
        task = self.get_task()
        if "task_type" in form.cleaned_data:
            task.task_type = form.cleaned_data["task_type"]
        task.title = form.cleaned_data["title"]
        task.due_date = form.cleaned_data["due_date"]
        task.due_time = form.cleaned_data.get("due_time")
        task.category = form.cleaned_data["category"]
        task.priority = form.cleaned_data["priority"]
        # Completing through the edit form goes through the service too, below,
        # so this only carries the three open states.
        wants_completing = (
            form.cleaned_data["progress"] == TaskProgress.COMPLETED and task.is_open
        )
        task.progress = (
            task.progress if wants_completing else form.cleaned_data["progress"]
        )
        task.contact = form.cleaned_data["contact"]
        task.description = form.cleaned_data["description"]
        task.save(
            update_fields=["task_type", "title", "due_date", "due_time", "category",
                           "priority", "progress", "contact", "description",
                           "updated_at"]
        )
        if wants_completing:
            complete_task(task, actor=self.request.user)
        messages.success(self.request, f"Saved: {task.title}")
        return redirect(
            safe_next(self.request) or reverse("accounts:detail", args=[task.account_id])
        )


class TaskDetailView(CapabilityRequiredMixin, TemplateView):
    """One task, read rather than edited.

    Clicking a row used to open the edit form, which is the wrong default: most
    of the time the question is "what is this and when is it due", not "change
    its wording". Edit and delete are buttons here.
    """

    required_capability = cap.TASK_VIEW
    template_name = "tasks/task_detail.html"

    def get_task(self):
        return get_object_or_404(
            self.ability.visible_tasks().select_related(
                "account", "contact", "series", "created_by", "completed_by"
            ),
            pk=self.kwargs["pk"],
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task = self.get_task()
        context["nav"] = "activity"
        context["task"] = task
        context["account"] = task.account
        context["today"] = today()
        context["assignees"] = [a.user for a in task.assignments.select_related("user")]
        # The log entry this task wrote when it was completed, if it wrote one:
        # the record of the thing, as opposed to the obligation to do it.
        context["logged"] = getattr(task, "logged_activity", None)
        context["next"] = safe_next(self.request)
        return context


class TaskDeleteView(CapabilityRequiredMixin, View):
    """Delete a manual task, cancel a generated one.

    The service decides which, because a hard delete frees the idempotency key
    and the task reappears the next morning looking like a bug.
    """

    required_capability = cap.TASK_DELETE
    template_name = "tasks/task_confirm_delete.html"
    fragment_template = "tasks/_delete_confirm.html"

    def get(self, request, pk):
        """The confirmation. One view serves the dialog and the page, so the
        two cannot come to say different things about what deleting does."""
        from django.shortcuts import render

        task = get_object_or_404(self.ability.visible_tasks(), pk=pk)
        return render(
            request,
            self.fragment_template if request.headers.get("HX-Request") else self.template_name,
            {
                "task": task,
                "account": task.account,
                "ability": self.ability,
                "nav": "activity",
                "next": safe_next(request),
            },
        )

    def post(self, request, pk):
        task = get_object_or_404(self.ability.visible_tasks(), pk=pk)
        account_id = task.account_id
        was_generated = task.is_system_generated

        if request.POST.get("suppress") == "1":
            # Asked not to be asked again today. Stored as the business date
            # rather than as a flag, so it expires on its own overnight and the
            # next working day asks again - which is what was wanted, and what a
            # boolean could not express.
            request.session[SKIP_DELETE_CONFIRM_KEY] = today().isoformat()

        delete_task(task)
        messages.success(
            request,
            "Task cancelled; generated tasks are kept so they do not reappear."
            if was_generated
            else "Task deleted.",
        )
        return redirect(
            safe_next(request) or reverse("accounts:detail", args=[account_id])
        )


class TaskCreateAnywhereView(SeriesStartMixin, CapabilityRequiredMixin, FormView):
    """Create a task from the task queue, picking the account."""

    required_capability = cap.TASK_CREATE
    template_name = "tasks/task_form.html"
    form_class = TaskAnywhereForm

    def get_initial(self):
        initial = super().get_initial()
        initial.update(_kind_from_query(self.request))
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["accounts"] = self.ability.accounts()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav"] = "tasks"
        context["pick_account"] = True
        context["kind_label"] = kind_label(
            self.request.GET.get("task_type", "MANUAL_NEXT_ACTION")
        )
        return context

    def series_account(self, form):
        """The account is a field here rather than a URL segment."""
        return form.cleaned_data["account"]

    def form_valid(self, form):
        account = form.cleaned_data["account"]
        if form.cleaned_data.get("repeat"):
            task = self._start_series(form)
        else:
            task = create_task(
                account=account,
                task_type=form.cleaned_data["task_type"],
                title=form.cleaned_data["title"],
                description=form.cleaned_data["description"],
                due_date=form.cleaned_data["due_date"],
                due_time=form.cleaned_data.get("due_time"),
                category=form.cleaned_data["category"],
                priority=form.cleaned_data["priority"],
                progress=form.cleaned_data["progress"],
                contact=form.cleaned_data["contact"],
                created_by=self.request.user,
            )
        if task is None:
            messages.warning(self.request, "That task already exists.")
        elif form.cleaned_data["progress"] == TaskProgress.COMPLETED:
            # Through the service, as on the account page. Saving the label
            # alone left a task that read Completed, stayed open, and never
            # reached the log or the score.
            log_after_the_fact(task, form, actor=self.request.user)
            messages.success(self.request, f"Logged: {task.title}")
        else:
            messages.success(self.request, f"Created: {task.title}")
        # Activities, not the task queue: that is the page this now belongs to,
        # and the task list is no longer in the navigation.
        return redirect(safe_next(self.request) or reverse("activities:list"))
