"""
Tests for game models — GameSession, Player, PlayerSession, pipeline models.
"""
from django.test import TestCase
from django.contrib.auth.models import User
from game.models import (
    GameSession, Player, PlayerSession, WeeklyState,
    PipelineOrder, PipelineShipment, CustomerDemand, LobbyMessage,
)


def _make_session(name="Test Game", max_weeks=20, status=GameSession.STATUS_LOBBY, user=None):
    return GameSession.objects.create(
        name=name, max_weeks=max_weeks, status=status, created_by=user,
    )


def _make_player(session, role='retailer', inventory=12, backlog=0):
    return Player.objects.create(
        session=session, name=role.title(), role=role,
        inventory=inventory, backlog=backlog,
    )


def _make_player_session(session, role='retailer', user=None):
    import secrets
    return PlayerSession.objects.create(
        game_session=session, role=role, user=user,
        token=secrets.token_urlsafe(32),
    )


class GameSessionModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('alice', password='pass12345')
        self.session = _make_session(user=self.user)

    def test_str(self):
        self.assertIn('Test Game', str(self.session))

    def test_is_finished_false(self):
        self.assertFalse(self.session.is_finished)

    def test_is_finished_true(self):
        self.session.current_week = self.session.max_weeks
        self.assertTrue(self.session.is_finished)

    def test_channel_group_name(self):
        self.assertEqual(self.session.channel_group_name, f"game_{self.session.id}")

    def test_submitted_role_list_empty(self):
        self.assertEqual(self.session.submitted_role_list, [])

    def test_mark_submitted(self):
        self.session.mark_submitted('retailer')
        self.assertIn('retailer', self.session.submitted_role_list)

    def test_mark_submitted_idempotent(self):
        self.session.mark_submitted('retailer')
        self.session.mark_submitted('retailer')
        self.assertEqual(self.session.submitted_role_list.count('retailer'), 1)

    def test_reset_submissions(self):
        self.session.mark_submitted('retailer')
        self.session.reset_submissions()
        self.assertEqual(self.session.submitted_role_list, [])

    def test_mark_ready(self):
        self.session.mark_ready('wholesaler')
        self.assertIn('wholesaler', self.session.ready_role_list)

    def test_all_submitted_no_player_sessions(self):
        # No player_sessions → all_submitted returns True
        self.assertTrue(self.session.all_submitted())

    def test_all_submitted_with_player_sessions(self):
        ps = _make_player_session(self.session, 'retailer')
        self.assertFalse(self.session.all_submitted())
        self.session.mark_submitted('retailer')
        self.assertTrue(self.session.all_submitted())

    def test_all_ready_with_player_sessions(self):
        ps = _make_player_session(self.session, 'retailer')
        self.assertFalse(self.session.all_ready())
        self.session.mark_ready('retailer')
        self.assertTrue(self.session.all_ready())

    def test_status_constants(self):
        self.assertEqual(GameSession.STATUS_LOBBY, 'lobby')
        self.assertEqual(GameSession.STATUS_PLAYING, 'playing')
        self.assertEqual(GameSession.STATUS_FINISHED, 'finished')


class PlayerModelTest(TestCase):
    def setUp(self):
        self.session = _make_session()
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            _make_player(self.session, role)

    def test_str(self):
        p = Player.objects.get(session=self.session, role='retailer')
        self.assertIn('retailer', str(p))

    def test_get_upstream(self):
        retailer = Player.objects.get(session=self.session, role='retailer')
        wholesaler = Player.objects.get(session=self.session, role='wholesaler')
        self.assertEqual(retailer.get_upstream(), wholesaler)

    def test_get_upstream_factory_is_none(self):
        factory = Player.objects.get(session=self.session, role='factory')
        self.assertIsNone(factory.get_upstream())

    def test_get_downstream(self):
        wholesaler = Player.objects.get(session=self.session, role='wholesaler')
        retailer   = Player.objects.get(session=self.session, role='retailer')
        self.assertEqual(wholesaler.get_downstream(), retailer)

    def test_get_downstream_retailer_is_none(self):
        retailer = Player.objects.get(session=self.session, role='retailer')
        self.assertIsNone(retailer.get_downstream())

    def test_initial_inventory(self):
        p = Player.objects.get(session=self.session, role='retailer')
        self.assertEqual(p.inventory, 12)

    def test_total_cost_default(self):
        p = Player.objects.get(session=self.session, role='retailer')
        self.assertEqual(p.total_cost, 0.0)


class PlayerSessionModelTest(TestCase):
    def setUp(self):
        self.session = _make_session()
        self.ps = _make_player_session(self.session, 'retailer')

    def test_str(self):
        self.assertIn('retailer', str(self.ps))

    def test_token_is_set(self):
        self.assertTrue(len(self.ps.token) > 8)

    def test_default_phase(self):
        self.assertEqual(self.ps.turn_phase, PlayerSession.PHASE_IDLE)

    def test_phase_constants(self):
        self.assertEqual(PlayerSession.PHASE_IDLE,    'idle')
        self.assertEqual(PlayerSession.PHASE_RECEIVE, 'receive')
        self.assertEqual(PlayerSession.PHASE_SHIP,    'ship')
        self.assertEqual(PlayerSession.PHASE_ORDER,   'order')
        self.assertEqual(PlayerSession.PHASE_DONE,    'done')

    def test_unique_token(self):
        ps2 = _make_player_session(self.session, 'wholesaler')
        self.assertNotEqual(self.ps.token, ps2.token)


class PipelineModelTest(TestCase):
    def setUp(self):
        self.session = _make_session()
        self.retailer = _make_player(self.session, 'retailer')
        self.wholesaler = _make_player(self.session, 'wholesaler')

    def test_pipeline_order_str(self):
        o = PipelineOrder.objects.create(
            sender=self.retailer, quantity=4,
            placed_on_week=0, arrives_on_week=2,
        )
        self.assertIn('4', str(o))
        self.assertIn('2', str(o))

    def test_pipeline_shipment_str(self):
        s = PipelineShipment.objects.create(
            receiver=self.retailer, quantity=4,
            shipped_on_week=0, arrives_on_week=2,
        )
        self.assertIn('4', str(s))

    def test_customer_demand_str(self):
        d = CustomerDemand.objects.create(session=self.session, week=1, quantity=4)
        self.assertIn('4', str(d))

    def test_lobby_message_str(self):
        msg = LobbyMessage.objects.create(
            game_session=self.session,
            author_name='Alice', author_role='retailer', body='Hello!'
        )
        self.assertIn('Alice', str(msg))

    def test_weekly_state_str(self):
        player = self.retailer
        ws = WeeklyState.objects.create(
            player=player, week=1, inventory=10, backlog=0,
        )
        self.assertIn('1', str(ws))
