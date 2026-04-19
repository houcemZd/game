"""
GameConsumer — phase-gated WebSocket handler.

Week turn sequence for each non-customer actor:
  1. Server calls open_week() → stages data → sends 'phase_receive' to each player
  2. Player clicks Confirm Receive → WS 'confirm_receive'
     → apply_receive() → inventory updated → sends 'phase_ship' back to that player
  3. Player clicks Confirm Ship → WS 'confirm_ship'
     → apply_ship() → stock deducted, shipment created → sends 'phase_order'
  4. Player enters order qty, submits → WS 'submit_order'
     → apply_order() → pipeline order created → sends 'phase_done'
     → if ALL non-customer roles are DONE → close_week() → broadcast new state

Customer actor:
  - submits demand via 'submit_order' (quantity field) → immediately PHASE_DONE
  - when customer is done AND all others are at phase >= RECEIVE, open_week runs
    (customer demand is used for retailer staging)
"""

import json
import asyncio
import traceback
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import GameSession, PlayerSession
from .services import open_week, apply_receive, apply_ship, apply_order, close_week, ai_complete_role

ALL_ROLES          = ['customer', 'retailer', 'wholesaler', 'distributor', 'factory']
NON_CUSTOMER_ROLES = ['retailer', 'wholesaler', 'distributor', 'factory']


class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        try:
            self.session_id = self.scope['url_route']['kwargs']['session_id']
            self.token      = self.scope['url_route']['kwargs']['token']
            self.group_name = f"game_{self.session_id}"
            self._ping_task = None

            self.player_session = await self._get_player_session()
            if not self.player_session:
                await self.close(code=4001)
                return

            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            await self._set_connected(True)
            self._ping_task = asyncio.ensure_future(self._keepalive())

            await self.channel_layer.group_send(self.group_name, {
                'type': 'broadcast_player_joined',
                'role': self.player_session.role,
                'name': self.player_session.name or self.player_session.role.title(),
            })

            # ── RECONNECTION RECOVERY ─────────────────────────────────────────
            # Send the correct initial message based on current game + phase state.
            # This restores the player to exactly where they were if they refresh.
            await self._send_reconnect_state()

            # Broadcast updated connected list to all others
            await self._broadcast_ready_status()

        except Exception as e:
            print("WS CONNECT ERROR:", repr(e))
            traceback.print_exc()
            await self.close(code=1011)

    async def disconnect(self, close_code):

        if self._ping_task:
            self._ping_task.cancel()
        if hasattr(self, 'player_session') and self.player_session:
            await self._set_connected(False)
            await self._save_disconnect_time()          # ← record when they left
            await self.channel_layer.group_send(self.group_name, {
                'type': 'broadcast_player_left',
                'role': self.player_session.role,
                'name': self.player_session.name or self.player_session.role.title(),
            })
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def _send_reconnect_state(self):
        """
        Called on every connect. Sends the right message type based on
        current game status and this player's turn phase.
        Restores the player's screen without them having to do anything.
        """
        ps      = self.player_session
        session = await self._get_session()
        state   = await self._build_state_for_role(ps.role)

        # ── Not yet in a game ─────────────────────────────────────────────────
        if session.status == GameSession.STATUS_LOBBY:
            await self.send(text_data=json.dumps({'type': 'state_update', **state}))
            await self.send(text_data=json.dumps({
                'type':      'ready_status',
                'submitted': session.submitted_role_list,
                'connected': await self._get_connected_roles(),
                'ready':     session.ready_role_list,
                'status':    session.status,
                'total':     5,
                'phases':    await self._get_all_phases(),
            }))
            return

        # ── Game finished ─────────────────────────────────────────────────────
        if session.status == GameSession.STATUS_FINISHED:
            await self.send(text_data=json.dumps({
                'type': 'game_over',
                'results_url': f"/game/{session.id}/results/",
                **state,
            }))
            return

        # ── Game in progress — restore to current phase ───────────────────────
        # First send a state_update so HUD and board are current
        await self.send(text_data=json.dumps({'type': 'state_update', **state}))

        # If they have a pending week summary (missed the close_week notification), show it
        summary = await self._get_last_week_summary()
        if summary:
            await self.send(text_data=json.dumps({
                'type':         'week_summary',
                'week_summary': summary,
                'week':         session.current_week,
            }))

        # Restore phase panel
        phase = ps.turn_phase

        if ps.role == 'customer':
            if phase == PlayerSession.PHASE_DONE:
                # Customer already submitted — show submitted state + week-ready
                await self.send(text_data=json.dumps({
                    'type':     'phase_done',
                    'role':     'customer',
                    'quantity': session.pending_customer_demand or 0,
                }))
            else:
                # Customer needs to submit demand
                await self.send(text_data=json.dumps({
                    'type': 'week_open', **state,
                }))
            return

        if phase == PlayerSession.PHASE_IDLE:
            # Week not opened yet — just show the state
            pass

        elif phase == PlayerSession.PHASE_RECEIVE:
            # Restore Phase 1 panel.
            # For factory: demand_incoming must be the distributor's arriving order
            # (not the production request in pending_order_qty which apply_receive hasn't run yet).
            if ps.role == 'factory':
                dist_order = await self._get_factory_distributor_order()
                demand_incoming    = dist_order
                production_request = ps.pending_order_qty   # factory's own request arriving
            else:
                demand_incoming    = ps.pending_order_qty
                production_request = None

            await self.send(text_data=json.dumps({
                'type':               'phase_receive',
                **state,
                'pending_received':   ps.pending_received_qty,
                'demand_incoming':    demand_incoming,
                'production_request': production_request,
            }))

        elif phase == PlayerSession.PHASE_SHIP:
            # Restore Phase 2 panel (they already confirmed receive)
            # Reconstruct the ship panel data from staged fields
            # Note: production_started is not persisted, so it's omitted from the
            # reconnect message; it was only needed for the log in the initial message.
            await self.send(text_data=json.dumps({
                'type':               'phase_ship',
                'received':           ps.pending_received_qty,
                'production_started': 0,
                'new_inventory':      state.get('own', {}).get('inventory', 0),
                'backlog':            state.get('own', {}).get('backlog', 0),
                'demand_incoming':    ps.pending_order_qty,
                'role':               ps.role,
                # Full state for board refresh
                'map':               state.get('map', {}),
                'own':               state.get('own', {}),
                'pipeline':          state.get('pipeline', []),
                'outgoing_orders':   state.get('outgoing_orders', []),
            }))

        elif phase == PlayerSession.PHASE_ORDER:
            # Restore Phase 3 panel
            await self.send(text_data=json.dumps({
                'type':          'phase_order',
                'shipped':       ps.pending_ship_qty or 0,
                'demand_received': ps.pending_order_qty,
                'new_inventory': state.get('own', {}).get('inventory', 0),
                'new_backlog':   state.get('own', {}).get('backlog', 0),
                'role':          ps.role,
                # Full state for board refresh
                'map':             state.get('map', {}),
                'own':             state.get('own', {}),
                'pipeline':        state.get('pipeline', []),
                'outgoing_orders': state.get('outgoing_orders', []),
            }))

        elif phase == PlayerSession.PHASE_DONE:
            # They already submitted — show done panel + week-ready button
            await self.send(text_data=json.dumps({
                'type':         'phase_done',
                'role':         ps.role,
                'order_placed': ps.pending_order or 0,
                # Full state for board refresh
                'map':             state.get('map', {}),
                'own':             state.get('own', {}),
                'pipeline':        state.get('pipeline', []),
                'outgoing_orders': state.get('outgoing_orders', []),
            }))
            # Check if all phases done so they see the week-ready button correctly
            if await self._all_phase_done():
                await self.send(text_data=json.dumps({'type': 'all_phases_done'}))

        # Always send current ready_status so pills update
        await self.send(text_data=json.dumps({
            'type':      'ready_status',
            'submitted': session.submitted_role_list,
            'connected': await self._get_connected_roles(),
            'ready':     session.ready_role_list,
            'status':    session.status,
            'total':     5,
            'phases':    await self._get_all_phases(),
        }))


    async def _keepalive(self):
        while True:
            await asyncio.sleep(20)
            try:
                await self.send(text_data=json.dumps({'type': 'ping'}))
            except Exception:
                break

    # ── Receive ───────────────────────────────────────────────────────────────

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON")
            return
        t = data.get('type')
        if   t == 'confirm_receive': await self._handle_confirm_receive()
        elif t == 'confirm_ship':    await self._handle_confirm_ship()
        elif t == 'submit_order':    await self._handle_submit_order(data)
        elif t == 'week_ready':      await self._handle_week_ready()
        elif t == 'player_ready':    await self._handle_ready()
        elif t == 'set_name':        await self._handle_set_name(data)
        elif t == 'pong':            pass
        else: await self._send_error(f"Unknown message type: {t}")

    # ── Lobby ─────────────────────────────────────────────────────────────────

    async def _handle_ready(self):
        session = await self._get_session()
        if session.status != GameSession.STATUS_LOBBY:
            await self._send_error("Game is not in lobby state.")
            return
        # Mark this player as ready in DB
        await self._mark_ready(session)
        # Broadcast updated ready list to all players immediately
        await self._broadcast_ready_status()
        # Check if all connected players are now ready
        if await self._all_ready():
            await self._start_game()

    async def _start_game(self):
        await self._set_status(GameSession.STATUS_PLAYING)
        # Tell all players the game has started so they hide their lobby overlays
        connected = await self._get_connected_roles()
        await self.channel_layer.group_send(self.group_name, {
            'type':      'broadcast_ready_status',
            'submitted': [],
            'connected': connected,
            'ready':     await self._get_ready_roles(),
            'status':    GameSession.STATUS_PLAYING,
            'total':     5,
            'phases':    {},
        })
        # Open week 1 — sends phase_receive to all non-customer roles
        await self._do_open_week()

    # ── Phase 1: Confirm Receive ──────────────────────────────────────────────

    async def _handle_confirm_receive(self):
        ps = await self._get_player_session()
        if ps.role == 'customer':
            await self._send_error("Customer has no receive phase.")
            return
        if ps.turn_phase != PlayerSession.PHASE_RECEIVE:
            await self._send_error(f"Not in receive phase (currently: {ps.turn_phase})")
            return

        result = await database_sync_to_async(apply_receive)(ps)
        ps = await self._get_player_session()

        # Build updated state so client can refresh the board
        state = await self._build_state_for_role(ps.role)

        await self.send(text_data=json.dumps({
            'type':               'phase_ship',
            'received':           result.get('received', 0),
            'production_started': result.get('production_started', 0),  # factory only
            'new_inventory':      result.get('new_inventory', 0),
            'backlog':            result.get('backlog', 0),
            # For factory: ps.pending_order_qty was updated by apply_receive to the
            # distributor's real order arriving this week (not the production request).
            # For other roles: pending_order_qty = incoming order from downstream.
            'demand_incoming':    ps.pending_order_qty,
            'role':               ps.role,
            # Full state for board refresh
            'map':               state.get('map', {}),
            'own':               state.get('own', {}),
            'pipeline':          state.get('pipeline', []),
            'outgoing_orders':   state.get('outgoing_orders', []),
        }))

        # Sync all other players' boards and phase pills
        await self._broadcast_board_to_others(ps.role)
        await self._broadcast_ready_status()

        # Notify the upstream partner that their shipment was received
        received_qty = result.get('received', 0)
        if received_qty > 0:
            await self._notify_shipment_received(ps.role, received_qty)

    # ── Phase 2: Confirm Ship ─────────────────────────────────────────────────

    async def _handle_confirm_ship(self):
        ps = await self._get_player_session()
        if ps.role == 'customer':
            await self._send_error("Customer has no ship phase.")
            return
        if ps.turn_phase != PlayerSession.PHASE_SHIP:
            await self._send_error(f"Not in ship phase (currently: {ps.turn_phase})")
            return

        result = await database_sync_to_async(apply_ship)(ps)
        ps     = await self._get_player_session()
        session = await self._get_session()

        # Build updated state so client can refresh the board
        state = await self._build_state_for_role(ps.role)

        await self.send(text_data=json.dumps({
            'type':            'phase_order',
            'shipped':         result.get('shipped', 0),
            'demand_received': result.get('demand_received', 0),
            'new_inventory':   result.get('new_inventory', 0),
            'new_backlog':     result.get('new_backlog', 0),
            'role':            ps.role,
            # Full state for board refresh
            'map':             state.get('map', {}),
            'own':             state.get('own', {}),
            'pipeline':        state.get('pipeline', []),
            'outgoing_orders': state.get('outgoing_orders', []),
        }))

        # Sync all other players' boards and phase pills
        await self._broadcast_board_to_others(ps.role)
        await self._broadcast_ready_status()

        # Notify the downstream partner that a shipment is heading their way
        shipped = result.get('shipped', 0)
        if shipped > 0:
            playing_week = session.current_week + 1
            await self._notify_shipment_dispatched(ps.role, shipped, playing_week + 2)

    # ── Phase 3: Submit Order ─────────────────────────────────────────────────

    async def _handle_submit_order(self, data):
        session = await self._get_session()
        ps      = await self._get_player_session()

        if session.status == GameSession.STATUS_LOBBY:
            await self._send_error("Game hasn't started. Click Ready first.")
            return
        if session.is_finished:
            await self._send_error("Game is over.")
            return

        try:
            qty = int(data.get('quantity', 0))
            if qty < 0:
                raise ValueError
        except (ValueError, TypeError):
            await self._send_error("Enter a non-negative integer.")
            return

        role = ps.role

        # ── Customer submits demand ──────────────────────────────────────────
        if role == 'customer':
            if ps.turn_phase == PlayerSession.PHASE_DONE:
                await self._send_error("Already submitted this week.")
                return
            await self._set_customer_demand(qty)
            await self._set_phase(PlayerSession.PHASE_DONE)

            # Update retailer's pending_order_qty with the real demand
            await self._update_retailer_demand(qty)

            await self.send(text_data=json.dumps({
                'type': 'phase_done',
                'role': role,
                'quantity': qty,
            }))
            await self._broadcast_ready_status()

            # Push updated demand to retailer if they're still in receive/ship phase
            await self._maybe_broadcast_updated_demand(qty)

            # Same as other roles: notify when all done, wait for week_ready clicks
            if await self._all_phase_done():
                await self._broadcast_all_phases_done()
            return

        # ── Non-customer: must be in PHASE_ORDER ────────────────────────────
        if ps.turn_phase == PlayerSession.PHASE_DONE:
            await self._send_error("Already submitted this week.")
            return
        if ps.turn_phase != PlayerSession.PHASE_ORDER:
            await self._send_error(
                f"Complete previous phases first (currently: {ps.turn_phase}). "
                f"You need to confirm receive and ship before placing an order."
            )
            return

        result = await database_sync_to_async(apply_order)(ps, qty)
        ps     = await self._get_player_session()

        # Build updated state so client can refresh the board
        state = await self._build_state_for_role(ps.role)

        await self.send(text_data=json.dumps({
            'type':         'phase_done',
            'role':         role,
            'order_placed': result.get('order_placed', 0),
            # Full state for board refresh
            'map':             state.get('map', {}),
            'own':             state.get('own', {}),
            'pipeline':        state.get('pipeline', []),
            'outgoing_orders': state.get('outgoing_orders', []),
        }))

        # Sync all other players' boards so they see the newly placed order
        await self._broadcast_board_to_others(ps.role)
        await self._broadcast_ready_status()

        # Notify the upstream partner that an order is heading their way
        # (factory self-orders have no external upstream — _notify_order_placed handles that)
        order_qty = result.get('order_placed', 0)
        if order_qty > 0:
            playing_week = session.current_week + 1
            delay = 1 if role == 'factory' else 2
            await self._notify_order_placed(role, order_qty, playing_week + delay)

        # When all phases done, notify everyone to show "week ready" button
        # The week closes only when all actors explicitly click week_ready
        if await self._all_phase_done():
            await self._broadcast_all_phases_done()

    # ── Week ready ────────────────────────────────────────────────────────────

    async def _handle_week_ready(self):
        """Player filled their weekly form and clicked 'Prêt pour semaine suivante'."""
        ps = await self._get_player_session()
        if ps.turn_phase != PlayerSession.PHASE_DONE:
            await self._send_error("Terminez toutes les étapes avant de confirmer.")
            return

        await self._mark_week_ready_flag()
        await self._broadcast_week_ready_status()

        if await self._all_week_ready():
            await self._do_close_week()

    async def _broadcast_board_to_others(self, acting_role):
        """
        After any phase action, push a fresh board_update to every other role
        so all clients stay in sync throughout the week turn.
        Each role receives a state view built from their own perspective,
        respecting the MIT beer game information-hiding rules.

        phases + connected are fetched once and embedded in every board_update
        so the observer's phase pills update atomically with the board, without
        having to wait for the separately-sent ready_status message.
        """
        phases    = await self._get_all_phases()
        connected = await self._get_connected_roles()
        for role in ALL_ROLES:
            if role == acting_role:
                continue
            state = await self._build_state_for_role(role)
            await self.channel_layer.group_send(self.group_name, {
                'type':        'broadcast_state_update',
                'target_role': role,
                'payload': {
                    'type':      'board_update',
                    'phases':    phases,
                    'connected': connected,
                    **state,
                },
            })

    # Downstream/upstream map used for targeted partner notifications
    _DOWNSTREAM = {
        'retailer': 'customer', 'wholesaler': 'retailer',
        'distributor': 'wholesaler', 'factory': 'distributor',
    }
    _UPSTREAM = {
        'retailer': 'wholesaler', 'wholesaler': 'distributor',
        'distributor': 'factory',
    }

    async def _notify_shipment_received(self, acting_role, received_qty):
        """
        After confirm_receive: tell the upstream partner that their shipment was delivered.
        Factory confirms production arriving to itself — no external upstream to notify.
        """
        upstream_role = self._UPSTREAM.get(acting_role)
        if not upstream_role:
            return
        await self.channel_layer.group_send(self.group_name, {
            'type':        'broadcast_state_update',
            'target_role': upstream_role,
            'payload': {
                'type':     'shipment_received',
                'by_role':  acting_role,
                'quantity': received_qty,
            },
        })

    async def _notify_shipment_dispatched(self, acting_role, shipped, arrives_week):
        """
        After confirm_ship: tell the downstream partner that a shipment is on its way.
        The customer has no WebSocket panel, so we skip that case.
        """
        downstream_role = self._DOWNSTREAM.get(acting_role)
        if not downstream_role or downstream_role == 'customer':
            return
        await self.channel_layer.group_send(self.group_name, {
            'type':        'broadcast_state_update',
            'target_role': downstream_role,
            'payload': {
                'type':         'shipment_incoming',
                'from_role':    acting_role,
                'quantity':     shipped,
                'arrives_week': arrives_week,
            },
        })

    async def _notify_order_placed(self, acting_role, qty, arrives_week):
        """
        After submit_order: tell the upstream partner that a new order is heading their way.
        Factory self-orders have no external upstream to notify.
        """
        upstream_role = self._UPSTREAM.get(acting_role)
        if not upstream_role:
            return
        await self.channel_layer.group_send(self.group_name, {
            'type':        'broadcast_state_update',
            'target_role': upstream_role,
            'payload': {
                'type':         'order_incoming',
                'from_role':    acting_role,
                'quantity':     qty,
                'arrives_week': arrives_week,
            },
        })

    async def _broadcast_all_phases_done(self):
        """Notify all players that everyone has completed their phases."""
        await self.channel_layer.group_send(self.group_name, {
            'type': 'broadcast_all_done',
        })

    async def broadcast_all_done(self, event):
        """Received by each client — tells them to show the week-ready button."""
        await self.send(text_data=json.dumps({'type': 'all_phases_done'}))

    # ── Open week (stage data for all non-customer roles) ────────────────────

    async def _do_open_week(self):
        """Stage the new week and push phase_receive to each non-customer role."""
        staging = await database_sync_to_async(open_week)(
            await self._get_session()
        )

        # Auto-complete all phases for AI-managed roles immediately after staging.
        ai_roles = await self._get_ai_roles()
        for role in ai_roles:
            await self._ai_complete_role_async(role)

        for role in NON_CUSTOMER_ROLES:
            if role in ai_roles:
                continue  # AI already handled — no need to send phase_receive
            s     = staging.get(role, {})
            state = await self._build_state_for_role(role)
            await self.channel_layer.group_send(self.group_name, {
                'type':        'broadcast_state_update',
                'target_role': role,
                'payload': {
                    'type':               'phase_receive',
                    **state,
                    # For all roles: units arriving from upstream shipments this week
                    'pending_received':   s.get('received', 0),
                    # For non-factory: incoming order qty from downstream partner
                    # For factory: distributor's real order arriving this week
                    'demand_incoming':    (
                        s.get('distributor_order', 0)
                        if role == 'factory'
                        else s.get('order_qty', 0)
                    ),
                    # Factory-specific: how many units the production request converts (starts production)
                    'production_request': s.get('production_request', 0) if role == 'factory' else None,
                },
            })

        # Send week_open to customer so they can submit new demand
        cust_state = await self._build_state_for_role('customer')
        await self.channel_layer.group_send(self.group_name, {
            'type':        'broadcast_state_update',
            'target_role': 'customer',
            'payload': {'type': 'week_open', **cust_state},
        })

        # If all roles (including newly AI-completed ones) are done and week_ready,
        # trigger an immediate week advance so human players aren't stuck waiting.
        if ai_roles:
            if await self._all_phase_done():
                await self._broadcast_all_phases_done()
            if await self._all_week_ready():
                await self._do_close_week()

    # ── Close week ────────────────────────────────────────────────────────────

    async def _do_close_week(self):
        session = await self._get_session()
        summary = await database_sync_to_async(close_week)(session)
        if not summary:
            # Already closed by another consumer (idempotency guard triggered).
            return
        session = await self._get_session()

        # ── Store each player's summary in DB for reconnecting players ────────
        await self._save_week_summaries(summary)

        if session.is_finished:
            await self._set_status(GameSession.STATUS_FINISHED)
            await self.channel_layer.group_send(self.group_name, {
                'type': 'broadcast_game_over',
                'session_id': session.id,
            })
            return

        # ── Broadcast week_summary to every role before opening next week ─────
        for role in ALL_ROLES:
            role_summary = summary.get(role, {})
            if not role_summary:
                continue
            await self.channel_layer.group_send(self.group_name, {
                'type':         'broadcast_week_summary',
                'target_role':  role,
                'week_summary': role_summary,
                'week_number':  session.current_week,   # already incremented
            })

        # Brief pause so the summary modal shows before the next week floods in
        await asyncio.sleep(0.3)

        # ── Broadcast state update + open next week ───────────────────────────
        for role in ALL_ROLES:
            state = await self._build_state_for_role(role)
            await self.channel_layer.group_send(self.group_name, {
                'type':        'broadcast_state_update',
                'target_role': role,
                'payload': {
                    'type': 'week_complete',
                    **state,
                    'week_summary': summary.get(role, {}),
                },
            })

        await self._do_open_week()
    async def broadcast_week_summary(self, event):
        if event['target_role'] == self.player_session.role:
            await self.send(text_data=json.dumps({
                'type':         'week_summary',
                'week_summary': event['week_summary'],
                'week':         event['week_number'],
            }))

    # ── Helpers ───────────────────────────────────────────────────────────────
    @database_sync_to_async
    def _save_disconnect_time(self):
        PlayerSession.objects.filter(token=self.token).update(
            disconnected_at=timezone.now()
        )

    @database_sync_to_async
    def _get_last_week_summary(self):
        ps = PlayerSession.objects.filter(token=self.token).first()
        if ps and ps.last_week_summary:
            summary = ps.last_week_summary
            # Clear it after delivering so it only shows once
            PlayerSession.objects.filter(token=self.token).update(last_week_summary=None)
            return summary
        return None

    @database_sync_to_async
    def _save_week_summaries(self, summary):
        """Store each role's week summary in their PlayerSession for reconnection."""
        for role, data in summary.items():
            PlayerSession.objects.filter(
                game_session_id=self.session_id, role=role
            ).update(last_week_summary=data)
    async def _maybe_broadcast_updated_demand(self, customer_qty):
        """
        After customer submits, push updated demand_incoming to retailer
        so they see the real customer order immediately on their board.
        """
        ps_retailer = await database_sync_to_async(
            lambda: self.player_session.game_session.player_sessions
                    .filter(role='retailer').first()
        )()
        if ps_retailer:
            await self.channel_layer.group_send(self.group_name, {
                'type':        'broadcast_state_update',
                'target_role': 'retailer',
                'payload': {
                    'type':            'demand_update',
                    'demand_incoming': customer_qty,
                },
            })

    async def _handle_set_name(self, data):
        name = str(data.get('name', '')).strip()[:50]
        if name:
            await self._save_name(name)
            await self.channel_layer.group_send(self.group_name, {
                'type': 'broadcast_player_joined',
                'role': self.player_session.role,
                'name': name,
            })

    async def _send_error(self, msg):
        await self.send(text_data=json.dumps({'type': 'error', 'message': msg}))

    async def _broadcast_ready_status(self):
        session   = await self._get_session()
        connected = await self._get_connected_roles()
        phases    = await self._get_all_phases()

        payload = {
            'submitted': session.submitted_role_list,
            'connected': connected,
            'ready':     session.ready_role_list,
            'status':    session.status,
            'total':     5,
            'phases':    phases,
        }
        # Broadcast to ALL players in the group (including self)
        await self.channel_layer.group_send(self.group_name, {
            'type': 'broadcast_ready_status',
            **payload,
        })

    # ── Channel layer handlers ────────────────────────────────────────────────

    async def broadcast_state_update(self, event):
        if event['target_role'] == self.player_session.role:
            await self.send(text_data=json.dumps(event['payload']))

    async def broadcast_player_joined(self, event):
        await self.send(text_data=json.dumps(
            {'type': 'player_joined', 'role': event['role'], 'name': event['name']}))

    async def broadcast_player_left(self, event):
        await self.send(text_data=json.dumps(
            {'type': 'player_left', 'role': event['role'], 'name': event['name']}))

    async def broadcast_ready_status(self, event):
        await self.send(text_data=json.dumps({
            'type':      'ready_status',
            'submitted': event.get('submitted', []),
            'connected': event.get('connected', []),
            'ready':     event.get('ready', []),
            'status':    event.get('status', 'lobby'),
            'total':     event.get('total', 5),
            'phases':    event.get('phases', {}),
        }))

    async def broadcast_game_over(self, event):
        await self.send(text_data=json.dumps({
            'type':        'game_over',
            'results_url': f"/game/{event['session_id']}/results/",
        }))

    async def trigger_week_advance(self, event):
        """
        Sent by the HTTP ai_replace_role view when all phases + week_ready flags
        are set.  Each consumer receiving this checks whether the week can be
        closed; close_week's select_for_update guard ensures only one succeeds.
        """
        if await self._all_week_ready():
            await self._do_close_week()

    @database_sync_to_async
    def _get_factory_distributor_order(self):
        """
        Return the sum of distributor PipelineOrders arriving at the factory
        this week (current_week + 1) that have not yet been fulfilled.
        Used during PHASE_RECEIVE reconnect for the factory role so the
        phase_receive panel shows the correct incoming demand (not the
        factory's own production-request quantity).
        """
        from .models import PipelineOrder, GameSession as GS
        session = GS.objects.get(id=self.session_id)
        week    = session.current_week + 1
        factory_player = session.players.filter(role='factory').first()
        if not factory_player:
            return 0
        distributor = factory_player.get_downstream()   # distributor
        if not distributor:
            return 0
        return sum(
            o.quantity for o in PipelineOrder.objects.filter(
                sender=distributor, arrives_on_week=week, fulfilled=False
            )
        )

    @database_sync_to_async
    def _get_ai_roles(self):
        """Return a list of non-customer roles marked as is_ai=True."""
        return list(
            PlayerSession.objects.filter(
                game_session_id=self.session_id, is_ai=True
            ).exclude(role='customer').values_list('role', flat=True)
        )

    @database_sync_to_async
    def _ai_complete_role_async(self, role):
        """Complete all phases for an AI-managed role (database_sync_to_async wrapper)."""
        session = GameSession.objects.get(id=self.session_id)
        ai_complete_role(session, role)

    @database_sync_to_async
    def _get_player_session(self):
        try:
            return PlayerSession.objects.select_related('game_session').get(
                token=self.token, game_session_id=self.session_id)
        except PlayerSession.DoesNotExist:
            return None

    @database_sync_to_async
    def _get_session(self):
        return GameSession.objects.get(id=self.session_id)

    @database_sync_to_async
    def _set_connected(self, value):
        PlayerSession.objects.filter(token=self.token).update(is_connected=value)
        self.player_session.is_connected = value

    @database_sync_to_async
    def _set_status(self, status):
        GameSession.objects.filter(id=self.session_id).update(status=status)

    @database_sync_to_async
    def _set_customer_demand(self, qty):
        GameSession.objects.filter(id=self.session_id).update(pending_customer_demand=qty)

    @database_sync_to_async
    def _update_retailer_demand(self, qty):
        PlayerSession.objects.filter(
            game_session_id=self.session_id, role='retailer'
        ).update(pending_order_qty=qty)

    @database_sync_to_async
    def _set_phase(self, phase):
        PlayerSession.objects.filter(token=self.token).update(turn_phase=phase)
        self.player_session.turn_phase = phase

    @database_sync_to_async
    def _save_name(self, name):
        PlayerSession.objects.filter(token=self.token).update(name=name)
        self.player_session.name = name

    @database_sync_to_async
    def _mark_ready(self, session):
        session.mark_ready(self.player_session.role)

    @database_sync_to_async
    def _all_ready(self):
        session = GameSession.objects.get(id=self.session_id)
        required = set(session.player_sessions.values_list('role', flat=True))
        if not required:
            return True
        return required <= set(session.ready_role_list)

    @database_sync_to_async
    def _all_phase_done(self):
        """All PlayerSessions (including customer) at PHASE_DONE."""
        required = set(
            GameSession.objects.get(id=self.session_id)
            .player_sessions.values_list('role', flat=True)
        )
        if not required:
            return True
        done = set(
            PlayerSession.objects.filter(
                game_session_id=self.session_id,
                turn_phase=PlayerSession.PHASE_DONE,
            ).values_list('role', flat=True)
        )
        return required <= done

    @database_sync_to_async
    def _get_connected_roles(self):
        return list(PlayerSession.objects.filter(
            game_session_id=self.session_id, is_connected=True
        ).values_list('role', flat=True))

    @database_sync_to_async
    def _get_ready_roles(self):
        session = GameSession.objects.get(id=self.session_id)
        return session.ready_role_list

    @database_sync_to_async
    def _get_all_phases(self):
        return {
            ps.role: ps.turn_phase
            for ps in PlayerSession.objects.filter(game_session_id=self.session_id)
        }

    @database_sync_to_async
    def _mark_week_ready_flag(self):
        """
        We reuse the GameSession.submitted_roles field to track who clicked
        'Prêt pour semaine suivante'. At phase_done we mark submitted; here
        we just need to know everyone is in PHASE_DONE (already true by guard).
        No extra field needed — _all_week_ready just checks all are PHASE_DONE
        AND have clicked (we flip them to a new sentinel via pending_order==-1).
        """
        PlayerSession.objects.filter(
            token=self.token
        ).update(pending_order=-1)   # sentinel: -1 = week_ready clicked

    @database_sync_to_async
    def _all_week_ready(self):
        """All connected PlayerSessions have clicked week_ready (pending_order==-1)."""
        required = set(
            GameSession.objects.get(id=self.session_id)
            .player_sessions.values_list('role', flat=True)
        )
        if not required:
            return True
        ready = set(
            PlayerSession.objects.filter(
                game_session_id=self.session_id,
                pending_order=-1,
            ).values_list('role', flat=True)
        )
        return required <= ready

    async def _broadcast_week_ready_status(self):
        """Tell everyone how many players have clicked week_ready."""
        ready_roles = await database_sync_to_async(
            lambda: list(PlayerSession.objects.filter(
                game_session_id=self.session_id,
                pending_order=-1,
            ).values_list('role', flat=True))
        )()
        total = await database_sync_to_async(
            lambda: PlayerSession.objects.filter(
                game_session_id=self.session_id
            ).count()
        )()
        await self.channel_layer.group_send(self.group_name, {
            'type':           'broadcast_week_ready',
            'ready_for_next': ready_roles,
            'total':          total,
        })

    async def broadcast_week_ready(self, event):
        await self.send(text_data=json.dumps({
            'type':           'week_ready_status',
            'ready_for_next': event['ready_for_next'],
            'total':          event['total'],
        }))

    @database_sync_to_async
    def _build_state_for_role(self, role):
        from .models import PipelineShipment, PipelineOrder, CustomerDemand

        # Single query with all related data prefetched
        session = (GameSession.objects
                   .prefetch_related('players', 'player_sessions', 'demands')
                   .get(id=self.session_id))

        players = {p.role: p for p in session.players.all()}

        upstream_map = {
            "retailer":"wholesaler","wholesaler":"distributor",
            "distributor":"factory","factory":None,"customer":"retailer",
        }
        downstream_map = {
            "customer":None,"retailer":"customer","wholesaler":"retailer",
            "distributor":"wholesaler","factory":"distributor",
        }

        # The "playing week" is always current_week + 1 because
        # session.current_week stores the last *completed* week.
        playing_week = session.current_week + 1

        def _two_items(rows):
            return [
                {
                    "quantity":     r.quantity,
                    "arrives_week": r.arrives_on_week,
                    "weeks_away":   max(0, r.arrives_on_week - playing_week),
                }
                for r in rows[:2]
            ]

        if role == 'customer':
            retailer = players.get('retailer')
            demand_history = list(
                session.demands.order_by('week').values('week', 'quantity')
            )
            return {
                'role':            'customer',
                'week':            session.current_week,
                'max_weeks':       session.max_weeks,
                'is_finished':     session.is_finished,
                'status':          session.status,
                'submitted':       session.submitted_role_list,
                'ready':           session.ready_role_list,
                'demand_history':  demand_history,
                'retailer_backlog': retailer.backlog if retailer else 0,
                'pending_demand':  session.pending_customer_demand,
                'map': {
                    'demand_customer_to_retailer': {
                        'this_week': session.pending_customer_demand,
                        'last_week': next(
                            (d['quantity'] for d in demand_history
                             if d['week'] == session.current_week - 1), None
                        ),
                    }
                }
            }

        viewer_player = players.get(role)
        if not viewer_player:
            return {'role': role, 'week': session.current_week, 'own': {}}

        own_data = {
            'inventory':  viewer_player.inventory,
            'backlog':    viewer_player.backlog,
            'total_cost': viewer_player.total_cost,
        }

        # Incoming shipments to me
        incoming_ships_qs = list(PipelineShipment.objects.filter(
            receiver=viewer_player, delivered=False
        ).order_by('arrives_on_week'))

        incoming_shipments = [
            {
                'quantity':     s.quantity,
                'arrives_week': s.arrives_on_week,
                'weeks_away':   max(0, s.arrives_on_week - playing_week),
            }
            for s in incoming_ships_qs
        ]

        # Outgoing orders from me (includes factory self-orders)
        outgoing_orders_qs = list(PipelineOrder.objects.filter(
            sender=viewer_player, fulfilled=False
        ).order_by('arrives_on_week'))

        outgoing_orders = [
            {
                'quantity':     o.quantity,
                'arrives_week': o.arrives_on_week,
                'weeks_away':   max(0, o.arrives_on_week - playing_week),
            }
            for o in outgoing_orders_qs
        ]

        history = list(viewer_player.history.values(
            'week', 'inventory', 'backlog', 'order_placed',
            'shipment_received', 'cost_this_week', 'cumulative_cost'
        ))

        # Map data
        upstream_role   = upstream_map.get(role)
        downstream_role = downstream_map.get(role)
        upstream_player   = players.get(upstream_role)   if upstream_role else None
        downstream_player = players.get(downstream_role) if downstream_role and downstream_role != 'customer' else None

        demand_customer_to_retailer = None
        if role == 'retailer':
            last_demand = next(
                (d['quantity'] for d in list(session.demands.order_by('-week').values('week','quantity'))
                 if d['week'] == session.current_week - 1), None
            )
            demand_customer_to_retailer = {
                'this_week': session.pending_customer_demand,
                'last_week': last_demand,
            }
        # Demand information is intentionally omitted for non-retailer roles
        # to preserve the MIT Beer Game information-hiding rule.

        incoming_orders_to_me       = None
        outgoing_shipments_from_me  = None
        incoming_shipments_to_me_map = None
        outgoing_orders_from_me_map = None
        factory_pending_requests    = None
        factory_production_delay    = None

        if role == 'factory':
            factory_pending_requests = _two_items(outgoing_orders_qs)
            factory_production_delay = _two_items(incoming_ships_qs)
            # The factory's "incoming orders" are the distributor's PipelineOrders
            # in transit — include all unfulfilled orders so the board pipeline is visible.
            # Quantities for future orders (weeks_away > 0) are hidden on the client side.
            distributor_player = players.get('distributor')
            if distributor_player:
                incoming_orders_to_me = _two_items(list(PipelineOrder.objects.filter(
                    sender=distributor_player, fulfilled=False,
                ).order_by('arrives_on_week')))
            else:
                incoming_orders_to_me = []

        elif role in ('wholesaler', 'distributor'):
            if downstream_player:
                # Show all in-transit orders from downstream so the pipeline
                # is visible on the board. Quantities for future orders
                # (weeks_away > 0) are hidden on the client side.
                incoming_orders_to_me = _two_items(list(PipelineOrder.objects.filter(
                    sender=downstream_player, fulfilled=False,
                ).order_by('arrives_on_week')))

        if role in ('wholesaler', 'distributor', 'factory') and downstream_player:
            outgoing_shipments_from_me = _two_items(list(PipelineShipment.objects.filter(
                receiver=downstream_player, delivered=False
            ).order_by('arrives_on_week')))

        if role in ('retailer', 'wholesaler', 'distributor'):
            incoming_shipments_to_me_map = _two_items(incoming_ships_qs)
            outgoing_orders_from_me_map  = _two_items(outgoing_orders_qs)

        # Phase data
        ps = next((p for p in session.player_sessions.all() if p.role == role), None)
        phase_data = {}
        if ps:
            phase_data = {
                'turn_phase':           ps.turn_phase,
                'pending_received_qty': ps.pending_received_qty,
                'pending_order_qty':    ps.pending_order_qty,
                'pending_ship_qty':     ps.pending_ship_qty,
            }

        return {
            'role':            role,
            'week':            session.current_week,
            'max_weeks':       session.max_weeks,
            'is_finished':     session.is_finished,
            'status':          session.status,
            'own':             own_data,
            'pipeline':        incoming_shipments,
            'outgoing_orders': outgoing_orders,
            'history':         history,
            'submitted':       session.submitted_role_list,
            'ready':           session.ready_role_list,
            **phase_data,
            'map': {
                'demand_customer_to_retailer':  demand_customer_to_retailer,
                'incoming_orders_to_me':        incoming_orders_to_me,
                'outgoing_orders_from_me':      outgoing_orders_from_me_map,
                'incoming_shipments_to_me':     incoming_shipments_to_me_map,
                'outgoing_shipments_from_me':   outgoing_shipments_from_me,
                'factory_pending_requests':     factory_pending_requests,
                'factory_production_delay':     factory_production_delay,
            }
        }

