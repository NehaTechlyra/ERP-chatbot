from django.core.management.base import BaseCommand, CommandError

from Items.hsn_sac_loader import (
    DATA_FILE, load_hsn_rows, load_sac_rows, sync_hsn, sync_sac,
)
from Items.models import HSNCode, SACCode


class Command(BaseCommand):
    help = (
        'Load / re-sync HSN (and optionally SAC) codes from Items/data/HSN_SAC.xlsx. '
        'Safe to run repeatedly: existing rows keep their id, missing codes are added, '
        'codes that lost their leading zero are corrected.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(DATA_FILE), help='Path to the xlsx (default: Items/data/HSN_SAC.xlsx)')
        parser.add_argument('--database', default='default', help='Database alias (default: "default")')
        parser.add_argument('--sac', action='store_true', help='Also add missing SAC codes (insert-only)')
        parser.add_argument('--overwrite-descriptions', action='store_true',
                            help='Replace existing HSN descriptions with the text from the file '
                                 '(default: keep existing descriptions)')
        parser.add_argument('--dry-run', action='store_true', help='Show what would change; write nothing')

    def handle(self, *args, **opts):
        db, path, dry = opts['database'], opts['file'], opts['dry_run']
        try:
            rows, skipped = load_hsn_rows(path)
        except (FileNotFoundError, ValueError) as exc:
            raise CommandError(str(exc))

        prefix = '[DRY RUN] ' if dry else ''
        s = sync_hsn(HSNCode, rows, using=db,
                     overwrite_descriptions=opts['overwrite_descriptions'], dry_run=dry)

        self.stdout.write(self.style.SUCCESS(f'{prefix}HSN sync ({path})'))
        self.stdout.write(f"  codes in file (valid)        : {s['in_file']:,}")
        self.stdout.write(f"  new codes added              : {s['created']:,}")
        self.stdout.write(f"  codes fixed (leading zero)   : {s['codes_fixed']:,}")
        self.stdout.write(f"  empty descriptions filled    : {s['descriptions_filled']:,}")
        self.stdout.write(f"  descriptions overwritten     : {s['descriptions_overwritten']:,}")
        self.stdout.write(f"  descriptions differ, kept    : {s['descriptions_differ_kept']:,}")
        self.stdout.write(f"  already up to date           : {s['unchanged']:,}")
        self.stdout.write(f"  in database but not in file  : {s['not_in_file']:,} (left untouched)")
        if skipped:
            self.stdout.write(self.style.WARNING(f'  skipped {len(skipped)} bad row(s) in file:'))
            for excel_row, code, reason in skipped:
                self.stdout.write(f'    row {excel_row}: {code!r} - {reason}')

        if opts['sac']:
            sac_rows, sac_skipped = load_sac_rows(path)
            t = sync_sac(SACCode, sac_rows, using=db, dry_run=dry)
            self.stdout.write(self.style.SUCCESS(f'{prefix}SAC sync (insert-only)'))
            self.stdout.write(f"  codes in file: {t['in_file']:,} | added: {t['created']:,} | already present: {t['already_present']:,}")
            for excel_row, code, reason in sac_skipped:
                self.stdout.write(self.style.WARNING(f'    row {excel_row}: {code!r} - {reason}'))
