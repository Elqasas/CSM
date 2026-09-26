"""Every enumerated value in the system, in one place.

Database check constraints, form choices and UI labels all derive from here, so
a value can only be added or renamed in one file. Nothing in this module may
import from anywhere else in ``csm``.
"""

from django.db import models

# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class Role(models.TextChoices):
    CSM = "CSM", "CSM"
    ACM = "ACM", "ACM"
    MANAGER = "MANAGER", "Manager"
    #: Administers the platform. A role rather than a flag on another role,
    #: because administering who exists is a job, not a decoration on a
    #: portfolio - and because it is the only role that may create a user.
    ADMIN = "ADMIN", "Admin"


class Level(models.TextChoices):
    """Influence and interest share one scale."""

    HIGH = "HIGH", "High"
    MEDIUM = "MEDIUM", "Medium"
    LOW = "LOW", "Low"


class CommunicationChannel(models.TextChoices):
    EMAIL = "EMAIL", "Email"
    PHONE = "PHONE", "Phone"
    WHATSAPP = "WHATSAPP", "WhatsApp"
    TEAMS = "TEAMS", "Teams"
    IN_PERSON = "IN_PERSON", "In person"


class PowerInterestQuadrant(models.TextChoices):
    MANAGE_CLOSELY = "MANAGE_CLOSELY", "Manage closely"
    KEEP_SATISFIED = "KEEP_SATISFIED", "Keep satisfied"
    KEEP_INFORMED = "KEEP_INFORMED", "Keep informed"
    MONITOR = "MONITOR", "Monitor"


# ---------------------------------------------------------------------------
# Account classification
class CustomFieldType(models.TextChoices):
    """What an admin-defined account field renders as.

    A closed set of three, not an open type system: the request behind
    ``AccountCustomFieldDefinition`` was "let me add a field", not a schema
    builder.
    """

    TEXT = "TEXT", "Free text"
    DATETIME = "DATETIME", "Date/time"
    DROPDOWN = "DROPDOWN", "Dropdown list"


# ---------------------------------------------------------------------------
# The three classifications below are deliberately independent of one another.
# In particular AccountClass is never read by the scoring engine.


class AccountClass(models.TextChoices):
    VIP = "VIP", "VIP"
    A = "A", "A"
    B = "B", "B"
    C = "C", "C"
    D = "D", "D"


class LicenseType(models.TextChoices):
    """The dPhish product edition the client is on.

    A closed set rather than free text: it drives a dashboard card, and a field
    that can hold "Complete", "complete " and "Complete Edition" produces three
    bars for one product.
    """

    ADVANTAGE = "ADVANTAGE", "Advantage"
    COMPLETE = "COMPLETE", "Complete"
    DRILL = "DRILL", "Drill"
    DISCOVER = "DISCOVER", "Discover"
    ESSENTIAL = "ESSENTIAL", "Essential"
    PHISHING = "PHISHING", "Phishing"


class AccountSize(models.TextChoices):
    ENTERPRISE = "ENTERPRISE", "Enterprise"
    MID = "MID", "Mid"
    SMB = "SMB", "SMB"


class Deployment(models.TextChoices):
    CLOUD = "CLOUD", "Cloud"
    ON_PREM = "ON_PREM", "On-premises"


class LifecycleStage(models.TextChoices):
    POC = "POC", "POC"
    CLOSED_WON = "CLOSED_WON", "Closed-Won"
    CLOSED_LOST = "CLOSED_LOST", "Closed-Lost"
    ONBOARDING = "ONBOARDING", "Onboarding"
    ACTIVE = "ACTIVE", "Active"
    ADOPTION = "ADOPTION", "Adoption"
    RENEWAL = "RENEWAL", "Renewal"
    EXPANSION = "EXPANSION", "Expansion"
    CHURNED = "CHURNED", "Churned"


#: Stages in which the health score is calculated. All four share one code path
#: with no stage-dependent branching whatsoever.
SCORABLE_STAGES = frozenset(
    {
        LifecycleStage.ACTIVE,
        LifecycleStage.ADOPTION,
        LifecycleStage.RENEWAL,
        LifecycleStage.EXPANSION,
    }
)

#: Stages in which health is not applicable.
NON_SCORABLE_STAGES = frozenset(
    {
        LifecycleStage.POC,
        LifecycleStage.CLOSED_WON,
        LifecycleStage.CLOSED_LOST,
        LifecycleStage.ONBOARDING,
        LifecycleStage.CHURNED,
    }
)

#: Closed-Lost is a permanent terminal state; Churned is reversible to
#: Onboarding when a new contract is signed.
TERMINAL_STAGES = frozenset({LifecycleStage.CLOSED_LOST})

#: Stages that no longer generate renewal or quarterly-meeting reminders.
NO_REMINDER_STAGES = frozenset({LifecycleStage.CLOSED_LOST, LifecycleStage.CHURNED})


class ReviewStatus(models.TextChoices):
    NONE = "NONE", "None"
    REQUESTED = "REQUESTED", "Requested"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    PUBLISHED = "PUBLISHED", "Published"
    DECLINED = "DECLINED", "Declined"


class MeetingFrequency(models.IntegerChoices):
    """Stored as meetings per month so compliance is a direct division."""

    MONTHLY = 1, "Monthly (1x/month)"
    BIWEEKLY = 2, "Bi-weekly (2x/month)"
    WEEKLY = 4, "Weekly (4x/month)"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthStatus(models.TextChoices):
    HEALTHY = "HEALTHY", "Healthy"
    WATCH = "WATCH", "Watch"
    AT_RISK = "AT_RISK", "At Risk"
    NOT_APPLICABLE = "NOT_APPLICABLE", "N/A"


#: A real enum member rather than NULL, so dashboard filters avoid
#: three-valued logic.
SCORED_STATUSES = frozenset(
    {HealthStatus.HEALTHY, HealthStatus.WATCH, HealthStatus.AT_RISK}
)


class FactorColor(models.TextChoices):
    GREEN = "GREEN", "Green"
    YELLOW = "YELLOW", "Yellow"
    RED = "RED", "Red"


class FactorId(models.TextChoices):
    OPEN_TICKETS = "OPEN_TICKETS", "Open Tickets"
    MONTHLY_ADOPTION = "MONTHLY_ADOPTION", "Monthly Adoption"
    CSAT = "CSAT", "CSAT"
    GENERAL_CONTACT = "GENERAL_CONTACT", "General Contact"
    WEEKLY_CONTACT = "WEEKLY_CONTACT", "Weekly Contact"
    MEETING_ATTENDANCE = "MEETING_ATTENDANCE", "Meeting Attendance"


class FactorSetId(models.TextChoices):
    MANAGED = "MANAGED", "Managed Service"
    NON_MANAGED = "NON_MANAGED", "Non-Managed"


class FactorOutcome(models.TextChoices):
    SCORED = "SCORED", "Scored"
    OMITTED = "OMITTED", "Omitted"


class ScoreTrigger(models.TextChoices):
    EVENT = "EVENT", "Event-driven"
    DAILY = "DAILY", "Daily scheduled"
    MANUAL = "MANUAL", "Manual refresh"
    IMPORT = "IMPORT", "Import"
    STAGE_CHANGE = "STAGE_CHANGE", "Lifecycle stage change"
    OVERRIDE_SET = "OVERRIDE_SET", "Override set"


class OverrideClearedReason(models.TextChoices):
    SCORE_CHANGED = "SCORE_CHANGED", "Computed score changed"
    MANUAL = "MANUAL", "Cleared manually"
    STAGE_NOT_SCORABLE = "STAGE_NOT_SCORABLE", "Stage no longer scored"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    ACCOUNT_DELETED = "ACCOUNT_DELETED", "Account deleted"


# ---------------------------------------------------------------------------
# Activities and health inputs
# ---------------------------------------------------------------------------


class ActivityType(models.TextChoices):
    CALL = "CALL", "Call"
    EMAIL = "EMAIL", "Email"
    MEETING = "MEETING", "Meeting"
    WHATSAPP = "WHATSAPP", "WhatsApp"
    TEAMS = "TEAMS", "Teams"
    IN_PERSON = "IN_PERSON", "In person"
    NOTE = "NOTE", "Note"
    OTHER = "OTHER", "Other"


#: A note is a record of thinking, not of contact, so it counts for neither
#: contact factor.
NON_CONTACT_ACTIVITY_TYPES = frozenset({ActivityType.NOTE})

#: General Contact counts any real contact, meetings included.
GENERAL_CONTACT_TYPES = frozenset(
    t for t in ActivityType if t not in NON_CONTACT_ACTIVITY_TYPES
)

#: Weekly Contact excludes meetings, which are counted only under Meeting
#: Attendance. This is the spec's explicit double-counting guard.
WEEKLY_CONTACT_TYPES = frozenset(GENERAL_CONTACT_TYPES - {ActivityType.MEETING})


class ActivitySource(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    OUTLOOK = "OUTLOOK", "Outlook"
    TEAMS = "TEAMS", "Teams"
    JIRA = "JIRA", "Jira"


class TicketKind(models.TextChoices):
    BUG = "BUG", "Bug"
    GENERAL = "GENERAL", "General"


class TicketStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"


class TicketSource(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    JIRA = "JIRA", "Jira"


class CsatSource(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    SURVEY = "SURVEY", "Survey"


class AdoptionEventType(models.TextChoices):
    PHISHING_CAMPAIGN = "PHISHING_CAMPAIGN", "Phishing campaign"
    COURSE_ASSIGNED = "COURSE_ASSIGNED", "Course assigned"


class EngagementItemType(models.TextChoices):
    EVENT = "EVENT", "Event"
    DIGITAL_CONTENT = "DIGITAL_CONTENT", "Digital content"


# ---------------------------------------------------------------------------
# Tasks and notifications
# ---------------------------------------------------------------------------


class TaskType(models.TextChoices):
    AUTO_RENEWAL = "AUTO_RENEWAL", "Auto-Renewal"
    AUTO_QUARTERLY_MEETING = "AUTO_QUARTERLY_MEETING", "Auto-Quarterly-Meeting"
    #: Stored value unchanged - it is in every history row and idempotency key
    #: ever written - but shown as what people call it.
    MANUAL_NEXT_ACTION = "MANUAL_NEXT_ACTION", "Task"
    AUTO_HEALTH_ALERT = "AUTO_HEALTH_ALERT", "Auto-Health-Alert"
    #: Two more kinds of hand-made task. A meeting or a call is something you
    #: arrange and then do, which is a task; the activity log records the one
    #: that happened. The pair is the plan and the record, not two spellings of
    #: the same thing - and keeping them apart is what lets "we said we would
    #: call them" and "we called them" disagree, which is the interesting case.
    MEETING = "MEETING", "Meeting"
    CALL = "CALL", "Call"


AUTO_TASK_TYPES = frozenset(
    {
        TaskType.AUTO_RENEWAL,
        TaskType.AUTO_QUARTERLY_MEETING,
        TaskType.AUTO_HEALTH_ALERT,
    }
)

#: The types a person may create. Everything else is written by a generator
#: against an idempotency key, and a hand-made one would either collide with
#: that key or, worse, not collide and duplicate the obligation.
MANUAL_TASK_TYPES = (
    TaskType.MANUAL_NEXT_ACTION,
    TaskType.MEETING,
    TaskType.CALL,
)


class TaskCategory(models.TextChoices):
    INTERNAL = "INTERNAL", "Internal"
    CLIENT_FACING = "CLIENT_FACING", "Client-facing"
    PARTNER_FACING = "PARTNER_FACING", "Partner-facing"


#: Only Internal tasks are eligible for future Jira ticket creation.
JIRA_ELIGIBLE_CATEGORIES = frozenset({TaskCategory.INTERNAL})


class TaskPriority(models.TextChoices):
    """How much a task matters, separately from when it is due.

    Its own enum rather than the shared ``Level``: influence, interest and
    priority happen to have three values each today, and folding them together
    would mean a change to one silently moving the others.
    """

    HIGH = "HIGH", "High"
    MEDIUM = "MEDIUM", "Medium"
    LOW = "LOW", "Low"


#: What a task is worth when nobody says. Medium rather than Low, because a
#: default of Low would make "priority" mean "somebody remembered to set it".
DEFAULT_TASK_PRIORITY = TaskPriority.MEDIUM


class RepeatFrequency(models.TextChoices):
    """How often a repeating task comes round."""

    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"
    YEARLY = "YEARLY", "Yearly"


class RepeatEnd(models.TextChoices):
    """When it stops."""

    NEVER = "NEVER", "Never"
    AFTER = "AFTER", "After a number of times"
    ON = "ON", "On a date"


class TaskProgress(models.TextChoices):
    """Where the work has got to, as the person doing it describes it.

    Distinct from ``TaskStatus``, which is the lifecycle: open, done,
    auto-closed, cancelled. That one is decided by the application - the Done
    button, a generator closing an alert, a cancellation - and this one is
    chosen by hand.

    Overdue is neither: it is a fact about the clock, not a state anybody sets,
    and it belongs beside the due date rather than in this list.
    """

    NOT_STARTED = "NOT_STARTED", "Not started"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    WAITING_INPUT = "WAITING_INPUT", "Waiting for input"
    COMPLETED = "COMPLETED", "Completed"


#: What the form offers, Completed included. Choosing it closes the task through
#: ``complete_task`` rather than merely labelling it, so a thing recorded after
#: the fact - an unplanned call - is one step: add it, set Completed, done. That
#: is what replaced the separate "log an activity" form, and it is why the
#: status and the lifecycle can never disagree.
SELECTABLE_TASK_PROGRESS = (
    TaskProgress.NOT_STARTED,
    TaskProgress.IN_PROGRESS,
    TaskProgress.WAITING_INPUT,
    TaskProgress.COMPLETED,
)


class TaskStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    DONE = "DONE", "Done"
    AUTO_CLOSED = "AUTO_CLOSED", "Auto-closed"
    CANCELLED = "CANCELLED", "Cancelled"


OPEN_TASK_STATUSES = frozenset({TaskStatus.PENDING})
CLOSED_TASK_STATUSES = frozenset(
    {TaskStatus.DONE, TaskStatus.AUTO_CLOSED, TaskStatus.CANCELLED}
)


class TaskCloseReason(models.TextChoices):
    COMPLETED = "COMPLETED", "Completed"
    HEALTH_RECOVERED = "HEALTH_RECOVERED", "Health recovered"
    CONTRACT_DATE_CHANGED = "CONTRACT_DATE_CHANGED", "Contract date changed"
    MEETING_DATE_CHANGED = "MEETING_DATE_CHANGED", "Meeting date changed"
    CANCELLED_BY_USER = "CANCELLED_BY_USER", "Cancelled by user"
    STAGE_NOT_SCORABLE = "STAGE_NOT_SCORABLE", "Stage no longer scored"


class TaskAssignmentRole(models.TextChoices):
    CSM = "CSM", "CSM"
    ACM = "ACM", "ACM"
    CREATOR = "CREATOR", "Creator"


class NotificationKind(models.TextChoices):
    TASK_ASSIGNED = "TASK_ASSIGNED", "Task assigned"
    TASK_DUE = "TASK_DUE", "Task due"
    TASK_OVERDUE = "TASK_OVERDUE", "Task overdue"
    HEALTH_ALERT = "HEALTH_ALERT", "Health alert"
    TASK_AUTO_CLOSED = "TASK_AUTO_CLOSED", "Task auto-closed"


class NotificationChannel(models.TextChoices):
    IN_APP = "IN_APP", "In-app"
    EMAIL = "EMAIL", "Email"


# ---------------------------------------------------------------------------
# Import and integrations
# ---------------------------------------------------------------------------


class ImportBatchStatus(models.TextChoices):
    UPLOADED = "UPLOADED", "Uploaded"
    VALIDATED = "VALIDATED", "Validated"
    APPLIED = "APPLIED", "Applied"
    FAILED = "FAILED", "Failed"


class ImportRowStatus(models.TextChoices):
    OK = "OK", "OK"
    ERROR = "ERROR", "Error"
    SKIPPED = "SKIPPED", "Skipped"


class IntegrationProvider(models.TextChoices):
    JIRA = "JIRA", "Jira"
    TEAMS = "TEAMS", "Microsoft Teams"
    OUTLOOK = "OUTLOOK", "Outlook"


class AuditAction(models.TextChoices):
    CREATE = "CREATE", "Create"
    UPDATE = "UPDATE", "Update"
    DELETE = "DELETE", "Delete"
    RESTORE = "RESTORE", "Restore"
    STAGE_CHANGE = "STAGE_CHANGE", "Lifecycle stage change"
    OVERRIDE_SET = "OVERRIDE_SET", "Health override set"
    OVERRIDE_CLEAR = "OVERRIDE_CLEAR", "Health override cleared"
    REASSIGN = "REASSIGN", "Reassignment"
    IMPORT_APPLY = "IMPORT_APPLY", "Import applied"
