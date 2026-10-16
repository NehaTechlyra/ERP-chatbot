from django.db import migrations

from Items.hsn_sac_loader import DATA_FILE, load_hsn_rows, sync_hsn


def sync_hsn_codes(apps, schema_editor):
    """
    Bring items_hsncode in line with Items/data/HSN_SAC.xlsx:
      * fixes codes that lost their leading zero (row id is kept, so Items still point to them)
      * adds the missing 2 / 4 / 6 / 8 digit codes
      * keeps existing descriptions
    Idempotent. To re-run later or to overwrite descriptions use:
        python manage.py import_hsn_sac [--overwrite-descriptions] [--dry-run]
    """
    HSNCode = apps.get_model('Items', 'HSNCode')
    db = schema_editor.connection.alias

    rows, skipped = load_hsn_rows(DATA_FILE)
    stats = sync_hsn(HSNCode, rows, using=db)

    print(f"\n[HSN sync] db={db}  file rows={stats['in_file']:,}  added={stats['created']:,}  "
          f"codes fixed={stats['codes_fixed']:,}  already ok={stats['unchanged']:,}  "
          f"skipped bad rows={len(skipped)}")


class Migration(migrations.Migration):

    dependencies = [
        ('Items', '0008_item_subcategory'),
    ]

    operations = [
        # Not reversible on purpose: removing the codes again could orphan Items.
        migrations.RunPython(sync_hsn_codes, migrations.RunPython.noop),
    ]
