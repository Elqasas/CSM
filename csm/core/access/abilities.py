"""The only place in the codebase that interprets a user's role.

Two axes are decided here and nowhere else:

* **Row scope** — which accounts a user can see at all, via ``accounts()``.
* **Capability** — what a user may do, via ``can()``.

Notification routing is deliberately *not* here. It lives in
``csm.notifications.routing``, which imports nothing from this package, so a
senior CSM's broad access can never widen who gets notified.
"""

from __future__ import annotations

from functools import cached_property

from csm.core.access import capabilities as cap
from csm.core.enums import Role

#: Capabilities granted to each role. Scope (own accounts versus all accounts)
#: is a separate question answered by ``accounts()``.
_MATRIX: dict[str, frozenset[str]] = {
    Role.CSM.value: frozenset(
        {
            cap.ACCOUNT_VIEW,
            cap.ACCOUNT_CREATE,
            cap.ACCOUNT_EDIT,
            cap.ACCOUNT_DELETE,
            cap.CONTACT_VIEW,
            cap.CONTACT_EDIT,
            cap.ACTIVITY_VIEW,
            cap.ACTIVITY_CREATE,
            cap.ACTIVITY_VOID,
            cap.TICKET_EDIT,
            cap.CSAT_RECORD,
            cap.ADOPTION_RECORD,
            cap.ENGAGEMENT_EDIT,
            cap.NOTE_VIEW,
            cap.NOTE_EDIT,
            cap.TASK_VIEW,
            cap.TASK_CREATE,
            cap.TASK_EDIT,
            cap.TASK_DELETE,
            cap.TASK_COMPLETE,
            cap.HEALTH_VIEW,
            cap.HEALTH_OVERRIDE,
            cap.LIFECYCLE_EDIT,
            cap.DASHBOARD_VIEW,
            cap.MYDAY_VIEW,
            cap.IMPORT_RUN,
            cap.EXPORT_RUN,
        }
    ),
    Role.ACM.value: frozenset(
        {
            cap.ACCOUNT_VIEW,
            cap.CONTACT_VIEW,
            cap.CONTACT_EDIT,
            cap.ACTIVITY_VIEW,
            cap.ACTIVITY_CREATE,
            cap.TASK_VIEW,
            cap.TASK_CREATE,
            cap.TASK_EDIT,
            cap.TASK_DELETE,
            cap.TASK_COMPLETE,
            cap.DASHBOARD_VIEW,
            cap.MYDAY_VIEW,
            cap.IMPORT_RUN,
            cap.EXPORT_RUN,
            # Deliberately absent: HEALTH_VIEW, HEALTH_OVERRIDE, LIFECYCLE_EDIT,
            # TICKET_EDIT, CSAT_RECORD, ADOPTION_RECORD, ACCOUNT_EDIT, NOTE_VIEW,
            # NOTE_EDIT. An ACM owns
            # the commercial renewal and is kept clear of anything that signals
            # account health, and never edits the account record itself. Notes
            # are a CSM's free text about the client, the sort of place "they
            # sounded unhappy" gets written down, so they stay on the CSM side.
            # IMPORT_RUN is held but narrowed to contacts only by
            # ``importable_kinds()`` below: the importer writes accounts
            # regardless of ``account.edit``, and an accounts import would
            # reopen the account record ACM has no other way to write.
        }
    ),
    Role.MANAGER.value: frozenset(
        {
            cap.ACCOUNT_VIEW,
            cap.TASK_VIEW,
            cap.CONTACT_VIEW,
            cap.ACTIVITY_VIEW,
            cap.HEALTH_VIEW,
            cap.NOTE_VIEW,
            cap.DASHBOARD_VIEW,
            cap.EXPORT_RUN,
            # Deliberately absent: MYDAY_VIEW. A manager holds no queue of their
            # own, so the landing page and the logo take them to Analytics.
        }
    ),
    # Everything, including the one capability nobody else holds. Written as
    # the whole vocabulary rather than a list, so a capability added later is
    # the administrator's by default: an administrator who cannot reach a new
    # corner of the system is a support ticket, not a safeguard.
    Role.ADMIN.value: frozenset(cap.ALL),
}

#: Account fields nobody edits through a form: they are written by the health
#: service, by the importer, or by the database.
SYSTEM_ACCOUNT_FIELDS = frozenset(
    {
        "computed_health_score",
        "computed_health_max",
        "computed_health_status",
        "effective_health_status",
        "health_calculated_at",
        "red_since",
        "health_alert_suppressed_at",
        "total_users_count",
        "has_event",
        "scoring_baseline_date",
        "deleted_at",
        "deleted_by",
        "imported_at",
        "created_by",
        "stage_changed_at",
    }
)


class Ability:
    """What one user may see and do.

    Construct it once per request and pass it down. Views, templates and
    presenters ask this object; none of them reads ``role`` or ``is_senior``.
    """

    def __init__(self, user):
        self.user = user

    # -- Identity ---------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return bool(getattr(self.user, "is_authenticated", False))

    @cached_property
    def _role(self) -> str | None:
        if not self.is_authenticated or not getattr(self.user, "is_active", False):
            return None
        return self.user.role

    @cached_property
    def _is_senior(self) -> bool:
        return bool(self._role == Role.CSM.value and self.user.is_senior)

    @cached_property
    def is_admin(self) -> bool:
        """Whether this user administers the platform.

        A role, not a flag. It used to be ``is_superuser`` sitting on top of
        whatever else somebody was, which made "who can create a user" a
        question about a Django field rather than about this application's
        access levels.
        """
        return self._role == Role.ADMIN.value

    @cached_property
    def has_django_admin(self) -> bool:
        """Whether this user may reach Django's own admin site.

        A different question from ``is_admin``: that one is an access level in
        this application, this one is a Django flag on the account. Read here
        rather than in a template, for the same reason ``role`` is.
        """
        if not self.is_authenticated or not getattr(self.user, "is_active", False):
            return False
        return bool(
            getattr(self.user, "is_staff", False)
            and getattr(self.user, "is_superuser", False)
        )

    @cached_property
    def capabilities(self) -> frozenset[str]:
        if self._role is None:
            return frozenset()
        return _MATRIX.get(self._role, frozenset())

    @property
    def level_label(self) -> str:
        if self._role is None:
            return "Signed out"
        if self._is_senior:
            return "CSM Senior"
        return Role(self._role).label

    @property
    def level_badge(self) -> str:
        """What the interface shows beside a name.

        The same as the label now that Admin is a level of its own. Kept as its
        own property because templates call it, and because the two were
        different facts until recently - a name worth keeping separate from the
        label in case they diverge again.
        """
        return self.level_label

    # -- Capability -------------------------------------------------------

    def can(self, capability: str) -> bool:
        if capability not in cap.ALL:
            raise ValueError(f"Unknown capability: {capability!r}")
        return capability in self.capabilities

    def can_all(self, capabilities) -> bool:
        return all(self.can(c) for c in capabilities)

    # -- Row scope --------------------------------------------------------

    def accounts(self):
        """Every account this user may see, and no others.

        Everything that lists, searches, filters or aggregates accounts starts
        here. Global search especially: a CSM searching a colleague's client by
        name must find nothing, or the search bar quietly defeats the scoping.
        """
        from csm.accounts.models import Account

        base = Account.objects.alive()
        if self._role is None:
            return base.none()
        # An administrator sees every account and is the CSM of record on none:
        # the job is running the platform, not carrying a portfolio.
        if self._role in (Role.MANAGER.value, Role.ADMIN.value):
            return base
        if self._role == Role.CSM.value:
            return base if self._is_senior else base.filter(csm=self.user)
        if self._role == Role.ACM.value:
            return base.filter(acm=self.user)
        return base.none()

    @cached_property
    def _visible_account_ids(self) -> frozenset:
        return frozenset(self.accounts().values_list("pk", flat=True))

    def can_see_account(self, account) -> bool:
        # Asked once per row by every list's action menu - four questions a
        # row, a hundred queries a page. The visible set is read once per
        # Ability, which lives for one request. Only a hit is trusted: an
        # account created after the set was read would be missing from it, so
        # a miss asks the database rather than answering no.
        if account.pk in self._visible_account_ids:
            return True
        return self.accounts().filter(pk=account.pk).exists()

    # -- Object-level decisions -------------------------------------------

    def assignable_csms(self):
        """Who this reader may name as an account's CSM.

        A CSM creating an account is taking it on, not handing it out: they get
        themselves and nothing else. Anyone who can see the whole book - a
        senior CSM, a manager, an administrator - may assign whoever should
        actually own it.

        Here rather than in the form, for the same reason every other question
        about what a role may do is: the form asks and does not decide.
        """
        from csm.core.enums import Role
        from csm.users.models import User

        eligible = User.objects.filter(role=Role.CSM, is_active=True)
        if self._role == Role.CSM.value and not self._is_senior:
            return eligible.filter(pk=self.user.pk)
        return eligible

    def assignable_acms(self):
        """Who this reader may name as an account's ACM.

        Unrestricted: an ACM is the commercial counterpart, chosen rather than
        claimed, and a CSM naming one is not giving away their own work.
        """
        from csm.core.enums import Role
        from csm.users.models import User

        return User.objects.filter(role=Role.ACM, is_active=True)

    def can_edit_account(self, account) -> bool:
        return self.can(cap.ACCOUNT_EDIT) and self.can_see_account(account)

    def can_delete_account(self, account) -> bool:
        """Deleting an account cascades. Row scope still limits a plain CSM to
        their own portfolio; a senior CSM's broader reach is what lets them
        delete anyone's."""
        return self.can(cap.ACCOUNT_DELETE) and self.can_see_account(account)

    def can_view_health(self, account=None) -> bool:
        if not self.can(cap.HEALTH_VIEW):
            return False
        return account is None or self.can_see_account(account)

    def can_override_health(self, account) -> bool:
        return self.can(cap.HEALTH_OVERRIDE) and self.can_see_account(account)

    def can_log_activity(self, account) -> bool:
        return self.can(cap.ACTIVITY_CREATE) and self.can_see_account(account)

    def can_edit_task(self, task) -> bool:
        return self.can(cap.TASK_EDIT) and self.can_see_account(task.account)

    def can_change_lifecycle(self, account) -> bool:
        return self.can(cap.LIFECYCLE_EDIT) and self.can_see_account(account)

    # -- Field scope ------------------------------------------------------

    def editable_account_fields(self, all_fields) -> frozenset[str]:
        """Narrow a form's field list to what this user may change."""
        if not self.can(cap.ACCOUNT_EDIT):
            return frozenset()
        allowed = frozenset(all_fields) - SYSTEM_ACCOUNT_FIELDS
        if not self.can(cap.LIFECYCLE_EDIT):
            allowed = allowed - {"lifecycle_stage"}
        return allowed

    # -- Import scope -------------------------------------------------------

    def importable_kinds(self) -> frozenset[str]:
        """Which ``ImportKind`` values this user may upload, preview and apply.

        A finer grain than ``import.run`` itself: the importer writes accounts
        with no ``account.edit`` check of its own (see ``importer/views.py``),
        so an ACM holding the bare capability could write account records they
        have no other way to touch. Contacts carry no health signal and no
        commercial field, so that half of the wizard is open to them; accounts
        is not.
        """
        from csm.importer.models import ImportKind

        if not self.can(cap.IMPORT_RUN):
            return frozenset()
        if self._role == Role.ACM.value:
            return frozenset({ImportKind.CONTACTS})
        return frozenset({ImportKind.ACCOUNTS, ImportKind.CONTACTS})

    # -- Task visibility --------------------------------------------------

    def visible_tasks(self):
        """Tasks this user may see.

        An ACM never sees a health alert, even on an account assigned to them.
        Routing already keeps alerts away from them, but a shared account list
        would otherwise leak the fact that an alert exists, which is itself a
        health signal.
        """
        from csm.core.enums import TaskType
        from csm.tasks.models import Task

        qs = Task.objects.filter(account__in=self.accounts())
        if not self.can(cap.HEALTH_VIEW):
            qs = qs.exclude(task_type=TaskType.AUTO_HEALTH_ALERT)
        return qs

    def visible_activities(self, qs):
        """Narrow an activity queryset to what this user may see.

        An ACM sees only what the system logged itself - a future Outlook or
        Teams sync - never a CSM's own record of a call or meeting, which is
        exactly the log the health engine reads. Everyone else sees whatever
        row scope already limited them to.
        """
        from csm.core.enums import ActivitySource

        if self._role == Role.ACM.value:
            return qs.exclude(source=ActivitySource.MANUAL)
        return qs
