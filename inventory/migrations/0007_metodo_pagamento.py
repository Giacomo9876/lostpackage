from django.db import migrations, models


def receipt_to_payment(apps, schema_editor):
    """Le vendite già registrate: senza scontrino → Yakuza; con scontrino → Altro (il metodo non era indicato).

    I titolari possono correggere il metodo di ogni vendita dal suo dettaglio. I pacchi ancora in
    magazzino non hanno un pagamento: restano al valore di base.
    """
    Package = apps.get_model("inventory", "Package")
    sold = Package.objects.filter(sold_at__isnull=False)
    sold.filter(receipt=False).update(payment="yakuza")
    sold.filter(receipt=True).update(payment="other")


def payment_to_receipt(apps, schema_editor):
    Package = apps.get_model("inventory", "Package")
    Package.objects.filter(payment="yakuza").update(receipt=False)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0006_prezzo_listino"),
    ]

    operations = [
        migrations.AddField(
            model_name="package",
            name="payment",
            field=models.CharField(
                choices=[
                    ("cash", "Contanti"),
                    ("card", "Carta"),
                    ("other", "Altro"),
                    ("yakuza", "Yakuza"),
                ],
                default="cash",
                max_length=10,
                verbose_name="Pagamento",
            ),
        ),
        migrations.RunPython(receipt_to_payment, payment_to_receipt),
        migrations.RemoveField(
            model_name="package",
            name="receipt",
        ),
    ]
