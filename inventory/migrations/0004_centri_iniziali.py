"""Crea i tre centri commerciali e assegna al primo tutto ciò che esisteva prima dei centri.

È una migrazione di soli dati, separata da quella che rende obbligatorio il centro:
su PostgreSQL mescolare dati e modifiche dello schema nella stessa transazione può fallire.
"""
from django.db import migrations

CENTRI = [
    ("Milanofiori", "Assago"),
    ("Globo", "Busnago"),
    ("Tor Vergata", "Roma"),
]


def crea_centri_e_assegna(apps, schema_editor):
    Store = apps.get_model("inventory", "Store")
    Batch = apps.get_model("inventory", "Batch")
    Package = apps.get_model("inventory", "Package")

    centri = [Store.objects.get_or_create(name=name, defaults={"city": city})[0] for name, city in CENTRI]
    # I dati inseriti prima dell'arrivo dei centri appartengono al primo (Milanofiori).
    # Si possono spostare dopo dal pannello di amministrazione.
    Batch.objects.filter(store__isnull=True).update(store=centri[0])
    Package.objects.filter(store__isnull=True).update(store=centri[0])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0003_centri_ruoli_chiusure"),
    ]

    operations = [
        migrations.RunPython(crea_centri_e_assegna, migrations.RunPython.noop),
    ]
