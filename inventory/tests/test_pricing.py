from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from ..models import Package, ShopSettings, Store


def price(weight, premium=False, **rules):
    shop = ShopSettings(**rules)
    return shop.price_for(Decimal(str(weight)), premium)


class PriceRulesTests(SimpleTestCase):
    """Regole: fino a 200 g ogni pacco costa 5 €; oltre, 25 € al kg (30 € al kg se premium)."""

    def test_small_packages_cost_a_flat_5_euro(self):
        for weight in ("0.001", "0.050", "0.100", "0.199", "0.200"):
            with self.subTest(weight=weight):
                self.assertEqual(price(weight), Decimal("5.00"))

    def test_premium_makes_no_difference_for_small_packages(self):
        for weight in ("0.050", "0.200"):
            with self.subTest(weight=weight):
                self.assertEqual(price(weight, premium=True), Decimal("5.00"))

    def test_just_over_the_limit_is_charged_by_weight(self):
        self.assertEqual(price("0.201"), Decimal("5.03"))  # 0,201 × 25 = 5,025 → arrotonda su
        self.assertEqual(price("0.201", premium=True), Decimal("6.03"))  # 0,201 × 30 = 6,03

    def test_normal_packages_cost_25_euro_per_kg(self):
        self.assertEqual(price("1"), Decimal("25.00"))
        self.assertEqual(price("2.350"), Decimal("58.75"))
        self.assertEqual(price("1.333"), Decimal("33.33"))

    def test_premium_packages_cost_30_euro_per_kg(self):
        self.assertEqual(price("1", premium=True), Decimal("30.00"))
        self.assertEqual(price("2.350", premium=True), Decimal("70.50"))
        self.assertEqual(price("0.500", premium=True), Decimal("15.00"))

    def test_premium_is_50_cents_more_per_hectogram(self):
        # «50 centesimi in più all'etto»: 25 € al kg → 30 € al kg.
        self.assertEqual(price("1", premium=True) - price("1"), Decimal("5.00"))

    def test_rounding_is_half_up_to_the_cent(self):
        self.assertEqual(price("0.205"), Decimal("5.13"))  # 5,125
        self.assertEqual(price("0.203"), Decimal("5.08"))  # 5,075

    def test_rules_are_configurable(self):
        rules = {"price_per_kg": Decimal("20"), "premium_price_per_kg": Decimal("26"), "small_parcel_max_g": 300, "small_parcel_price": Decimal("7.5")}
        self.assertEqual(price("0.300", **rules), Decimal("7.50"))
        self.assertEqual(price("0.301", **rules), Decimal("6.02"))
        self.assertEqual(price("1", premium=True, **rules), Decimal("26.00"))

    def test_price_with_uses_the_package_weight_and_flag(self):
        shop = ShopSettings()
        self.assertEqual(Package(weight_kg=Decimal("2"), premium=True).price_with(shop), Decimal("60.00"))
        self.assertEqual(Package(weight_kg=Decimal("0.1"), premium=True).price_with(shop), Decimal("5.00"))


class SeededStoresTests(TestCase):
    def test_the_three_centers_exist_after_migrating(self):
        labels = sorted(s.label for s in Store.objects.all())
        self.assertEqual(labels, ["Globo · Busnago", "Milanofiori · Assago", "Tor Vergata · Roma"])
