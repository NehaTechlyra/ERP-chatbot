"""
Loader for the HSN / SAC master data shipped in ``Items/data/HSN_SAC.xlsx``.

Sheets used
    HSN_MSTR : HSN_CD, HSN_Description   (2 / 4 / 6 / 8 digit codes)
    SAC_MSTR : SAC_CD, SAC_Description   (2 / 4 / 6 digit codes)

Design rules
    * ``Item.hsn_code`` stores the *id* of an HSNCode row, so existing rows are
      never deleted or re-created - they are updated in place.
    * Older imports lost the leading zero of codes ('01011010' -> '1011010').
      Those rows are found by their zero-stripped code and renamed to the
      correct code, keeping their id.
    * Existing descriptions are kept unless ``overwrite_descriptions=True``.
    * Rows are only matched on ``code``; the function is idempotent.

The model class is passed in, so the same code works with the real model
(management command) and with the historical model (data migration).
"""
import re
from pathlib import Path

from django.db import transaction

DATA_FILE = Path(__file__).resolve().parent / 'data' / 'HSN_SAC.xlsx'
HSN_SHEET = 'HSN_MSTR'
SAC_SHEET = 'SAC_MSTR'

HSN_LENGTHS = {2, 4, 6, 8}
SAC_LENGTHS = {2, 4, 6}


# --------------------------------------------------------------------------
# Cleaning helpers
# --------------------------------------------------------------------------
def _code_text(raw):
    """Cell value -> trimmed text (SAC codes arrive as ints, HSN as text)."""
    if raw is None:
        return ''
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    return str(raw).strip()


def clean_description(raw):
    """Remove spreadsheet artefacts and collapse whitespace.

    * ``_x000D_`` / CR / line breaks  -> single space
    * ``~`` (hierarchy separator in the source) -> `` - `` (same style as the
      descriptions already in the database)
    """
    if raw is None:
        return ''
    text = str(raw).replace('_x000D_', ' ').replace('\r', ' ').replace('~', ' - ')
    return re.sub(r'\s+', ' ', text).strip()


def _same_text(a, b):
    return re.sub(r'\s+', ' ', a or '').strip() == re.sub(r'\s+', ' ', b or '').strip()


# --------------------------------------------------------------------------
# Reading the workbook
# --------------------------------------------------------------------------
def _read_sheet(path, sheet_name):
    from openpyxl import load_workbook  # imported lazily; already used by other import views

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f'HSN/SAC data file not found: {path}')
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise ValueError(f'Sheet "{sheet_name}" not found in {path.name} (found: {wb.sheetnames})')
        ws = wb[sheet_name]
        for excel_row, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            code = row[0] if len(row) > 0 else None
            desc = row[1] if len(row) > 1 else None
            if code is None and desc is None:
                continue
            yield excel_row, code, desc
    finally:
        wb.close()


def _load_rows(path, sheet_name, valid_lengths):
    """Return (rows, skipped).  rows = [(code, description)], codes unique."""
    rows, skipped, seen = [], [], set()
    for excel_row, raw_code, raw_desc in _read_sheet(path, sheet_name):
        code = _code_text(raw_code)
        if not code.isdigit() or len(code) not in valid_lengths:
            skipped.append((excel_row, code, 'malformed code'))
            continue
        if code in seen:
            skipped.append((excel_row, code, 'duplicate code in file'))
            continue
        seen.add(code)
        rows.append((code, clean_description(raw_desc)))
    return rows, skipped


def load_hsn_rows(path=DATA_FILE):
    return _load_rows(path, HSN_SHEET, HSN_LENGTHS)


def load_sac_rows(path=DATA_FILE):
    return _load_rows(path, SAC_SHEET, SAC_LENGTHS)


# --------------------------------------------------------------------------
# Writing to the database
# --------------------------------------------------------------------------
def _manager(model, using):
    mgr = model._default_manager
    return mgr.using(using) if using else mgr


def sync_hsn(model, rows, using=None, overwrite_descriptions=False,
             dry_run=False, batch_size=500):
    """Bring the HSNCode table in line with ``rows``.  Returns a stats dict."""
    mgr = _manager(model, using)
    existing = {obj.code: obj for obj in mgr.all()}
    canonical = {code for code, _ in rows}

    stats = {
        'in_file': len(rows), 'created': 0, 'codes_fixed': 0,
        'descriptions_filled': 0, 'descriptions_overwritten': 0,
        'descriptions_differ_kept': 0, 'unchanged': 0, 'not_in_file': 0,
    }
    to_create, to_update = [], {}

    for code, desc in rows:
        obj = existing.get(code)
        changed = False

        # Old import dropped leading zeros: find that row and give it the right code.
        if obj is None and code.startswith('0'):
            stripped = code.lstrip('0')
            candidate = existing.get(stripped)
            if candidate is not None and stripped != code and stripped not in canonical:
                del existing[stripped]
                candidate.code = code
                existing[code] = candidate
                obj = candidate
                changed = True
                stats['codes_fixed'] += 1

        if obj is None:
            to_create.append(model(code=code, description=desc))
            stats['created'] += 1
            continue

        if desc:
            if not (obj.description or '').strip():
                obj.description = desc
                changed = True
                stats['descriptions_filled'] += 1
            elif not _same_text(obj.description, desc):
                if overwrite_descriptions:
                    obj.description = desc
                    changed = True
                    stats['descriptions_overwritten'] += 1
                else:
                    stats['descriptions_differ_kept'] += 1

        if changed:
            to_update[obj.pk] = obj
        else:
            stats['unchanged'] += 1

    stats['not_in_file'] = sum(1 for c in existing if c not in canonical)

    if not dry_run:
        with transaction.atomic(using=using):
            if to_update:
                mgr.bulk_update(list(to_update.values()), ['code', 'description'],
                                batch_size=batch_size)
            if to_create:
                mgr.bulk_create(to_create, batch_size=1000)
    return stats


def sync_sac(model, rows, using=None, dry_run=False):
    """Insert-only: SAC codes missing from the table are added, nothing else changes."""
    mgr = _manager(model, using)
    existing = set(mgr.values_list('code', flat=True))
    new = [model(code=c, description=d) for c, d in rows if c not in existing]
    if new and not dry_run:
        with transaction.atomic(using=using):
            mgr.bulk_create(new, batch_size=1000)
    return {'in_file': len(rows), 'created': len(new), 'already_present': len(rows) - len(new)}
