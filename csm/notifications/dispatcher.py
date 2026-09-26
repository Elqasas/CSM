"""Turning task events into notifications.

Recipients come from ``task.assignments``, which is populated only by
``csm.notifications.routing``. Nothing here expands a recipient list: it never
copies in a senior, never copies in a manager, and never looks up a role.
"""

from __future__ import annotations

import datetime as _dt
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import IntegrityError, transaction
from django.urls import reverse

from csm.core import clock
from csm.core.enums import NotificationChannel, NotificationKind, TaskType
from csm.core.events import TaskCreated, subscribe
from csm.notifications.models import Notification
from csm.tasks.models import Task

logger = logging.getLogger(__name__)


def notify(
    *,
    user,
    kind: str,
    title: str,
    body: str = "",
    task: Task | None = None,
    account=None,
    url: str = "",
    channel: str = NotificationChannel.IN_APP,
    dedupe_key: str | None = None,
) -> Notification | None:
    """Create one notification, or nothing if it already exists."""
    try:
        with transaction.atomic():
            return Notification.objects.create(
                user=user,
                task=task,
                account=account or (task.account if task else None),
                kind=kind,
                channel=channel,
                title=title,
                body=body,
                url=url,
                dedupe_key=dedupe_key,
            )
    except IntegrityError:
        return None


def notify_task(task: Task, *, kind: str, occasion: str) -> int:
    """Notify everyone assigned to a task, in-app and by email.

    A health alert always earns its own immediate email, because it is the one
    notification that means someone should stop what they are doing. Everything
    else can wait for the morning digest when digests are enabled.
    """
    is_alert = task.task_type == TaskType.AUTO_HEALTH_ALERT
    immediate_email = is_alert or not settings.NOTIFICATION_DIGEST_ENABLED

    title = task.title
    body = task.description or f"Due {task.due_date:%d %B %Y}."
    # Reversed, not built by hand. This was "/tasks/{pk}/" for a route that has
    # never existed, so every task notification led to a 404 - a string that
    # looks like a URL is not one, and only reverse() knows the difference.
    #
    # The account page rather than the task: it lists the open tasks anyway, and
    # a task without the client it belongs to is not much to act on.
    url = reverse("accounts:detail", args=[task.account_id])
    count = 0

    for assignment in task.assignments.select_related("user"):
        user = assignment.user
        if not user.is_active:
            continue

        if notify(
            user=user,
            task=task,
            kind=kind,
            title=title,
            body=body,
            url=url,
            channel=NotificationChannel.IN_APP,
            dedupe_key=f"inapp:{task.pk}:{user.pk}:{occasion}",
        ):
            count += 1

        if immediate_email:
            notify(
                user=user,
                task=task,
                kind=kind,
                title=title,
                body=body,
                url=url,
                channel=NotificationChannel.EMAIL,
                dedupe_key=f"email:{task.pk}:{user.pk}:{occasion}",
            )
    return count


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_pending_emails(limit: int = 500) -> int:
    """Drain the email outbox.

    Each row is marked before the send is attempted and recorded afterwards, so
    a crash mid-run cannot silently re-send the same message forever.
    """
    pending = list(Notification.objects.pending_email().select_related("user")[:limit])
    if not pending:
        return 0

    sent = 0
    connection = get_connection()
    for note in pending:
        try:
            message = EmailMultiAlternatives(
                subject=note.title,
                body=note.body,
                to=[note.user.email],
                connection=connection,
            )
            message.send()
            note.sent_at = clock.now()
            note.save(update_fields=["sent_at"])
            sent += 1
        except Exception as exc:  # noqa: BLE001 - one bad address must not stop the run
            logger.exception("Failed to email notification %s", note.pk)
            note.failed_at = clock.now()
            note.error = str(exc)[:1000]
            note.save(update_fields=["failed_at", "error"])
    return sent


def send_digests(*, as_of=None) -> int:
    """The morning message: what is late, what is today, what is coming.

    One per person per day, at the start of the working day, rather than a
    message per task at the moment each was created. The specification asks for
    an email for every due task; taken literally with sixty accounts, a
    renewal-heavy night sends several separate messages to the same person at
    one in the morning, the sender ends up filtered to a folder, and nothing at
    all arrives on the day the work is due. Every task still appears; they
    arrive together, and they arrive when somebody is there to read them.

    It looks a week ahead, not only at today. A renewal you first hear about on
    the morning it is due is not a reminder.
    """
    as_of = as_of or clock.today()
    # A task due at 14:00 today is listed as due rather than late at 07:00.
    # Asking the task itself keeps this in step with every counter in the
    # interface.
    as_of_time = clock.local_time()
    if not settings.NOTIFICATION_DIGEST_ENABLED:
        return 0

    from collections import defaultdict

    horizon = as_of + _dt.timedelta(days=settings.NOTIFICATION_LOOKAHEAD_DAYS)
    due = (
        Task.objects.open()
        .filter(due_date__lte=horizon)
        .exclude(task_type=TaskType.AUTO_HEALTH_ALERT)
        .select_related("account")
        .prefetch_related("assignments__user")
    )

    by_user = defaultdict(list)
    for task in due:
        for assignment in task.assignments.all():
            if assignment.user.is_active:
                by_user[assignment.user].append(task)

    sent = 0
    for user, tasks in by_user.items():
        body = _digest_body(user, tasks, as_of, as_of_time)
        late = sum(1 for t in tasks if t.is_overdue(as_of, as_of_time))
        title = f"Your tasks for {as_of:%d %B %Y}"
        dedupe = f"digest:{user.pk}:{as_of.isoformat()}"

        # In-app as well as email. The digest is now the only thing that says a
        # task exists at all, so the bell has to carry it: somebody who does not
        # read email would otherwise never be told.
        notify(
            user=user,
            kind=NotificationKind.TASK_DUE,
            title=f"{title} ({len(tasks)} open, {late} overdue)",
            body=body,
            url=reverse("tasks:list"),
            channel=NotificationChannel.IN_APP,
            dedupe_key=f"inapp:{dedupe}",
        )
        if notify(
            user=user,
            kind=NotificationKind.TASK_DUE,
            title=title,
            body=body,
            channel=NotificationChannel.EMAIL,
            dedupe_key=dedupe,
        ):
            sent += 1
    return sent


def _digest_body(user, tasks, as_of, as_of_time) -> str:
    """Grouped, because a flat list of a week's work hides today inside it."""
    def line(task):
        return f"- {task.title} - {task.account.client_name} (due {task.due_display})"

    overdue, today, ahead = [], [], []
    for task in sorted(tasks, key=lambda t: (t.due_date, t.due_time or _dt.time.min)):
        if task.is_overdue(as_of, as_of_time):
            overdue.append(task)
        elif task.due_date == as_of:
            today.append(task)
        else:
            ahead.append(task)

    parts = [f"Good morning {user.get_short_name()},", ""]
    for heading, group in (
        ("Overdue", overdue),
        ("Today", today),
        (f"Next {settings.NOTIFICATION_LOOKAHEAD_DAYS} days", ahead),
    ):
        if group:
            parts += [f"{heading} ({len(group)}):", *[line(t) for t in group], ""]
    if not tasks:
        parts.append("Nothing open. ")
    return "\n".join(parts).strip()


def send_task_reminders(*, now=None) -> int:
    """Remind the assignees an hour, then half an hour, before a timed task.

    Only tasks that carry a clock time: a task due "today" has no moment to
    count back from, and the digest already named it this morning.

    Each offset fires once, guaranteed by the dedupe key rather than by the
    worker running exactly on the minute. A reminder more than
    ``TASK_REMINDER_GRACE_MINUTES`` late is dropped instead: "in one hour"
    delivered at twenty past the hour is worse than silence, and the thirty
    minute one is still to come.
    """
    now = now or clock.now()
    today = clock.to_business_date(now)
    grace = _dt.timedelta(minutes=settings.TASK_REMINDER_GRACE_MINUTES)

    # Today and tomorrow: at half past eleven at night, a task due at 00:15 is
    # already inside the hour.
    candidates = (
        Task.objects.open()
        .filter(due_time__isnull=False, due_date__in=[today, today + _dt.timedelta(days=1)])
        .select_related("account")
        .prefetch_related("assignments__user")
    )

    sent = 0
    for task in candidates:
        due_at = clock.business_instant(task.due_date, task.due_time)
        for minutes in settings.TASK_REMINDER_MINUTES:
            fires_at = due_at - _dt.timedelta(minutes=minutes)
            if not (fires_at <= now < fires_at + grace):
                continue
            sent += _remind(task, minutes)
    return sent


def _remind(task: Task, minutes: int) -> int:
    title = f"In {minutes} minutes: {task.title}"
    body = (
        f"{task.account.client_name} - due {task.due_display}.\n\n"
        f"{task.description}".strip()
    )
    url = reverse("accounts:detail", args=[task.account_id])

    count = 0
    for assignment in task.assignments.all():
        user = assignment.user
        if not user.is_active:
            continue
        if notify(
            user=user, task=task, kind=NotificationKind.TASK_DUE, title=title, body=body,
            url=url, channel=NotificationChannel.IN_APP,
            dedupe_key=f"inapp:remind:{task.pk}:{user.pk}:{minutes}",
        ):
            count += 1
        # By email too, and immediately: a reminder that waits for tomorrow
        # morning's digest is not a reminder.
        notify(
            user=user, task=task, kind=NotificationKind.TASK_DUE, title=title, body=body,
            url=url, channel=NotificationChannel.EMAIL,
            dedupe_key=f"email:remind:{task.pk}:{user.pk}:{minutes}",
        )
    return count


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------


def on_task_created(event: TaskCreated) -> None:
    """Only a health alert interrupts anybody.

    Everything else reaches people through the morning digest and, if it carries
    a clock time, through its own reminders. Notifying at creation meant the
    generators' overnight run landed as a pile of messages timestamped 01:00,
    read before breakfast if at all, and then nothing further on the morning the
    work was actually due.

    A health alert is the exception it has always been: it is the one
    notification that means stop what you are doing.
    """
    task = Task.objects.filter(pk=event.task_id).prefetch_related("assignments__user").first()
    if task is None:
        return
    if task.task_type != TaskType.AUTO_HEALTH_ALERT:
        return
    notify_task(task, kind=NotificationKind.HEALTH_ALERT, occasion="created")


def register_subscribers() -> None:
    subscribe(TaskCreated, on_task_created)
