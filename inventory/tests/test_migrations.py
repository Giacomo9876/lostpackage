"""La migrazione 0007 deve conservare le vendite già registrate: nessun dato perso né cambiato di significato."""
from decimal import Decimal

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("inventory", "0006_prezzo_listino")]
AFTER = [("inventory", "0007_metodo_pagamento")]


class PaymentMigrationTests(TransactionTestCase):
    # Il test riporta il database indietro e poi in avanti: lo stato iniziale (i tre centri) va ripristinato.
    serialized_rollback = True

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        call_command("migrate", verbosity=0)  # lascia il database all'ultima migrazione
        super().tearDown()

    def make(self, apps, **fields):
        Store = apps.get_model("inventory", "Store")
        Package = apps.get_model("inventory", "Package")
        store = Store.objects.get(name="Milanofiori")
        return Package.objects.create(store=store, weight_kg=Decimal("1.000"), **fields)

    def test_old_sales_keep_their_meaning(self):
        old = self.migrate(BEFORE)
        with_receipt = self.make(old, sold_price=Decimal("25.00"), sold_at="2026-10-01T10:00:00Z", receipt=True)
        without_receipt = self.make(old, sold_price=Decimal("25.00"), sold_at="2026-10-01T10:00:00Z", receipt=False)
        in_stock = self.make(old, receipt=False)  # ancora in magazzino: il flag non conta

        new = self.migrate(AFTER)
        Package = new.get_model("inventory", "Package")
        payments = dict(Package.objects.values_list("pk", "payment"))
        self.assertEqual(payments[with_receipt.pk], "other")  # il metodo non era indicato
        self.assertEqual(payments[without_receipt.pk], "yakuza")  # «senza scontrino» è Yakuza
        self.assertEqual(payments[in_stock.pk], "cash")  # valore di base, nessun pagamento ancora
        self.assertNotIn("receipt", [f.name for f in Package._meta.get_fields()])

    def test_other_fields_of_a_sale_are_untouched(self):
        old = self.migrate(BEFORE)
        sale = self.make(
            old, sold_price=Decimal("48.00"), list_price=Decimal("50.00"), sold_at="2026-10-01T10:00:00Z", premium=True, notes="x"
        )
        new = self.migrate(AFTER)
        package = new.get_model("inventory", "Package").objects.get(pk=sale.pk)
        self.assertEqual(
            (package.sold_price, package.list_price, package.premium, package.notes, package.weight_kg),
            (Decimal("48.00"), Decimal("50.00"), True, "x", Decimal("1.000")),
        )

    def test_it_can_be_undone(self):
        self.migrate(BEFORE)
        new = self.migrate(AFTER)
        Package = new.get_model("inventory", "Package")
        store = new.get_model("inventory", "Store").objects.get(name="Milanofiori")
        yakuza = Package.objects.create(store=store, weight_kg=Decimal("1"), sold_price=Decimal("25"), sold_at="2026-10-01T10:00:00Z", payment="yakuza")
        card = Package.objects.create(store=store, weight_kg=Decimal("1"), sold_price=Decimal("25"), sold_at="2026-10-01T10:00:00Z", payment="card")

        back = self.migrate(BEFORE)
        Old = back.get_model("inventory", "Package")
        self.assertFalse(Old.objects.get(pk=yakuza.pk).receipt)
        self.assertTrue(Old.objects.get(pk=card.pk).receipt)


class ReceiptCloseMigrationTests(TransactionTestCase):
    """La 0008 aggiunge Yakuza, foto e importo letto alle chiusure: quelle già fatte restano com'erano."""

    serialized_rollback = True
    PAYMENT = [("inventory", "0007_metodo_pagamento")]
    SCONTRINI = [("inventory", "0008_scontrini_chiusura")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    def test_old_closes_keep_their_amount_and_start_without_yakuza_photo_or_reading(self):
        old = self.migrate(self.PAYMENT)
        Store = old.get_model("inventory", "Store")
        DayClose = old.get_model("inventory", "DayClose")
        close = DayClose.objects.create(store=Store.objects.get(name="Globo"), date="2026-10-01", receipts_total=Decimal("321.45"))

        new = self.migrate(self.SCONTRINI)
        migrated = new.get_model("inventory", "DayClose").objects.get(pk=close.pk)
        self.assertEqual(migrated.receipts_total, Decimal("321.45"))
        self.assertEqual(migrated.yakuza_cash, Decimal("0.00"))
        self.assertIsNone(migrated.detected_total)
        self.assertFalse(migrated.photo)

    def test_it_can_be_undone(self):
        self.migrate(self.PAYMENT)
        new = self.migrate(self.SCONTRINI)
        Store = new.get_model("inventory", "Store")
        DayClose = new.get_model("inventory", "DayClose")
        close = DayClose.objects.create(
            store=Store.objects.get(name="Globo"), date="2026-10-02", receipts_total=Decimal("10.00"), yakuza_cash=Decimal("5.00")
        )

        back = self.migrate(self.PAYMENT)
        Old = back.get_model("inventory", "DayClose")
        self.assertEqual(Old.objects.get(pk=close.pk).receipts_total, Decimal("10.00"))
        self.assertNotIn("yakuza_cash", [f.name for f in Old._meta.get_fields()])


class FranchiseMigrationTests(TransactionTestCase):
    """La 0009 aggiunge il franchising: i ruoli e le chiusure che ci sono già non cambiano."""

    serialized_rollback = True
    BEFORE = [("inventory", "0008_scontrini_chiusura")]
    AFTER = [("inventory", "0009_franchising")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    def test_existing_roles_and_closes_are_kept_and_it_can_be_undone(self):
        old = self.migrate(self.BEFORE)
        User = old.get_model("auth", "User")
        Profile = old.get_model("inventory", "Profile")
        Store = old.get_model("inventory", "Store")
        DayClose = old.get_model("inventory", "DayClose")
        globo = Store.objects.get(name="Globo")
        owner = Profile.objects.create(user=User.objects.create(username="t"), role="owner")
        manager = Profile.objects.create(user=User.objects.create(username="m"), role="manager", store=globo)
        close = DayClose.objects.create(store=globo, date="2026-10-01", receipts_total=Decimal("10.00"), yakuza_cash=Decimal("2.00"))

        new = self.migrate(self.AFTER)
        NewProfile = new.get_model("inventory", "Profile")
        self.assertEqual(NewProfile.objects.get(pk=owner.pk).role, "owner")
        migrated = NewProfile.objects.get(pk=manager.pk)
        self.assertEqual((migrated.role, migrated.store_id, migrated.franchise_id), ("manager", globo.pk, None))
        self.assertEqual(new.get_model("inventory", "DayClose").objects.get(pk=close.pk).yakuza_cash, Decimal("2.00"))

        back = self.migrate(self.BEFORE)
        self.assertEqual(back.get_model("inventory", "Profile").objects.count(), 2)
