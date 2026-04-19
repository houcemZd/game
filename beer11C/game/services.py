"""
Beer Game Engine — Phase-gated turn system
==========================================

Physical Beer Game sequence each week:

  Phase 1 — RECEIVE:  server stages incoming goods; player clicks "Confirm Receive"
                       → inventory += arriving shipments (production completing for factory)
                       → factory also converts last week's production request into production
  Phase 2 — SHIP:     server computes downstream demand; player sees it and confirms
                       → ship to downstream, backlog updated
  Phase 3 — ORDER:    player decides upstream order qty and submits
                       → non-factory: PipelineOrder upstream, ORDER_DELAY=2 weeks
                       → factory: PipelineOrder to SELF, 1-week production request delay
                         (next week, factory reads this request and starts actual production
                          which then takes 2 more weeks in production delay)

Factory pipeline summary:
  Week N:   Factory places Production Request = X (→ PipelineOrder, arrives week N+1)
  Week N+1: Factory reads request, confirms → X enters Production Delay (PipelineShipment, arrives N+3)
  Week N+3: X units complete and enter inventory

Key rules:
  - order placed in week W arrives upstream in W + ORDER_DELAY (2 weeks)
  - factory production request placed in W arrives back at factory in W + 1 (1 week)
  - production started in W completes in W + SHIP_DELAY (2 weeks)
  - customer role skips receive/ship phases; submitting demand counts as 'done'
"""

import statistics as _stats
from django.db import transaction
from .models import (
    GameSession, Player, PlayerSession, WeeklyState,
    PipelineOrder, PipelineShipment, CustomerDemand,
)

ORDER_DELAY = 2
SHIP_DELAY  = 2
CHAIN_ORDER = {'retailer': 1, 'wholesaler': 2, 'distributor': 3, 'factory': 4}

NON_CUSTOMER_ROLES = ['retailer', 'wholesaler', 'distributor', 'factory']


# ─────────────────────────────────────────────────────────────────────────────
# Demand schedule helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_scheduled_demand(session, week):
    """
    Return the pre-seeded demand for *week* based on session.demand_schedule.

    Returns:
      - int  if the schedule supplies a value for this week.
      - None if the session is in manual mode (demand_schedule is None).
    """
    schedule = session.demand_schedule
    if schedule is None:
        return None  # manual — customer player must enter demand
    if schedule == 'classic':
        # MIT classic step-function: 4 units per week for weeks 1-4, then 8.
        return 4 if week <= 4 else 8
    if isinstance(schedule, list):
        if len(schedule) >= week:
            return int(schedule[week - 1])
        if schedule:
            return int(schedule[-1])  # repeat last value if week exceeds list
    return 4  # safe fallback


# ─────────────────────────────────────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────────────────────────────────────

def initialise_session(session, init_orders_placed=4, init_incoming=4):
    """
    Steady-state pipeline initialisation.

    For non-factory roles:
      - PipelineShipment × 2: goods in transit (ship delay), arriving weeks 1 & 2
      - PipelineOrder    × 2: orders placed upstream, arriving weeks 1 & 2

    For factory:
      - PipelineShipment × 2: production in progress (production delay), arriving weeks 1 & 2
      - PipelineOrder    × 2: production REQUESTS placed last week (1-week delay),
                               arriving week 1 (so factory reads it in week 1 and starts production)
        Note: only 1 pre-game production request needed since the delay is 1 week.
    """
    for player in session.players.all():
        # Ship delay / Production delay slots (blue): goods arriving weeks 1 & 2
        for arrival_week in [1, 2]:
            PipelineShipment.objects.create(
                receiver=player,
                quantity=init_incoming,
                shipped_on_week=arrival_week - SHIP_DELAY,
                arrives_on_week=arrival_week,
            )

        if player.role == 'factory':
            # Factory production request (1-week order delay):
            # The request placed in week 0 arrives in week 1 → factory starts that production.
            PipelineOrder.objects.create(
                sender=player,
                quantity=init_orders_placed,
                placed_on_week=0,
                arrives_on_week=1,
                fulfilled=False,
            )
        else:
            # Non-factory: orders placed upstream (2-week order delay)
            for arrival_week in [1, 2]:
                PipelineOrder.objects.create(
                    sender=player,
                    quantity=init_orders_placed,
                    placed_on_week=arrival_week - ORDER_DELAY,
                    arrives_on_week=arrival_week,
                    fulfilled=False,
                )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 — Open a new week (server-side staging)
# ─────────────────────────────────────────────────────────────────────────────

def open_week(session):
    """
    Called once per week, before any player acts.
    Computes for each PlayerSession:
      - pending_received_qty  : units arriving this week
      - pending_order_qty     : downstream demand (order or customer demand)
    Sets every non-customer PlayerSession to PHASE_RECEIVE.
    Customer stays PHASE_IDLE (will submit demand separately).

    If session.demand_schedule is not None, the customer demand for this week
    is taken from the schedule automatically (no human customer input needed).

    IMPORTANT: Does NOT mutate Player.inventory or Player.backlog yet.
    Returns a dict of {role: {received, order_qty}} for broadcasting.
    """
    week    = session.current_week + 1
    players = {p.role: p for p in session.players.all()}
    staging = {}

    # --- Auto-populate demand from schedule if not already set manually ---
    if session.pending_customer_demand is None and session.demand_schedule is not None:
        scheduled = get_scheduled_demand(session, week)
        if scheduled is not None:
            session.pending_customer_demand = scheduled
            session.save(update_fields=['pending_customer_demand'])

    # --- Compute arriving shipments (production completing / goods arriving) ---
    for role, player in players.items():
        arriving = PipelineShipment.objects.filter(
            receiver=player, arrives_on_week=week, delivered=False
        )
        received = sum(s.quantity for s in arriving)
        staging[role] = {'received': received}

    # --- Compute incoming orders / production requests ---
    customer_qty = session.pending_customer_demand or 0

    for role, player in players.items():
        if role == 'retailer':
            order_qty = customer_qty
        elif role == 'factory':
            # Production request arriving this week (factory's own 1-week self-directed order).
            # Stored so apply_receive can convert it into actual production.
            pending_requests = PipelineOrder.objects.filter(
                sender=player,
                arrives_on_week=week,
                fulfilled=False,
            )
            order_qty = sum(o.quantity for o in pending_requests)
            staging[role]['production_request'] = order_qty

            # Distributor's real order arriving at the factory this week (2-week order delay).
            # This is what the factory will actually ship in Phase 2.
            downstream = player.get_downstream()  # distributor
            if downstream:
                dist_arriving = PipelineOrder.objects.filter(
                    sender=downstream, arrives_on_week=week, fulfilled=False
                )
                staging[role]['distributor_order'] = sum(o.quantity for o in dist_arriving)
            else:
                staging[role]['distributor_order'] = 0
        else:
            downstream = player.get_downstream()
            if downstream:
                arriving_orders = PipelineOrder.objects.filter(
                    sender=downstream, arrives_on_week=week, fulfilled=False
                )
                order_qty = sum(o.quantity for o in arriving_orders)
            else:
                order_qty = 0
        staging[role]['order_qty'] = order_qty

    # --- Update PlayerSession staging fields ---
    for ps in session.player_sessions.all():
        if ps.role == 'customer':
            # customer doesn't go through receive/ship
            ps.turn_phase           = PlayerSession.PHASE_IDLE
            ps.pending_received_qty = 0
            ps.pending_order_qty    = 0
            ps.pending_ship_qty     = None
            ps.pending_order        = None
        else:
            s = staging.get(ps.role, {})
            ps.turn_phase           = PlayerSession.PHASE_RECEIVE
            ps.pending_received_qty = s.get('received', 0)
            # For factory: store the production_request in pending_order_qty so
            # apply_receive can find and convert it; apply_receive will overwrite
            # it with the distributor's real order (for Phase 2 display + reconnect).
            ps.pending_order_qty    = s.get('order_qty', 0)
            ps.pending_ship_qty     = None
            ps.pending_order        = None
        ps.save(update_fields=[
            'turn_phase', 'pending_received_qty',
            'pending_order_qty', 'pending_ship_qty', 'pending_order'
        ])

    return staging


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Confirm Receive
# ─────────────────────────────────────────────────────────────────────────────

def apply_receive(ps):
    """
    Phase 1 — Confirm Receive.

    For non-factory:
      - Mark arriving PipelineShipments as delivered → add to inventory.

    For factory (two sub-steps):
      A) Receive completed production:
         PipelineShipments arriving this week (from production started 2 weeks ago)
         → add to inventory.
      B) Read & start production request:
         PipelineOrders placed by the factory itself last week (arrives_on_week == week)
         → convert each into a PipelineShipment (production delay, 2 weeks).
         → mark those PipelineOrders as fulfilled.
         The quantity of this production request is stored in staging
         so the panel can display it.

    Returns {received, production_started, new_inventory, backlog}
    """
    session = ps.game_session
    week    = session.current_week + 1
    player  = session.players.filter(role=ps.role).first()
    if not player:
        return {}

    # ── A: Receive completed production (PipelineShipments arriving now) ──────
    arriving_ships = PipelineShipment.objects.filter(
        receiver=player, arrives_on_week=week, delivered=False
    )
    received = sum(s.quantity for s in arriving_ships)
    arriving_ships.update(delivered=True)
    player.inventory += received
    player.save(update_fields=['inventory'])

    production_started = 0
    distributor_order  = 0
    if ps.role == 'factory':
        # ── B: Convert last week's production request into actual production ──
        pending_requests = PipelineOrder.objects.filter(
            sender=player,
            arrives_on_week=week,
            fulfilled=False,
        )
        production_started = sum(o.quantity for o in pending_requests)
        if production_started > 0:
            PipelineShipment.objects.create(
                receiver=player,
                quantity=production_started,
                shipped_on_week=week,
                arrives_on_week=week + SHIP_DELAY,  # completes in 2 weeks
            )
        pending_requests.update(fulfilled=True)

        # Consume the distributor's order arriving this week so it leaves the
        # order pipeline right after receive confirmation.
        downstream = player.get_downstream()  # distributor
        if downstream:
            dist_orders = PipelineOrder.objects.filter(
                sender=downstream, arrives_on_week=week, fulfilled=False
            )
            distributor_order = sum(o.quantity for o in dist_orders)
            dist_orders.update(fulfilled=True)

        # Overwrite pending_order_qty with the distributor's real order so:
        # a) phase_ship panel (sent below) shows correct demand, and
        # b) reconnect in PHASE_SHIP also sees the correct demand.
        ps.pending_order_qty = distributor_order
    elif ps.role in ('wholesaler', 'distributor'):
        # Consume downstream orders arriving this week as soon as receive is
        # confirmed, so the downstream can place a fresh order in the pipeline.
        downstream = player.get_downstream()
        if downstream:
            arriving_orders = PipelineOrder.objects.filter(
                sender=downstream, arrives_on_week=week, fulfilled=False
            )
            incoming_order_qty = sum(o.quantity for o in arriving_orders)
            arriving_orders.update(fulfilled=True)
            ps.pending_order_qty = incoming_order_qty

    ps.turn_phase = PlayerSession.PHASE_SHIP
    save_fields = ['turn_phase', 'pending_received_qty']
    if ps.role in ('factory', 'wholesaler', 'distributor'):
        save_fields.append('pending_order_qty')
    ps.save(update_fields=save_fields)

    # Refresh retailer's demand if customer already submitted
    if ps.role == 'retailer' and session.pending_customer_demand is not None:
        ps.pending_order_qty = session.pending_customer_demand
        ps.save(update_fields=['pending_order_qty'])

    return {
        'received':           received,            # units entering inventory now
        'production_started': production_started,  # units entering production delay now (factory)
        'distributor_order':  distributor_order,   # distributor's demand arriving this week (factory)
        'new_inventory':      player.inventory,
        'backlog':            player.backlog,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Confirm Shipment
# ─────────────────────────────────────────────────────────────────────────────

def apply_ship(ps):
    """
    Player confirmed the shipment to downstream.
    - Compute how much can be shipped (inventory vs. demand + backlog)
    - Deduct from Player.inventory, update Player.backlog
    - Create PipelineShipment to downstream
    - Advance ps.turn_phase to PHASE_ORDER
    Returns {shipped, new_inventory, new_backlog, demand_received}
    """
    session = ps.game_session
    week    = session.current_week + 1
    player  = session.players.filter(role=ps.role).first()
    if not player:
        return {}

    # For retailer, use actual customer demand now (should be set by customer submit)
    if ps.role == 'retailer':
        order_qty = session.pending_customer_demand or ps.pending_order_qty
        # Update staging to reflect real demand
        ps.pending_order_qty = order_qty
    else:
        # Downstream orders are consumed during apply_receive; ship phase uses
        # the staged quantity captured in pending_order_qty.
        order_qty = ps.pending_order_qty

    total_demand = order_qty + player.backlog
    available    = player.inventory

    if available >= total_demand:
        shipped         = total_demand
        player.inventory -= total_demand
        player.backlog    = 0
    else:
        shipped          = available
        player.backlog   = total_demand - available
        player.inventory = 0

    # Create shipment to downstream
    downstream = player.get_downstream()
    if downstream and shipped > 0:
        PipelineShipment.objects.create(
            receiver=downstream,
            quantity=shipped,
            shipped_on_week=week,
            arrives_on_week=week + SHIP_DELAY,
        )

    player.save(update_fields=['inventory', 'backlog'])

    ps.pending_ship_qty = shipped
    ps.pending_order_qty = order_qty
    ps.turn_phase = PlayerSession.PHASE_ORDER
    ps.save(update_fields=['turn_phase', 'pending_ship_qty', 'pending_order_qty'])

    return {
        'shipped':         shipped,
        'demand_received': order_qty,
        'new_inventory':   player.inventory,
        'new_backlog':     player.backlog,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Submit Order (upstream order / production)
# ─────────────────────────────────────────────────────────────────────────────

def apply_order(ps, order_qty):
    """
    Player submits their upstream order / factory production request.

    For non-factory roles:
      - Creates PipelineOrder → arrives at upstream after ORDER_DELAY (2 weeks)

    For factory:
      - Creates PipelineOrder TO ITSELF with ORDER_DELAY=1 week.
        This represents the 1-week "production request" delay before
        production actually starts.
      - At the NEXT week's Phase 1, the factory reads this order,
        and apply_receive converts it into a PipelineShipment (production delay, 2 weeks).

    Returns {order_placed}
    """
    session = ps.game_session
    week    = session.current_week + 1
    player  = session.players.filter(role=ps.role).first()
    if not player:
        return {}

    if ps.role == 'factory':
        # Production request: 1-week order delay before production starts.
        # We store as a self-directed PipelineOrder using the player as both
        # sender AND we mark upstream=None. We use a sentinel: sender=player,
        # arrives_on_week = week + 1.
        if order_qty > 0:
            PipelineOrder.objects.create(
                sender=player,
                quantity=order_qty,
                placed_on_week=week,
                arrives_on_week=week + 1,   # 1-week production request delay
                fulfilled=False,
            )
    else:
        upstream = player.get_upstream()
        if upstream and order_qty > 0:
            PipelineOrder.objects.create(
                sender=player,
                quantity=order_qty,
                placed_on_week=week,
                arrives_on_week=week + ORDER_DELAY,
            )

    ps.pending_order = order_qty
    ps.turn_phase    = PlayerSession.PHASE_DONE
    ps.save(update_fields=['pending_order', 'turn_phase'])

    session.mark_submitted(ps.role)

    return {'order_placed': order_qty}


# ─────────────────────────────────────────────────────────────────────────────
# Week close — costs + snapshot (called when all roles are DONE)
# ─────────────────────────────────────────────────────────────────────────────

def close_week(session):
    """
    Called after all PlayerSessions reach PHASE_DONE.
    Calculates costs, saves WeeklyState snapshots, advances session.current_week.
    Returns summary dict, or {} if the week was already closed (idempotency guard).
    """
    with transaction.atomic():
        # Lock the session row; bail if another consumer already closed this week.
        session_locked = GameSession.objects.select_for_update().get(pk=session.pk)
        if session_locked.current_week != session.current_week:
            return {}  # already advanced by a concurrent call
        week    = session_locked.current_week + 1
        return _close_week_inner(session_locked, week)


def _close_week_inner(session, week):
    """Inner implementation of close_week (already inside a select_for_update block)."""
    summary = {}
    players = {p.role: p for p in session.players.all()}

    # Fetch all PlayerSessions in a single query to avoid N+1
    player_sessions = {
        ps.role: ps for ps in session.player_sessions.all()
    }

    for role, player in players.items():
        ps = player_sessions.get(role)
        if not ps:
            continue

        cost = (player.inventory * player.holding_cost +
                player.backlog   * player.backlog_cost)
        player.total_cost += cost
        player.save(update_fields=['total_cost'])

        order_placed      = ps.pending_order        or 0
        shipment_received = ps.pending_received_qty or 0
        shipped           = ps.pending_ship_qty      or 0
        order_received    = ps.pending_order_qty     or 0

        WeeklyState.objects.create(
            player=player, week=week,
            inventory=player.inventory,
            backlog=player.backlog,
            order_placed=order_placed,
            order_received=order_received,
            shipment_sent=shipped,
            shipment_received=shipment_received,
            cost_this_week=cost,
            cumulative_cost=player.total_cost,
        )

        summary[role] = {
            'inventory':         player.inventory,
            'backlog':           player.backlog,
            'order_placed':      order_placed,
            'order_received':    order_received,
            'shipped':           shipped,
            'shipment_received': shipment_received,
            'cost_this_week':    cost,
            'total_cost':        player.total_cost,
        }

    # Customer demand history
    customer_qty = session.pending_customer_demand or 0
    CustomerDemand.objects.create(session=session, week=week, quantity=customer_qty)

    # Advance week
    session.current_week = week
    if session.current_week >= session.max_weeks:
        session.is_active = False
        session.status    = GameSession.STATUS_FINISHED
    session.reset_submissions()  # clears submitted_roles + pending_customer_demand
    session.save(update_fields=['current_week', 'is_active', 'status'])

    # Reset all PlayerSession phases to IDLE (keep is_ai flag)
    session.player_sessions.update(
        turn_phase=PlayerSession.PHASE_IDLE,
        pending_received_qty=0,
        pending_order_qty=0,
        pending_ship_qty=None,
        pending_order=None,
    )

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# AI auto-complete (used when a role is marked is_ai=True)
# ─────────────────────────────────────────────────────────────────────────────

def ai_complete_role(session, role):
    """
    Complete all three weekly phases (receive → ship → order) for an AI-managed
    role using the base-stock AI policy.  Also sets the week_ready sentinel
    (pending_order = -1) so the week can advance without a human click.

    Safe to call from both the HTTP view (sync) and the consumer (via
    database_sync_to_async).

    Returns True if phases were completed, False if the role is not in a
    phase that needs completing.
    """
    ps = PlayerSession.objects.select_related('game_session').get(
        game_session=session, role=role
    )
    player = session.players.filter(role=role).first()
    if not player:
        return False

    changed = False
    if ps.turn_phase == PlayerSession.PHASE_RECEIVE:
        apply_receive(ps)
        ps.refresh_from_db()
        changed = True
    if ps.turn_phase == PlayerSession.PHASE_SHIP:
        apply_ship(ps)
        ps.refresh_from_db()
        changed = True
    if ps.turn_phase == PlayerSession.PHASE_ORDER:
        player.refresh_from_db()
        apply_order(ps, _ai_order(player))
        ps.refresh_from_db()
        changed = True

    # Mark week_ready sentinel so the week can advance without a human click.
    if ps.turn_phase == PlayerSession.PHASE_DONE:
        PlayerSession.objects.filter(id=ps.id).update(pending_order=-1)

    return changed


# ─────────────────────────────────────────────────────────────────────────────
# Legacy single-call process_week (used by single-player / HTTP views)
# ─────────────────────────────────────────────────────────────────────────────

def process_week(session, player_orders: dict):
    """
    Original single-pass week processing for single-player mode (HTTP form submit).
    player_orders = {player_id: order_qty}

    The entire function runs inside a DB transaction so any failure leaves the
    game state fully consistent (no half-processed weeks).

    Double-submit protection: the function records `session.current_week` before
    acquiring the row lock.  After locking it re-reads the DB value; if the week
    has already advanced (another request won the race), it returns silently.
    """
    # Remember the week the view saw before we enter the transaction.
    expected_current = session.current_week

    with transaction.atomic():
        # Lock the session row so concurrent submissions queue up.
        session_locked = GameSession.objects.select_for_update().get(pk=session.pk)
        # If current_week already advanced, this submit is a duplicate — bail.
        if session_locked.current_week != expected_current:
            return {}
        week = session_locked.current_week + 1
        return _process_week_inner(session_locked, player_orders, week)


def _process_week_inner(session, player_orders: dict, week: int):
    """Inner (already-locked, already-validated) implementation of process_week."""
    summary = {}
    players = {p.role: p for p in session.players.all()}

    for player in players.values():
        # Receive completing production (PipelineShipments) → inventory
        arriving = PipelineShipment.objects.filter(
            receiver=player, arrives_on_week=week, delivered=False
        )
        received = sum(s.quantity for s in arriving)
        arriving.update(delivered=True)
        player.inventory += received
        player._received  = received

        # Factory: also convert pending production requests into actual production
        if player.role == 'factory':
            pending_requests = PipelineOrder.objects.filter(
                sender=player, arrives_on_week=week, fulfilled=False
            )
            production_started = sum(o.quantity for o in pending_requests)
            if production_started > 0:
                PipelineShipment.objects.create(
                    receiver=player,
                    quantity=production_started,
                    shipped_on_week=week,
                    arrives_on_week=week + SHIP_DELAY,
                )
            pending_requests.update(fulfilled=True)

    customer_qty = session.pending_customer_demand or 0

    for player in players.values():
        if player.role == 'retailer':
            player._incoming_order = customer_qty
        elif player.role == 'factory':
            # Factory's "incoming order" = its own production request arriving this week
            # (already processed above — just read from distributor's orders)
            downstream = player.get_downstream()  # distributor
            if downstream:
                arriving_orders = PipelineOrder.objects.filter(
                    sender=downstream, arrives_on_week=week, fulfilled=False
                )
                qty = sum(o.quantity for o in arriving_orders)
                arriving_orders.update(fulfilled=True)
            else:
                qty = 0
            player._incoming_order = qty
        else:
            downstream = player.get_downstream()
            arriving_orders = PipelineOrder.objects.filter(
                sender=downstream, arrives_on_week=week, fulfilled=False
            )
            qty = sum(o.quantity for o in arriving_orders)
            arriving_orders.update(fulfilled=True)
            player._incoming_order = qty

    for player in players.values():
        total_demand = player._incoming_order + player.backlog
        available    = player.inventory
        if available >= total_demand:
            shipped = total_demand
            player.inventory -= total_demand
            player.backlog    = 0
        else:
            shipped = available
            player.backlog   = total_demand - available
            player.inventory = 0
        player._shipped = shipped
        downstream = player.get_downstream()
        if downstream and shipped > 0:
            PipelineShipment.objects.create(
                receiver=downstream,
                quantity=shipped,
                shipped_on_week=week,
                arrives_on_week=week + SHIP_DELAY,
            )

    for player in players.values():
        order_qty = player_orders.get(player.id, _ai_order(player))
        if player.role == 'factory':
            # Production request: 1-week delay before actual production starts
            if order_qty > 0:
                PipelineOrder.objects.create(
                    sender=player,
                    quantity=order_qty,
                    placed_on_week=week,
                    arrives_on_week=week + 1,   # 1-week production request delay
                    fulfilled=False,
                )
        else:
            upstream = player.get_upstream()
            if upstream and order_qty > 0:
                PipelineOrder.objects.create(
                    sender=player,
                    quantity=order_qty,
                    placed_on_week=week,
                    arrives_on_week=week + ORDER_DELAY,
                )
        player._order_placed = order_qty

    for player in players.values():
        cost = (player.inventory * player.holding_cost +
                player.backlog   * player.backlog_cost)
        player.total_cost    += cost
        player._cost_this_week = cost

    for player in players.values():
        player.save()
        WeeklyState.objects.create(
            player=player, week=week,
            inventory=player.inventory,
            backlog=player.backlog,
            order_placed=player._order_placed,
            order_received=player._incoming_order,
            shipment_sent=player._shipped,
            shipment_received=player._received,
            cost_this_week=player._cost_this_week,
            cumulative_cost=player.total_cost,
        )
        summary[player.role] = {
            'inventory':         player.inventory,
            'backlog':           player.backlog,
            'order_placed':      player._order_placed,
            'order_received':    player._incoming_order,
            'shipped':           player._shipped,
            'shipment_received': player._received,
            'cost_this_week':    player._cost_this_week,
            'total_cost':        player.total_cost,
        }

    CustomerDemand.objects.create(session=session, week=week, quantity=customer_qty)
    session.current_week = week
    session.pending_customer_demand = None   # consumed; reset so stale value can't leak
    save_fields = ['current_week', 'pending_customer_demand']
    if session.current_week >= session.max_weeks:
        session.is_active = False
        session.status = GameSession.STATUS_FINISHED
        save_fields += ['is_active', 'status']
    session.save(update_fields=save_fields)
    return summary


def _ai_order(player):
    """Pipeline-aware base-stock policy (used by single-player AI)."""
    target = 16
    in_transit = sum(
        s.quantity for s in PipelineShipment.objects.filter(
            receiver=player, delivered=False
        )
    )
    return max(0, target - player.inventory - in_transit + player.backlog)


def get_bullwhip_data(session):
    demand_vals = list(
        CustomerDemand.objects.filter(session=session)
        .order_by('week').values_list('quantity', flat=True)
    )
    if len(demand_vals) < 2:
        return {}
    demand_std = _stats.stdev(demand_vals) or 1.0
    result = {}
    for player in session.players.all():
        orders = list(player.history.values_list('order_placed', flat=True))
        if len(orders) >= 2:
            result[player.role] = round(_stats.stdev(orders) / demand_std, 2)
    return result


def get_advanced_analytics(session):
    """
    Returns richer post-game analytics for instructor debrief:

    Per-role keys:
      service_level       — % of weeks with zero backlog (higher is better)
      weeks_with_backlog  — how many weeks the role had unmet demand
      avg_backlog         — average backlog across all weeks
      max_backlog         — peak backlog experienced
      max_inventory       — peak inventory held
      avg_inventory       — average inventory held
      holding_cost_total  — portion of total_cost from holding
      backlog_cost_total  — portion of total_cost from stockouts
      order_variance      — variance of order quantities (higher → more erratic)
      inventory_variance  — variance of inventory levels
      avg_order           — average weekly order placed
      demand_match        — avg_order / avg_demand (1.0 = perfect average matching)

    Top-level keys:
      total_holding_cost  — chain-wide holding cost
      total_backlog_cost  — chain-wide backlog/stockout cost
      chain_service_level — average service level across all supply roles
      demand_avg          — average customer demand
      demand_std          — std-dev of customer demand (0 if <2 data points)
      bullwhip_diagnosis  — list of human-readable interpretation strings
    """
    demand_vals = list(
        CustomerDemand.objects.filter(session=session)
        .order_by('week').values_list('quantity', flat=True)
    )
    n_weeks    = len(demand_vals)
    avg_demand = _stats.mean(demand_vals) if demand_vals else 4.0
    demand_std = _stats.stdev(demand_vals) if len(demand_vals) >= 2 else 0.0

    roles_data = {}
    total_holding = 0.0
    total_backlog = 0.0
    service_levels = []

    for player in session.players.all():
        history = list(player.history.order_by('week'))
        if not history:
            continue

        orders      = [h.order_placed for h in history]
        inventories = [h.inventory    for h in history]
        backlogs    = [h.backlog      for h in history]

        weeks_no_bl = sum(1 for b in backlogs if b == 0)
        srv_level   = (weeks_no_bl / len(backlogs) * 100) if backlogs else 100.0

        holding_total  = round(sum(h.inventory * player.holding_cost for h in history), 2)
        backlog_total  = round(sum(h.backlog   * player.backlog_cost  for h in history), 2)
        total_holding += holding_total
        total_backlog += backlog_total

        avg_order = _stats.mean(orders) if orders else 0.0

        roles_data[player.role] = {
            'service_level':      round(srv_level, 1),
            'weeks_with_backlog': len(backlogs) - weeks_no_bl,
            'avg_backlog':        round(_stats.mean(backlogs), 1) if backlogs else 0.0,
            'max_backlog':        max(backlogs, default=0),
            'max_inventory':      max(inventories, default=0),
            'avg_inventory':      round(_stats.mean(inventories), 1) if inventories else 0.0,
            'holding_cost_total': holding_total,
            'backlog_cost_total': backlog_total,
            'order_variance':     round(_stats.variance(orders), 2) if len(orders) >= 2 else 0.0,
            'inventory_variance': round(_stats.variance(inventories), 2) if len(inventories) >= 2 else 0.0,
            'avg_order':          round(avg_order, 1),
            'demand_match':       round(avg_order / avg_demand, 2) if avg_demand > 0 else 1.0,
        }
        if player.role != 'customer':
            service_levels.append(srv_level)

    # Build guided interpretation strings
    bullwhip_data   = get_bullwhip_data(session)
    diagnosis_lines = _bullwhip_diagnosis(bullwhip_data, roles_data, demand_std)

    return {
        'roles':               roles_data,
        'total_holding_cost':  round(total_holding, 2),
        'total_backlog_cost':  round(total_backlog, 2),
        'chain_service_level': round(_stats.mean(service_levels), 1) if service_levels else 100.0,
        'demand_avg':          round(avg_demand, 1),
        'demand_std':          round(demand_std, 2),
        'bullwhip_diagnosis':  diagnosis_lines,
    }


def _bullwhip_diagnosis(bullwhip, roles_data, demand_std):
    """
    Return a list of plain-English sentences explaining what the data shows.
    These surface in the results debrief to help students understand causes.
    """
    lines = []

    if not bullwhip:
        lines.append(
            "Not enough order history to calculate bullwhip ratios — "
            "play at least 2 weeks to see amplification metrics."
        )
        return lines

    # Identify worst and best roles
    ordered = sorted(bullwhip.items(), key=lambda kv: kv[1], reverse=True)
    worst_role, worst_ratio = ordered[0]
    best_role,  best_ratio  = ordered[-1]

    if worst_ratio > 3.0:
        lines.append(
            f"Severe bullwhip: {worst_role.title()} amplified demand variability "
            f"{worst_ratio:.1f}× — far above the ideal of 1.0. "
            "This is a textbook symptom of panic ordering or over-reaction to stock-outs."
        )
    elif worst_ratio > 1.5:
        lines.append(
            f"Moderate bullwhip: {worst_role.title()} shows a {worst_ratio:.1f}× amplification ratio. "
            "Ordering policies that do not account for pipeline inventory typically cause this."
        )
    else:
        lines.append(
            f"Minimal bullwhip: all roles stayed close to a 1.0 ratio — excellent pipeline awareness."
        )

    # Check if amplification increases upstream (classic bullwhip pattern)
    chain = [r for r in ['retailer', 'wholesaler', 'distributor', 'factory'] if r in bullwhip]
    if len(chain) >= 2:
        ratios = [bullwhip[r] for r in chain]
        if all(ratios[i] <= ratios[i + 1] for i in range(len(ratios) - 1)):
            lines.append(
                "Classic upstream amplification detected: each tier placed more erratic orders than "
                "the one below it — exactly the bullwhip pattern described by Lee et al. (1997). "
                "Information delays and batching are the main drivers."
            )
        elif all(ratios[i] >= ratios[i + 1] for i in range(len(ratios) - 1)):
            lines.append(
                "Unusually, the upstream roles were more stable than downstream — "
                "the upstream players may have used smoother ordering policies."
            )

    # Service level hints
    stockout_roles = [
        role for role, d in roles_data.items()
        if d.get('weeks_with_backlog', 0) > 0 and role != 'customer'
    ]
    if stockout_roles:
        lines.append(
            f"Stock-outs occurred at: {', '.join(r.title() for r in stockout_roles)}. "
            "Backlog cost doubles holding cost per unit — keeping a modest safety stock "
            "usually reduces total chain cost."
        )
    else:
        lines.append(
            "No stock-outs across the chain — excellent service level. "
            "Check whether excess inventory held too long pushed up holding costs."
        )

    # Demand-variability context
    if demand_std < 0.5:
        lines.append(
            "Customer demand was nearly constant — all variability in orders was self-generated "
            "by the supply chain, not caused by real demand swings."
        )

    return lines


def get_chart_data(session):
    data = {}
    for player in session.players.all():
        history = list(player.history.values(
            'week', 'inventory', 'backlog',
            'order_placed', 'shipment_received', 'cost_this_week', 'cumulative_cost'
        ))
        data[player.role] = {'name': player.name, 'history': history}
    demand_hist = list(
        CustomerDemand.objects.filter(session=session)
        .order_by('week').values('week', 'quantity')
    )
    data['customer'] = {'name': 'Customer', 'history': demand_hist}
    return data
