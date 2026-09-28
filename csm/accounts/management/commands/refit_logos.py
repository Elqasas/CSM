"""Refit logos uploaded before the upload itself did.

Safe to re-run: a logo already at the stored size is skipped, so the margin is
never added twice. The old file is deleted only once the new one is saved and
the row points at it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from csm.accounts.logos import fit_logo, is_fitted
from csm.accounts.models import Account


class Command(BaseCommand):
    help = "Refit every stored account logo to the square it is shown in."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change and write nothing."
        )

    def handle(self, *args, dry_run: bool = False, **options):
        refitted = skipped = failed = 0
        for account in Account.objects.exclude(logo="").exclude(logo__isnull=True):
            field = account.logo
            storage = field.storage
            old_name = field.name
            try:
                with storage.open(old_name, "rb") as source:
                    if is_fitted(source):
                        skipped += 1
                        continue
                    fitted = fit_logo(source, old_name)
            except Exception as exc:  # a missing or unreadable file is reported, not fatal
                failed += 1
                self.stderr.write(f"{account.client_code}: {old_name}: {exc}")
                continue

            if dry_run:
                refitted += 1
                self.stdout.write(f"{account.client_code}: would refit {old_name}")
                continue

            new_name = storage.save(field.field.generate_filename(account, fitted.name), fitted)
            # A queryset update, not save(): the logo moves no health signal,
            # so there is nothing for the model hooks to do.
            Account.objects.filter(pk=account.pk).update(logo=new_name)
            storage.delete(old_name)
            refitted += 1
            self.stdout.write(f"{account.client_code}: {old_name} -> {new_name}")

        verb = "would refit" if dry_run else "refitted"
        self.stdout.write(f"{verb} {refitted}, already fitted {skipped}, failed {failed}")
