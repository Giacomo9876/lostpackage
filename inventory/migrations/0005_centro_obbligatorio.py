import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0004_centri_iniziali"),
    ]

    operations = [
        migrations.AlterField(
            model_name="batch",
            name="store",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="batches",
                to="inventory.store",
                verbose_name="Centro",
            ),
        ),
        migrations.AlterField(
            model_name="package",
            name="store",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="packages",
                to="inventory.store",
                verbose_name="Centro",
            ),
        ),
        migrations.AddConstraint(
            model_name="batch",
            constraint=models.UniqueConstraint(fields=("store", "name"), name="unique_batch_name_per_store"),
        ),
    ]
