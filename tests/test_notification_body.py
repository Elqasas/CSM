"""A digest on the notifications page reads as a list, not one paragraph."""

from __future__ import annotations

from django.urls import reverse

from csm.core.enums import NotificationChannel, NotificationKind
from csm.notifications.models import Notification
from csm.notifications.templatetags.notification_body import body_lines
from tests.factories import UserFactory
from tests.test_views import PASSWORD, login

DIGEST = (
    "Good morning Ahmed,\n\n"
    "Overdue (1):\n- test 3 - Arabia Telecom 10 (due 22 Sep 2026 at 14:00)\n\n"
    "Today (1):\n- call - dPhish account (due 23 Sep 2026)\n\n"
    "Next 7 days (2):\n- renewal - Gulf Bank (due 25 Sep 2026)\n- review - Misk (due 28 Sep 2026)"
)


def test_a_digest_splits_into_greeting_headings_and_one_line_per_task():
    lines = body_lines(DIGEST)

    assert [line["kind"] for line in lines] == [
        "text", "heading", "item", "heading", "item", "heading", "item", "item",
    ]
    headings = [line for line in lines if line["kind"] == "heading"]
    assert [(h["text"], h["count"], h["tone"]) for h in headings] == [
        ("Overdue", "1", "danger"),
        ("Today", "1", "primary"),
        ("Next 7 days", "2", "secondary"),
    ]
    assert lines[2]["text"] == "test 3 - Arabia Telecom 10 (due 22 Sep 2026 at 14:00)"


def test_a_plain_body_is_left_as_text():
    assert body_lines("Gulf Bank turned At Risk.") == [
        {"kind": "text", "text": "Gulf Bank turned At Risk."}
    ]
    assert body_lines("") == []


def test_the_page_puts_each_task_on_its_own_line(client, db):
    user = UserFactory(csm=True, email="digest-reader@dphish.com")
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    Notification.objects.create(
        user=user,
        kind=NotificationKind.TASK_DUE,
        channel=NotificationChannel.IN_APP,
        title="Your tasks for 23 September 2026 (2 open, 1 overdue)",
        body=DIGEST,
    )
    login(client, user)
    body = client.get(reverse("notifications:list")).content.decode()

    assert body.count('class="note-item"') == 4
    assert "text-danger mt-2" in body  # Overdue, in the alarm colour
