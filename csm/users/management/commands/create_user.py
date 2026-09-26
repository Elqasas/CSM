"""Create a real staff member.

Django's ``createsuperuser`` makes a database administrator, which is a
different thing from a platform user: this application's access levels come from
``role`` and ``is_senior``, and a superuser bypasses all of it. This command
creates someone at one of the four access levels instead, and grants Django
admin only when asked for explicitly.

The four levels come from two fields, which is why ``--senior`` is a flag on CSM
rather than a role of its own. A database constraint enforces that; passing
``--senior`` with any other role is rejected here first, with an explanation
rather than an IntegrityError.
"""

from __future__ import annotations

import secrets
import string

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from csm.core.enums import Role
from csm.users.models import User

#: Unambiguous when read aloud or copied: no l/1/I or O/0.
_ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
    + "!@#$%^&*-_=+"
)


def generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


class Command(BaseCommand):
    help = "Create a dPhish staff member at one of the four access levels."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("full_name")
        parser.add_argument(
            "--role",
            default=Role.CSM,
            choices=[value for value, _ in Role.choices],
            help="CSM, ACM or MANAGER. Defaults to CSM.",
        )
        parser.add_argument(
            "--senior",
            action="store_true",
            help="Promote a CSM to CSM Senior: may view and act on every account.",
        )
        parser.add_argument(
            "--admin",
            action="store_true",
            help=(
                "Also grant Django admin. This bypasses the whole access layer, "
                "so grant it only to whoever administers the system."
            ),
        )
        parser.add_argument(
            "--password",
            help="Set a password. One is generated and printed if you omit this.",
        )
        parser.add_argument(
            "--inactive",
            action="store_true",
            help="Create the account disabled, to be enabled later.",
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        role = options["role"]
        senior = options["senior"]

        if senior and role != Role.CSM:
            raise CommandError(
                "--senior only applies to a CSM. The access levels are CSM, CSM "
                "Senior, ACM, Manager and Admin; seniority is not a thing an ACM, "
                "a Manager or an Admin has."
            )

        # Admin is a role now, so --admin and --role are two ways of saying the
        # same thing. Contradicting each other silently would be worse than
        # refusing: one of them has to lose, and the operator cannot see which.
        if options["admin"] and role != Role.ADMIN:
            if options["role"] != Role.CSM:
                raise CommandError(
                    "--admin is the Admin role. Pass --role ADMIN, or drop "
                    "--admin and choose a different role - not both."
                )
            role = Role.ADMIN

        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(
                f"{email} already exists. Change them in the admin, or pick "
                "another address."
            )

        password = options["password"] or generate_password()
        generated = not options["password"]

        try:
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=options["full_name"],
                role=role,
                is_senior=senior,
                is_active=not options["inactive"],
                # Django's own admin site follows the role: an administrator
                # who can reach one surface and not the other is a puzzle.
                is_staff=role == Role.ADMIN,
                is_superuser=role == Role.ADMIN,
            )
        except IntegrityError as exc:
            raise CommandError(f"The database refused this user: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(f"Created {user.email} as {user.effective_level_label}")
        )
        if role == Role.ADMIN:
            self.stdout.write(
                self.style.WARNING(
                    "  Django admin granted. That bypasses row scope, field scope "
                    "and the health rules entirely."
                )
            )
        if options["inactive"]:
            self.stdout.write("  Disabled. Enable them in the admin when ready.")

        if generated:
            self.stdout.write("")
            self.stdout.write(f"  Password: {password}")
            self.stdout.write(
                "  Shown once and not stored anywhere in readable form. Hand it "
                "over out of band and have them change it."
            )
