"""The closed set of things a user can be permitted to do.

Capabilities are strings rather than an enum so templates can name them without
an import, but the set is closed: ``ALL`` is the whole vocabulary and a typo in a
view is caught by the check in ``Ability.can``.
"""

from __future__ import annotations

ACCOUNT_VIEW = "account.view"
ACCOUNT_CREATE = "account.create"
ACCOUNT_EDIT = "account.edit"
ACCOUNT_DELETE = "account.delete"

CONTACT_VIEW = "contact.view"
CONTACT_EDIT = "contact.edit"

ACTIVITY_VIEW = "activity.view"
ACTIVITY_CREATE = "activity.create"
ACTIVITY_VOID = "activity.void"

TICKET_EDIT = "ticket.edit"
CSAT_RECORD = "csat.record"
ADOPTION_RECORD = "adoption.record"
ENGAGEMENT_EDIT = "engagement.edit"

NOTE_VIEW = "note.view"
NOTE_EDIT = "note.edit"

TASK_VIEW = "task.view"
TASK_CREATE = "task.create"
TASK_EDIT = "task.edit"
TASK_DELETE = "task.delete"
TASK_COMPLETE = "task.complete"

HEALTH_VIEW = "health.view"
HEALTH_OVERRIDE = "health.override"

LIFECYCLE_EDIT = "lifecycle.edit"

DASHBOARD_VIEW = "dashboard.view"
#: The "My day" landing page: one person's own queue. A manager owns no queue,
#: so a manager lands on Analytics instead.
MYDAY_VIEW = "myday.view"
IMPORT_RUN = "import.run"
EXPORT_RUN = "export.run"
#: The date-ranged reports: tasks, activities, health, renewals. Separate from
#: ``export.run``, which an ACM holds for the accounts and contacts sheets alone -
#: every report under this one reads the CSM side of the work.
REPORT_VIEW = "report.view"
USER_MANAGE = "user.manage"
CUSTOM_FIELD_MANAGE = "custom_field.manage"
INTEGRATION_MANAGE = "integration.manage"

ALL = frozenset(
    {
        ACCOUNT_VIEW,
        ACCOUNT_CREATE,
        ACCOUNT_EDIT,
        ACCOUNT_DELETE,
        CONTACT_VIEW,
        CONTACT_EDIT,
        ACTIVITY_VIEW,
        ACTIVITY_CREATE,
        ACTIVITY_VOID,
        TICKET_EDIT,
        CSAT_RECORD,
        ADOPTION_RECORD,
        ENGAGEMENT_EDIT,
        NOTE_VIEW,
        NOTE_EDIT,
        TASK_VIEW,
        TASK_CREATE,
        TASK_EDIT,
        TASK_DELETE,
        TASK_COMPLETE,
        HEALTH_VIEW,
        HEALTH_OVERRIDE,
        LIFECYCLE_EDIT,
        DASHBOARD_VIEW,
        MYDAY_VIEW,
        IMPORT_RUN,
        EXPORT_RUN,
        REPORT_VIEW,
        USER_MANAGE,
        CUSTOM_FIELD_MANAGE,
        INTEGRATION_MANAGE,
    }
)
