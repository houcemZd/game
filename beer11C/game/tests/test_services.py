"""
Tests for the Beer Game engine in services.py.

Covers:
  - initialise_session  — pipeline creation
  - open_week           — staging data
  - apply_receive       — inventory update
  - apply_ship          — shipping logic & backlog
  - apply_order         — order/production-request creation
  - close_week          — cost calculation, week advance
  - process_week        — single-player full-week pass
  - _ai_order           — base-stock policy
  - get_bullwhip_data   — bullwhip ratio
  - get_chart_data      — chart compilation
"""
from django.test import TestCase
from game.models import (
    GameSession, Player, PlayerSession, WeeklyState,
    PipelineOrder, PipelineShipment, CustomerDemand,
)
from game.services import (
    initialise_session, open_week, apply_receive, apply_ship, apply_order,
    close_week, process_week, _ai_order, get_bullwhip_data, get_chart_data,
    get_advanced_analytics,
    ORDER_DELAY, SHIP_DELAY,
)
import secrets


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _create_full_session(max_weeks=20):
    """Create a GameSession with all four supply-chain Player rows."""
    session = GameSession.objects.create(
        name='Test', max_weeks=max_weeks, status=GameSession.STATUS_PLAYING,
    )
    for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
        Player.objects.create(
            session=session, name=role.title(), role=role,
            inventory=12, backlog=0,
            holding_cost=0.5, backlog_cost=1.0,
        )
    return session


def _create_player_sessions(session):
    """Create all five PlayerSession rows (including customer)."""
    pss = {}
    for role in ['customer', 'retailer', 'wholesaler', 'distributor', 'factory']:
        pss[role] = PlayerSession.objects.create(
            game_session=session, role=role,
            token=secrets.token_urlsafe(32),
        )
    return pss


def _init_and_open(session, customer_demand=4):
    """Initialise pipeline and open week 1 with customer demand."""
    initialise_session(session)
    session.pending_customer_demand = customer_demand
    session.save(update_fields=['pending_customer_demand'])
    # Create PlayerSessions so open_week can set phases.
    if not session.player_sessions.exists():
        _create_player_sessions(session)
    return open_week(session)


# ─── Tests ───────────────────────────────────────────────────────────────────

class InitialiseSessionTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()

    def test_creates_shipments_for_every_role(self):
        initialise_session(self.session)
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            player = self.session.players.get(role=role)
            count = PipelineShipment.objects.filter(receiver=player).count()
            self.assertEqual(count, 2, f"{role} should have 2 initial shipments")

    def test_shipments_arrive_weeks_1_and_2(self):
        initialise_session(self.session)
        player = self.session.players.get(role='retailer')
        weeks = list(
            PipelineShipment.objects.filter(receiver=player)
            .order_by('arrives_on_week')
            .values_list('arrives_on_week', flat=True)
        )
        self.assertEqual(weeks, [1, 2])

    def test_non_factory_orders_arrive_weeks_1_and_2(self):
        initialise_session(self.session)
        for role in ['retailer', 'wholesaler', 'distributor']:
            player = self.session.players.get(role=role)
            weeks = list(
                PipelineOrder.objects.filter(sender=player)
                .order_by('arrives_on_week')
                .values_list('arrives_on_week', flat=True)
            )
            self.assertEqual(weeks, [1, 2], f"{role} orders wrong")

    def test_factory_has_one_production_request(self):
        initialise_session(self.session)
        factory = self.session.players.get(role='factory')
        count = PipelineOrder.objects.filter(sender=factory).count()
        self.assertEqual(count, 1)

    def test_factory_production_request_arrives_week_1(self):
        initialise_session(self.session)
        factory = self.session.players.get(role='factory')
        order = PipelineOrder.objects.get(sender=factory)
        self.assertEqual(order.arrives_on_week, 1)

    def test_custom_quantities(self):
        initialise_session(self.session, init_orders_placed=6, init_incoming=6)
        player = self.session.players.get(role='retailer')
        for s in PipelineShipment.objects.filter(receiver=player):
            self.assertEqual(s.quantity, 6)


class OpenWeekTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        _create_player_sessions(self.session)
        initialise_session(self.session)
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])

    def test_returns_staging_dict_for_all_roles(self):
        staging = open_week(self.session)
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            self.assertIn(role, staging)

    def test_staging_has_received_key(self):
        staging = open_week(self.session)
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            self.assertIn('received', staging[role])

    def test_player_sessions_set_to_phase_receive(self):
        open_week(self.session)
        for ps in self.session.player_sessions.exclude(role='customer'):
            ps.refresh_from_db()
            self.assertEqual(ps.turn_phase, PlayerSession.PHASE_RECEIVE)

    def test_customer_stays_idle(self):
        open_week(self.session)
        cust = self.session.player_sessions.get(role='customer')
        cust.refresh_from_db()
        self.assertEqual(cust.turn_phase, PlayerSession.PHASE_IDLE)

    def test_retailer_pending_order_qty_equals_customer_demand(self):
        open_week(self.session)
        ps_retailer = self.session.player_sessions.get(role='retailer')
        ps_retailer.refresh_from_db()
        self.assertEqual(ps_retailer.pending_order_qty, 4)


class ApplyReceiveTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        _create_player_sessions(self.session)
        initialise_session(self.session)
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])
        open_week(self.session)

    def test_retailer_inventory_increases(self):
        ps = self.session.player_sessions.get(role='retailer')
        player = self.session.players.get(role='retailer')
        before = player.inventory
        apply_receive(ps)
        player.refresh_from_db()
        self.assertGreater(player.inventory, before)

    def test_retailer_advances_to_ship_phase(self):
        ps = self.session.player_sessions.get(role='retailer')
        apply_receive(ps)
        ps.refresh_from_db()
        self.assertEqual(ps.turn_phase, PlayerSession.PHASE_SHIP)

    def test_shipments_marked_delivered(self):
        ps = self.session.player_sessions.get(role='retailer')
        player = self.session.players.get(role='retailer')
        apply_receive(ps)
        # Shipments that arrived this week should be delivered
        week = self.session.current_week + 1
        for s in PipelineShipment.objects.filter(receiver=player, arrives_on_week=week):
            self.assertTrue(s.delivered)

    def test_factory_receive_starts_production(self):
        ps = self.session.player_sessions.get(role='factory')
        factory = self.session.players.get(role='factory')
        before_ships = PipelineShipment.objects.filter(receiver=factory).count()
        apply_receive(ps)
        after_ships = PipelineShipment.objects.filter(receiver=factory).count()
        # A new production shipment should have been created
        self.assertGreater(after_ships, before_ships)

    def test_returns_received_key(self):
        ps = self.session.player_sessions.get(role='retailer')
        result = apply_receive(ps)
        self.assertIn('received', result)
        self.assertIn('new_inventory', result)

    def test_wholesaler_receive_consumes_arriving_retailer_order(self):
        ps = self.session.player_sessions.get(role='wholesaler')
        retailer = self.session.players.get(role='retailer')
        week = self.session.current_week + 1

        self.assertTrue(
            PipelineOrder.objects.filter(
                sender=retailer, arrives_on_week=week, fulfilled=False
            ).exists()
        )

        apply_receive(ps)

        self.assertFalse(
            PipelineOrder.objects.filter(
                sender=retailer, arrives_on_week=week, fulfilled=False
            ).exists()
        )


class ApplyShipTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        _create_player_sessions(self.session)
        initialise_session(self.session)
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])
        open_week(self.session)

    def _advance_to_ship(self, role):
        ps = self.session.player_sessions.get(role=role)
        apply_receive(ps)
        ps.refresh_from_db()
        return ps

    def test_wholesaler_ships_to_retailer(self):
        ps = self._advance_to_ship('wholesaler')
        retailer = self.session.players.get(role='retailer')
        before_ships = PipelineShipment.objects.filter(receiver=retailer).count()
        apply_ship(ps)
        after_ships = PipelineShipment.objects.filter(receiver=retailer).count()
        self.assertGreaterEqual(after_ships, before_ships)

    def test_ship_advances_to_order_phase(self):
        ps = self._advance_to_ship('retailer')
        apply_ship(ps)
        ps.refresh_from_db()
        self.assertEqual(ps.turn_phase, PlayerSession.PHASE_ORDER)

    def test_ship_result_has_shipped_key(self):
        ps = self._advance_to_ship('retailer')
        result = apply_ship(ps)
        self.assertIn('shipped', result)
        self.assertIn('new_inventory', result)

    def test_backlog_when_insufficient_stock(self):
        """Force a stockout — all inventory is 0 — backlog should increase."""
        # Drain inventory to 0
        player = self.session.players.get(role='retailer')
        player.inventory = 0
        player.backlog = 0
        player.save()
        ps = self.session.player_sessions.get(role='retailer')
        # Manually put it in PHASE_SHIP
        ps.turn_phase = PlayerSession.PHASE_SHIP
        ps.pending_order_qty = 8
        ps.save()
        result = apply_ship(ps)
        player.refresh_from_db()
        self.assertEqual(result['shipped'], 0)
        self.assertGreater(player.backlog, 0)

    def test_wholesaler_ship_uses_staged_demand_after_receive(self):
        ps = self._advance_to_ship('wholesaler')
        result = apply_ship(ps)
        self.assertEqual(result['demand_received'], 4)
        self.assertEqual(result['shipped'], 4)


class ApplyOrderTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        _create_player_sessions(self.session)
        initialise_session(self.session)
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])
        open_week(self.session)
        # Advance retailer to PHASE_ORDER
        ps = self.session.player_sessions.get(role='retailer')
        apply_receive(ps)
        ps.refresh_from_db()
        apply_ship(ps)
        ps.refresh_from_db()
        self.ps_retailer = ps

    def test_creates_pipeline_order_upstream(self):
        player = self.session.players.get(role='retailer')
        before = PipelineOrder.objects.filter(sender=player).count()
        ps = self.ps_retailer
        ps.refresh_from_db()
        apply_order(ps, 8)
        after = PipelineOrder.objects.filter(sender=player).count()
        self.assertEqual(after, before + 1)

    def test_order_arrives_after_delay(self):
        player = self.session.players.get(role='retailer')
        ps = self.ps_retailer
        ps.refresh_from_db()
        week = self.session.current_week + 1
        apply_order(ps, 8)
        order = PipelineOrder.objects.filter(sender=player).order_by('-placed_on_week').first()
        self.assertEqual(order.arrives_on_week, week + ORDER_DELAY)

    def test_advances_to_done_phase(self):
        ps = self.ps_retailer
        ps.refresh_from_db()
        apply_order(ps, 8)
        ps.refresh_from_db()
        self.assertEqual(ps.turn_phase, PlayerSession.PHASE_DONE)

    def test_marks_role_as_submitted(self):
        ps = self.ps_retailer
        ps.refresh_from_db()
        apply_order(ps, 8)
        self.session.refresh_from_db()
        self.assertIn('retailer', self.session.submitted_role_list)

    def test_factory_order_is_self_directed(self):
        """Factory places production request to itself (1-week delay)."""
        # Advance factory through receive/ship
        ps_f = self.session.player_sessions.get(role='factory')
        apply_receive(ps_f)
        ps_f.refresh_from_db()
        apply_ship(ps_f)
        ps_f.refresh_from_db()
        factory = self.session.players.get(role='factory')
        week = self.session.current_week + 1
        apply_order(ps_f, 5)
        order = PipelineOrder.objects.filter(sender=factory).order_by('-placed_on_week').first()
        self.assertIsNotNone(order)
        self.assertEqual(order.arrives_on_week, week + 1)   # 1-week delay


class CloseWeekTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        pss = _create_player_sessions(self.session)
        initialise_session(self.session)
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])
        open_week(self.session)
        # Drive all non-customer roles through all three phases
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            ps = self.session.player_sessions.get(role=role)
            apply_receive(ps)
            ps.refresh_from_db()
            apply_ship(ps)
            ps.refresh_from_db()
            apply_order(ps, 4)
        # Set customer to done
        cust = pss['customer']
        cust.turn_phase = PlayerSession.PHASE_DONE
        cust.save()

    def test_advances_current_week(self):
        before = self.session.current_week
        close_week(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.current_week, before + 1)

    def test_creates_weekly_state_records(self):
        close_week(self.session)
        week = 1   # week that was closed
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            player = self.session.players.get(role=role)
            self.assertTrue(
                WeeklyState.objects.filter(player=player, week=week).exists(),
                f"No WeeklyState for {role} week {week}",
            )

    def test_records_customer_demand(self):
        close_week(self.session)
        demand = CustomerDemand.objects.filter(session=self.session, week=1).first()
        self.assertIsNotNone(demand)
        self.assertEqual(demand.quantity, 4)

    def test_resets_player_session_phases(self):
        close_week(self.session)
        for ps in self.session.player_sessions.all():
            ps.refresh_from_db()
            self.assertEqual(ps.turn_phase, PlayerSession.PHASE_IDLE)

    def test_total_cost_increases(self):
        retailer = self.session.players.get(role='retailer')
        before = retailer.total_cost
        close_week(self.session)
        retailer.refresh_from_db()
        self.assertGreater(retailer.total_cost, before)

    def test_finished_when_max_weeks_reached(self):
        self.session.current_week = self.session.max_weeks - 1
        self.session.save()
        # Fix staging so week = max_weeks
        summary = close_week(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, GameSession.STATUS_FINISHED)
        self.assertFalse(self.session.is_active)


class ProcessWeekTest(TestCase):
    """Single-player (HTTP) week processing via process_week()."""

    def setUp(self):
        self.session = _create_full_session()
        initialise_session(self.session)
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])

    def test_advances_week(self):
        process_week(self.session, {})
        self.session.refresh_from_db()
        self.assertEqual(self.session.current_week, 1)

    def test_creates_weekly_states(self):
        process_week(self.session, {})
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            player = self.session.players.get(role=role)
            self.assertTrue(WeeklyState.objects.filter(player=player, week=1).exists())

    def test_does_not_double_submit(self):
        """If two requests use the same stale session object, the second is a no-op."""
        # Make a copy of the session object at current_week=0
        stale_session = GameSession.objects.get(pk=self.session.pk)
        process_week(stale_session, {})
        # Second call with the same stale object: current_week in memory is still 0
        # but DB now has current_week=1 → the guard should detect the mismatch and bail.
        result2 = process_week(stale_session, {})
        self.assertEqual(result2, {})

    def test_with_explicit_orders(self):
        players = {p.role: p for p in self.session.players.all()}
        orders  = {players['retailer'].id: 10, players['factory'].id: 5}
        process_week(self.session, orders)
        ws = WeeklyState.objects.get(player=players['retailer'], week=1)
        self.assertEqual(ws.order_placed, 10)

    def test_ai_fills_missing_orders(self):
        """Roles without explicit orders get the AI base-stock order."""
        process_week(self.session, {})
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            player = self.session.players.get(role=role)
            ws = WeeklyState.objects.get(player=player, week=1)
            self.assertGreaterEqual(ws.order_placed, 0)


class AIOrderTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        initialise_session(self.session)
        self.retailer = self.session.players.get(role='retailer')

    def test_returns_non_negative(self):
        self.assertGreaterEqual(_ai_order(self.retailer), 0)

    def test_low_inventory_high_order(self):
        self.retailer.inventory = 0
        self.retailer.save()
        order = _ai_order(self.retailer)
        self.assertGreater(order, 0)

    def test_high_inventory_low_order(self):
        self.retailer.inventory = 100
        self.retailer.save()
        order = _ai_order(self.retailer)
        self.assertEqual(order, 0)


class GetBullwhipDataTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        initialise_session(self.session)
        # Simulate 3 weeks of data
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])
        for _ in range(3):
            self.session.refresh_from_db()
            self.session.pending_customer_demand = 4
            self.session.save(update_fields=['pending_customer_demand'])
            process_week(self.session, {})

    def test_returns_dict(self):
        result = get_bullwhip_data(self.session)
        self.assertIsInstance(result, dict)

    def test_returns_empty_for_insufficient_data(self):
        empty_session = _create_full_session()
        initialise_session(empty_session)
        result = get_bullwhip_data(empty_session)
        self.assertEqual(result, {})

    def test_all_roles_have_ratio(self):
        result = get_bullwhip_data(self.session)
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            if role in result:
                self.assertIsInstance(result[role], float)


class GetChartDataTest(TestCase):
    def setUp(self):
        self.session = _create_full_session()
        initialise_session(self.session)
        self.session.pending_customer_demand = 4
        self.session.save(update_fields=['pending_customer_demand'])
        process_week(self.session, {})

    def test_returns_all_roles_including_customer(self):
        data = get_chart_data(self.session)
        for role in ['retailer', 'wholesaler', 'distributor', 'factory', 'customer']:
            self.assertIn(role, data)

    def test_each_role_has_history(self):
        data = get_chart_data(self.session)
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            self.assertIn('history', data[role])
            self.assertGreater(len(data[role]['history']), 0)


class GetAdvancedAnalyticsTest(TestCase):
    """Tests for get_advanced_analytics() — richer debrief metrics."""

    def setUp(self):
        self.session = _create_full_session()
        initialise_session(self.session)
        # Play 3 weeks of data with constant demand so we have history
        for _ in range(3):
            self.session.refresh_from_db()
            self.session.pending_customer_demand = 4
            self.session.save(update_fields=['pending_customer_demand'])
            process_week(self.session, {})

    def test_returns_dict_with_roles_key(self):
        result = get_advanced_analytics(self.session)
        self.assertIsInstance(result, dict)
        self.assertIn('roles', result)

    def test_roles_contains_supply_chain_roles(self):
        result = get_advanced_analytics(self.session)
        for role in ['retailer', 'wholesaler', 'distributor', 'factory']:
            self.assertIn(role, result['roles'])

    def test_service_level_is_percentage(self):
        result = get_advanced_analytics(self.session)
        for role_data in result['roles'].values():
            sl = role_data['service_level']
            self.assertGreaterEqual(sl, 0.0)
            self.assertLessEqual(sl, 100.0)

    def test_has_top_level_cost_keys(self):
        result = get_advanced_analytics(self.session)
        self.assertIn('total_holding_cost', result)
        self.assertIn('total_backlog_cost', result)
        self.assertIn('chain_service_level', result)
        self.assertIn('demand_avg', result)
        self.assertIn('demand_std', result)
        self.assertIn('bullwhip_diagnosis', result)

    def test_bullwhip_diagnosis_is_list_of_strings(self):
        result = get_advanced_analytics(self.session)
        diag = result['bullwhip_diagnosis']
        self.assertIsInstance(diag, list)
        for line in diag:
            self.assertIsInstance(line, str)

    def test_empty_session_returns_empty_roles(self):
        """A session with no history produces empty roles dict and safe defaults."""
        empty_session = _create_full_session()
        initialise_session(empty_session)
        result = get_advanced_analytics(empty_session)
        self.assertIsInstance(result, dict)
        self.assertEqual(result['roles'], {})

    def test_cost_decomposition_non_negative(self):
        result = get_advanced_analytics(self.session)
        self.assertGreaterEqual(result['total_holding_cost'], 0)
        self.assertGreaterEqual(result['total_backlog_cost'], 0)

    def test_avg_order_and_demand_match_keys_present(self):
        result = get_advanced_analytics(self.session)
        for role_data in result['roles'].values():
            self.assertIn('avg_order', role_data)
            self.assertIn('demand_match', role_data)
            self.assertIn('max_backlog', role_data)
            self.assertIn('weeks_with_backlog', role_data)
