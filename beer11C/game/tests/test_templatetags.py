"""
Tests for templatetags/game_extras.py.
"""
from django.test import SimpleTestCase
from game.templatetags.game_extras import (
    get_item, currency, role_display, role_emoji, phase_display,
)


class GetItemFilterTest(SimpleTestCase):
    def test_existing_key(self):
        self.assertEqual(get_item({'a': 1}, 'a'), 1)

    def test_missing_key_returns_none(self):
        self.assertIsNone(get_item({'a': 1}, 'z'))

    def test_none_dict_returns_none(self):
        self.assertIsNone(get_item(None, 'a'))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(get_item({}, 'a'))


class CurrencyFilterTest(SimpleTestCase):
    def test_integer(self):
        self.assertEqual(currency(10), '$10.00')

    def test_float(self):
        self.assertEqual(currency(12.5), '$12.50')

    def test_zero(self):
        self.assertEqual(currency(0), '$0.00')

    def test_large(self):
        self.assertEqual(currency(1000), '$1,000.00')

    def test_non_numeric_passthrough(self):
        result = currency('oops')
        self.assertEqual(result, 'oops')

    def test_none_passthrough(self):
        result = currency(None)
        self.assertIsNone(result)


class RoleDisplayFilterTest(SimpleTestCase):
    def test_retailer(self):
        self.assertEqual(role_display('retailer'), 'Retailer')

    def test_wholesaler(self):
        self.assertEqual(role_display('wholesaler'), 'Wholesaler')

    def test_distributor(self):
        self.assertEqual(role_display('distributor'), 'Distributor')

    def test_factory(self):
        self.assertEqual(role_display('factory'), 'Factory')

    def test_customer(self):
        self.assertEqual(role_display('customer'), 'Customer')

    def test_unknown_role_title_cased(self):
        self.assertEqual(role_display('unknown_role'), 'Unknown_Role')

    def test_none_returns_empty(self):
        self.assertEqual(role_display(None), '')


class RoleEmojiFilterTest(SimpleTestCase):
    def test_retailer_emoji(self):
        self.assertEqual(role_emoji('retailer'), '🛒')

    def test_customer_emoji(self):
        self.assertEqual(role_emoji('customer'), '👤')

    def test_unknown_emoji(self):
        self.assertEqual(role_emoji('unknown'), '❓')


class PhaseDisplayFilterTest(SimpleTestCase):
    def test_idle(self):
        self.assertEqual(phase_display('idle'), 'Waiting')

    def test_receive(self):
        self.assertEqual(phase_display('receive'), 'Receive')

    def test_ship(self):
        self.assertEqual(phase_display('ship'), 'Ship')

    def test_order(self):
        self.assertEqual(phase_display('order'), 'Order')

    def test_done(self):
        self.assertEqual(phase_display('done'), 'Done')

    def test_unknown_title_cased(self):
        self.assertEqual(phase_display('custom'), 'Custom')

    def test_none_returns_empty(self):
        self.assertEqual(phase_display(None), '')
