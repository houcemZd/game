import csv
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from .models import (
    GameSession, Player, CustomerDemand,
    PlayerSession, PipelineShipment, PipelineOrder,
    LobbyMessage,
)
from .services import (
    initialise_session, process_week, get_chart_data, get_bullwhip_data,
    get_advanced_analytics, _ai_order, ai_complete_role, get_scheduled_demand,
)

CHAIN_ORDER  = {'retailer': 1, 'wholesaler': 2, 'distributor': 3, 'factory': 4}
ROLE_EMOJIS  = {
    'customer':    '👤',
    'retailer':    '🛒',
    'wholesaler':  '🏪',
    'distributor': '🚚',
    'factory':     '🏭',
}
SUPPLY_ROLES = ['retailer', 'wholesaler', 'distributor', 'factory']
ALL_ROLES    = ['customer', 'retailer', 'wholesaler', 'distributor', 'factory']


# ── Authorization helpers ─────────────────────────────────────────────────────

def _is_session_creator(request, session):
    """Return True if the logged-in user created this session.

    If created_by is None (legacy sessions without an owner) we allow access so
    existing games are not inadvertently locked out.
    """
    if session.created_by is None:
        return True
    return session.created_by_id == request.user.pk


def _is_session_member(request, session):
    """Return True if the user is creator OR holds a player slot in this session."""
    if _is_session_creator(request, session):
        return True
    return session.player_sessions.filter(user=request.user).exists()


def _require_creator(request, session):
    """Return a 403 response if the user is not the session creator, else None."""
    if not _is_session_creator(request, session):
        return HttpResponseForbidden("Only the session creator can perform this action.")
    return None


def _require_member(request, session):
    """Return a 403 response if the user has no stake in the session, else None."""
    if not _is_session_member(request, session):
        return HttpResponseForbidden("You are not a member of this game session.")
    return None


def _sorted_players(players):
    return sorted(players, key=lambda p: CHAIN_ORDER.get(p.role, 99))


def _build_pipeline_data(players, current_week):
    data = []
    for player in players:
        upstream = player.get_upstream()
        for s in PipelineShipment.objects.filter(
            receiver=player, delivered=False
        ).order_by('arrives_on_week'):
            data.append({
                'from':       upstream.role if upstream else player.role,
                'to':         player.role,
                'qty':        s.quantity,
                'arrives':    s.arrives_on_week,
                'weeks_away': max(0, s.arrives_on_week - current_week),
                'type':       'ship' if player.role in ('distributor', 'wholesaler') else 'truck',
            })
        if player.role != 'factory':
            for o in PipelineOrder.objects.filter(
                sender=player, fulfilled=False
            ).order_by('arrives_on_week'):
                data.append({
                    'from':       player.role,
                    'to':         upstream.role if upstream else player.role,
                    'qty':        o.quantity,
                    'arrives':    o.arrives_on_week,
                    'weeks_away': max(0, o.arrives_on_week - current_week),
                    'type':       'order',
                })
    return data


# ── Home ──────────────────────────────────────────────────────────────────────
@login_required
def home(request):
    # Limit to 100 most recent sessions to avoid unbounded memory / timeout.
    all_sessions = list(
        GameSession.objects.select_related('created_by')
        .prefetch_related('player_sessions')
        .order_by('-created_at')[:100]
    )

    # Open multiplayer lobbies anyone can join
    lobby_sessions = [
        s for s in all_sessions
        if s.status == GameSession.STATUS_LOBBY and s.player_sessions.exists()
    ]
    for s in lobby_sessions:
        ordered_slots = sorted(
            s.player_sessions.all(),
            key=lambda ps: ALL_ROLES.index(ps.role) if ps.role in ALL_ROLES else 999,
        )
        # Prefer an unclaimed slot; fallback to first slot for legacy sessions.
        join_slot = next((ps for ps in ordered_slots if ps.user_id is None), None)
        if not join_slot and ordered_slots:
            join_slot = ordered_slots[0]
        s.public_join_token = join_slot.token if join_slot else None
    # Active multiplayer games (spectate/observe)
    active_sessions = [
        s for s in all_sessions
        if s.status == GameSession.STATUS_PLAYING and s.player_sessions.exists()
    ]
    # Sessions created by this user (solo or theirs)
    my_sessions = [
        s for s in all_sessions
        if s.created_by == request.user
    ]

    total_count    = GameSession.objects.count()
    finished_count = GameSession.objects.filter(status=GameSession.STATUS_FINISHED).count()
    stats = {
        'total':    total_count,
        'active':   len(active_sessions),
        'lobby':    len(lobby_sessions),
        'finished': finished_count,
    }
    weeks_options = [(12, 'short'), (20, 'standard'), (30, 'long'), (40, 'extended')]

    return render(request, 'game/home.html', {
        'lobby_sessions':  lobby_sessions,
        'active_sessions': active_sessions,
        'my_sessions':     my_sessions,
        'stats':           stats,
        'weeks_options':   weeks_options,
    })


# ── New game ──────────────────────────────────────────────────────────────────
@login_required
def new_game(request):
    if request.method == 'POST':
        name      = request.POST.get('name', 'Beer Game').strip() or 'Beer Game'
        max_weeks = int(request.POST.get('max_weeks', 20))
        max_weeks = max(12, min(40, max_weeks))
        mode      = request.POST.get('mode', 'single')

        session = GameSession.objects.create(
            name=name,
            max_weeks=max_weeks,
            status=GameSession.STATUS_LOBBY,
            created_by=request.user,          # ← NEW: track creator
        )
        for player_name, role in [
            ('Retailer','retailer'), ('Wholesaler','wholesaler'),
            ('Distributor','distributor'), ('Factory','factory'),
        ]:
            Player.objects.create(session=session, name=player_name, role=role)

        if mode == 'multi':
            for role in ALL_ROLES:
                PlayerSession.objects.create(game_session=session, role=role)

        return redirect('game_init', session_id=session.id)

    return render(request, 'game/new_game.html')

# ── Initialisation step ───────────────────────────────────────────────────────
@login_required
def game_init(request, session_id):
    """
    Step 2: configure initial state before the game starts.
    Player sets: initial inventory, orders placed (pipeline), incoming orders (pipeline).
    """
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_creator(request, session)
    if denied:
        return denied

    if request.method == 'POST':
        # Read initial parameters from form
        init_inventory     = max(0, int(request.POST.get('init_inventory', 12)))
        init_orders_placed = max(0, int(request.POST.get('init_orders_placed', 4)))
        init_incoming      = max(0, int(request.POST.get('init_incoming', 4)))
        holding_cost       = float(request.POST.get('holding_cost', 0.5))
        backlog_cost       = float(request.POST.get('backlog_cost', 1.0))

        # --- Demand schedule ---
        demand_mode = request.POST.get('demand_mode', 'manual')
        if demand_mode == 'classic':
            demand_schedule = 'classic'
        elif demand_mode == 'custom':
            raw = request.POST.get('demand_custom_values', '').strip()
            try:
                values = [max(0, int(v.strip())) for v in raw.split(',') if v.strip()]
                demand_schedule = values if values else 'classic'
            except ValueError:
                demand_schedule = 'classic'
        else:
            demand_schedule = None  # manual — customer player submits demand each week

        # Apply inventory + costs to all players
        for player in session.players.all():
            player.inventory    = init_inventory
            player.holding_cost = holding_cost
            player.backlog_cost = backlog_cost
            player.save()

        # Save demand schedule on session
        session.demand_schedule = demand_schedule
        session.save(update_fields=['demand_schedule'])

        # Call initialise_session with custom pipeline values
        initialise_session(
            session,
            init_orders_placed=init_orders_placed,
            init_incoming=init_incoming,
        )

        # Transition to correct status
        has_player_sessions = session.player_sessions.exists()
        if has_player_sessions:
            session.status = GameSession.STATUS_LOBBY
            session.save(update_fields=['status'])
            return redirect('lobby', session_id=session.id)
        else:
            session.status = GameSession.STATUS_PLAYING
            session.save(update_fields=['status'])
            return redirect('dashboard', session_id=session.id)

    return render(request, 'game/game_init.html', {'session': session})


# ── Lobby ─────────────────────────────────────────────────────────────────────
@login_required
def lobby(request, session_id):
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_member(request, session)
    if denied:
        return denied
    role_links = []
    for ps in sorted(session.player_sessions.all(), key=lambda p: ALL_ROLES.index(p.role)):
        join_url = request.build_absolute_uri(f'/join/{ps.token}/')
        role_links.append({
            'role':     ps.role,
            'emoji':    ROLE_EMOJIS.get(ps.role, ''),
            'token':    ps.token,
            'url':      join_url,
        })

    # Initial board state for the preview — separate orders vs shipments
    initial_players  = _sorted_players(session.players.all())
    first_order = PipelineOrder.objects.filter(
        sender__session=session
    ).first()
    first_ship  = PipelineShipment.objects.filter(
        receiver__session=session
    ).first()
    initial_orders   = first_order.quantity if first_order else 4
    initial_ships    = first_ship.quantity  if first_ship  else 4
    initial_inv      = initial_players[0].inventory if initial_players else 12

    # Game settings for the info card
    first_player = initial_players[0] if initial_players else None
    game_settings = {
        'initial_inventory': first_player.inventory if first_player else 12,
        'holding_cost':      first_player.holding_cost if first_player else 0.5,
        'backlog_cost':      first_player.backlog_cost if first_player else 1.0,
        'max_weeks':         session.max_weeks,
        'pipeline_delay':    2,
    }

    is_host = (session.created_by == request.user)

    return render(request, 'game/lobby.html', {
        'session':         session,
        'role_links':      role_links,
        'initial_players': initial_players,
        'initial_orders':  initial_orders,
        'initial_ships':   initial_ships,
        'initial_inv':     initial_inv,
        'game_settings':   game_settings,
        'is_host':         is_host,
    })


# ── Lobby status API (polled by lobby.html every 2s) ──────────────────────────
@login_required
def lobby_status(request, session_id):
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_member(request, session)
    if denied:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    player_sessions = list(session.player_sessions.all())
    joined      = [ps.role for ps in player_sessions if ps.name]
    connected   = [ps.role for ps in player_sessions if ps.is_connected]
    names       = {ps.role: ps.name for ps in player_sessions if ps.name}
    game_started = session.status == GameSession.STATUS_PLAYING
    is_finished  = session.status == GameSession.STATUS_FINISHED

    # Ready roles (players who have marked themselves ready in the lobby)
    ready_roles = session.ready_role_list

    # Host info
    is_host = (session.created_by == request.user)

    # Include live player board data so the lobby can act as a spectator view
    players_data = []
    if game_started or is_finished:
        for player in _sorted_players(session.players.all()):
            last_state = player.history.order_by('-week').first()
            players_data.append({
                'role':         player.role,
                'name':         player.name,
                'inventory':    player.inventory,
                'backlog':      player.backlog,
                'total_cost':   round(player.total_cost, 1),
                'order_placed': last_state.order_placed if last_state else 0,
            })

    # Last customer demand (useful for spectators)
    last_demand = None
    if game_started or is_finished:
        d = CustomerDemand.objects.filter(session=session, week=session.current_week).first()
        if d:
            last_demand = d.quantity

    # Full pipeline data for supply-chain board
    pipeline_data = []
    if game_started or is_finished:
        pipeline_data = _build_pipeline_data(
            _sorted_players(session.players.all()),
            session.current_week + 1,
        )

    # Recent chat messages (last 50)
    chat_messages = []
    for msg in session.lobby_messages.order_by('-created_at')[:50]:
        chat_messages.append({
            'id':     msg.id,
            'author': msg.author_name,
            'role':   msg.author_role,
            'body':   msg.body,
            'time':   msg.created_at.strftime('%H:%M'),
        })
    chat_messages.reverse()

    return JsonResponse({
        'joined':        joined,
        'connected':     connected,
        'names':         names,
        'ready':         ready_roles,
        'game_started':  game_started,
        'is_finished':   is_finished,
        'status':        session.status,
        'current_week':  session.current_week,
        'max_weeks':     session.max_weeks,
        'players':       players_data,
        'last_demand':   last_demand,
        'pipeline':      pipeline_data,
        'is_host':       is_host,
        'chat':          chat_messages,
        # Phase status for each role (used by instructor view)
        'phases': {
            ps.role: ps.turn_phase
            for ps in session.player_sessions.all()
        },
        # AI-managed roles
        'ai_roles': [
            ps.role for ps in session.player_sessions.all() if ps.is_ai
        ],
    })


# ── Join ──────────────────────────────────────────────────────────────────────
@login_required
def join_game(request, token):
    """
    Players join via a role-specific token link (shared by the session creator).
    If the user is authenticated, we link the PlayerSession to their account.
    """
    ps      = get_object_or_404(PlayerSession, token=token)
    session = ps.game_session

    # If this role is already claimed by a different user, block it
    if ps.user and request.user.is_authenticated and ps.user != request.user:
        return render(request, 'game/join_taken.html', {
            'session': session,
            'role':    ps.role,
            'claimed_by': ps.user.get_full_name() or ps.user.username,
        })

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()[:50]

        # Use account name as default if logged in and no name given
        if not name and request.user.is_authenticated:
            name = request.user.first_name or request.user.username

        update_fields = []
        if name:
            ps.name = name
            update_fields.append('name')

        # Claim role for this user
        if request.user.is_authenticated and not ps.user:
            ps.user = request.user
            update_fields.append('user')

        if update_fields:
            ps.save(update_fields=update_fields)

        request.session['player_token'] = token
        if ps.role == 'customer':
            return redirect(f"{reverse('customer_play', args=[session.id])}?token={token}")
        return redirect(f"{reverse('play', args=[session.id])}?token={token}")

    # Pre-fill name from account
    default_name = ''
    if request.user.is_authenticated:
        default_name = request.user.first_name or request.user.username

    return render(request, 'game/join.html', {
        'session':      session,
        'role':         ps.role,
        'emoji':        ROLE_EMOJIS.get(ps.role, ''),
        'token':        token,
        'default_name': default_name,
        'already_claimed': bool(ps.user and ps.user == request.user),
    })

# ── Start game (host-only) ────────────────────────────────────────────────────
@login_required
@require_POST
def lobby_start_game(request, session_id):
    """Allow the host to start the game directly from the lobby."""
    session = get_object_or_404(GameSession, id=session_id)
    if session.created_by != request.user:
        return JsonResponse({'error': 'Only the host can start the game.'}, status=403)
    if session.status != GameSession.STATUS_LOBBY:
        return JsonResponse({'error': 'Game is not in lobby state.'}, status=400)
    # Require at least 2 roles to have joined
    joined_count = session.player_sessions.exclude(name='').count()
    if joined_count < 2:
        return JsonResponse({'error': 'At least 2 players must join before starting.'}, status=400)
    session.status = GameSession.STATUS_PLAYING
    session.save(update_fields=['status'])
    return JsonResponse({'ok': True, 'redirect': reverse('lobby', args=[session.id])})


# ── Lobby chat ────────────────────────────────────────────────────────────────
@login_required
@require_POST
def lobby_chat(request, session_id):
    """Post a chat message in the lobby."""
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_member(request, session)
    if denied:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    body = request.POST.get('body', '').strip()[:300]
    if not body:
        return JsonResponse({'error': 'Empty message.'}, status=400)
    author_name = request.user.first_name or request.user.username
    # Find role for this user in the session
    ps = session.player_sessions.filter(user=request.user).first()
    author_role = ps.role if ps else 'host'
    LobbyMessage.objects.create(
        game_session=session,
        author_name=author_name,
        author_role=author_role,
        body=body,
    )
    return JsonResponse({'ok': True})


# ── Multiplayer supply-chain player ───────────────────────────────────────────
@login_required
def play(request, session_id):
    # Accept token from URL param (cross-device) or session cookie (same-device)
    token = request.GET.get('token') or request.session.get('player_token')
    if not token:
        return redirect('home')
    ps = get_object_or_404(PlayerSession, token=token, game_session_id=session_id)
    session = ps.game_session
    # Store in session cookie so refreshes on same device stay authenticated
    request.session['player_token'] = token
    return render(request, 'game/play.html', {
        'session': session, 'player_session': ps,
        'role': ps.role, 'emoji': ROLE_EMOJIS.get(ps.role, ''),
        'token': token, 'roles': ALL_ROLES,
        'ws_path': f"/ws/game/{session_id}/{token}/",
    })


# ── Customer play (WebSocket, real-time) ──────────────────────────────────────
@login_required
def customer_play(request, session_id):
    token = request.GET.get('token') or request.session.get('player_token')
    if not token:
        return redirect('home')
    ps = get_object_or_404(PlayerSession, token=token, game_session_id=session_id, role='customer')
    session = ps.game_session
    request.session['player_token'] = token
    demand_history = list(CustomerDemand.objects.filter(session=session).order_by('week'))
    return render(request, 'game/customer_play.html', {
        'session': session, 'player_session': ps,
        'token': token,
        'demand_history': demand_history,
        'roles': ALL_ROLES,
        'ws_path': f"/ws/game/{session_id}/{token}/",
    })


# ── Single-player dashboard ───────────────────────────────────────────────────
@login_required
def dashboard(request, session_id):
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_member(request, session)
    if denied:
        return denied
    players = _sorted_players(session.players.prefetch_related('history').all())

    chart_data    = json.dumps(get_chart_data(session))
    pipeline_data = json.dumps(_build_pipeline_data(players, session.current_week + 1))

    last_week_states = {}
    if session.current_week > 0:
        for player in players:
            for state in player.history.all():
                if state.week == session.current_week:
                    last_week_states[player.role] = state
                    break

    # Last customer demand (for display + pre-filling next turn form)
    last_demand = CustomerDemand.objects.filter(
        session=session, week=session.current_week
    ).first()

    # AI-suggested orders for each player (shown as form defaults when no manual value)
    ai_orders = {player.id: _ai_order(player) for player in players} if not session.is_finished else {}

    return render(request, 'game/dashboard.html', {
        'session':          session,
        'players':          players,
        'chart_data':       chart_data,
        'pipeline_data':    pipeline_data,
        'last_week_states': last_week_states,
        'weeks_range':      range(1, session.current_week + 1),
        'roles':            SUPPLY_ROLES,
        'last_demand':      last_demand,
        'ai_orders':        ai_orders,
    })


# ── Single-player next turn (customer demand entered in form) ─────────────────
@require_POST
@login_required
def next_turn(request, session_id):
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_creator(request, session)
    if denied:
        return denied
    if not session.is_active or session.is_finished:
        return redirect('dashboard', session_id=session_id)

    # Customer demand (required)
    try:
        customer_qty = max(0, int(request.POST.get('customer_demand', '').strip()))
    except (ValueError, TypeError):
        customer_qty = 4

    # Store pending demand on the session so process_week can read it inside
    # the atomic transaction (select_for_update re-fetches, so we pre-save here).
    session.pending_customer_demand = customer_qty
    session.save(update_fields=['pending_customer_demand'])

    # Supply chain orders (only roles the user explicitly supplied)
    player_orders = {}
    for player in session.players.all():
        key = f'order_{player.id}'
        val = request.POST.get(key, '').strip()
        if val:
            try:
                player_orders[player.id] = max(0, int(val))
            except (ValueError, TypeError):
                pass

    process_week(session, player_orders)
    return redirect('dashboard', session_id=session_id)


# ── Per-role client view (read-only) ─────────────────────────────────────────
@login_required
def client_view(request, session_id, role):
    if role not in CHAIN_ORDER:
        return redirect('dashboard', session_id=session_id)
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_member(request, session)
    if denied:
        return denied
    player  = get_object_or_404(Player, session=session, role=role)
    upstream = player.get_upstream()
    incoming = list(PipelineShipment.objects.filter(
        receiver=player, delivered=False).order_by('arrives_on_week').values('quantity','arrives_on_week'))
    outgoing = []
    if role != 'factory':
        outgoing = list(PipelineOrder.objects.filter(
            sender=player, fulfilled=False).order_by('arrives_on_week').values('quantity','arrives_on_week'))
    history = list(player.history.order_by('week').values(
        'week','inventory','backlog','order_placed',
        'shipment_received','cost_this_week','cumulative_cost'))
    return render(request, 'game/client_view.html', {
        'session': session, 'player': player, 'role': role,
        'emoji': ROLE_EMOJIS.get(role,''), 'incoming': incoming, 'outgoing': outgoing,
        'history': json.dumps(history), 'roles': SUPPLY_ROLES,
        'role_emojis': ROLE_EMOJIS,
    })


# ── Customer view (single-player read/overview) ───────────────────────────────
@login_required
def customer_view(request, session_id):
    """Single-player customer overview — shows demand history and retailer state."""
    session  = get_object_or_404(GameSession, id=session_id)
    denied   = _require_member(request, session)
    if denied:
        return denied
    retailer = session.players.filter(role='retailer').first()
    demand_history = list(CustomerDemand.objects.filter(session=session).order_by('week'))
    return render(request, 'game/customer_view.html', {
        'session':         session,
        'retailer':        retailer,
        'demand_history':  demand_history,
        'retailer_inv':    retailer.inventory if retailer else 0,
        'retailer_backlog':retailer.backlog if retailer else 0,
        'total_demand':    sum(d.quantity for d in demand_history),
        'avg_demand':      round(sum(d.quantity for d in demand_history) / max(len(demand_history), 1), 1),
        'demand_history_json': json.dumps([{'week': d.week, 'qty': d.quantity} for d in demand_history]),
        'retailer_history_json': json.dumps(list(retailer.history.order_by('week').values('week','inventory','backlog')) if retailer else []),
        'demand_pattern':  request.session.get(f'demand_pattern_{session_id}', 'live'),
        'presets':         [2, 4, 6, 8, 10, 12],
        'next_week':       session.current_week + 1,
        'scheduled_demand': 0,
        'current_override': None,
        'overridden':       False,
    })



@require_POST
@login_required
def reset_game(request, session_id):
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_creator(request, session)
    if denied:
        return denied
    session.delete()
    return redirect('home')


# ── Delete via GET with confirmation token (for simple link-based delete) ────
@login_required
def delete_session(request, session_id):
    """GET: confirm page. POST: delete."""
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_creator(request, session)
    if denied:
        return denied
    if request.method == 'POST':
        session.delete()
        return redirect('home')
    return render(request, 'game/home.html', {
        'sessions': GameSession.objects.order_by('-created_at'),
        'confirm_delete': session,
    })


# ── Results ───────────────────────────────────────────────────────────────────
@login_required
def results(request, session_id):
    session    = get_object_or_404(GameSession, id=session_id)
    denied     = _require_member(request, session)
    if denied:
        return denied
    players    = _sorted_players(session.players.prefetch_related('history').all())
    chart_data = json.dumps(get_chart_data(session))
    bullwhip   = get_bullwhip_data(session)
    analytics  = get_advanced_analytics(session)
    total_cost = sum(p.total_cost for p in players)
    demand_history = list(CustomerDemand.objects.filter(session=session).order_by('week'))
    winner_role = min(players, key=lambda p: p.total_cost).role if players else None
    # Use max(actual max ratio, 5) so the bar scale is consistent across sessions
    # and the ratio=1 baseline marker always appears at ≤20% of the track width.
    bullwhip_max = max(max(bullwhip.values(), default=1), 5) if bullwhip else 5
    return render(request, 'game/results.html', {
        'session': session, 'players': players,
        'chart_data': chart_data, 'bullwhip': bullwhip,
        'total_cost': total_cost, 'demand_history': demand_history,
        'winner_role': winner_role,
        'bullwhip_max': bullwhip_max,
        'analytics': analytics,
    })


# ── Chart API ─────────────────────────────────────────────────────────────────
@login_required
def chart_data_api(request, session_id):
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_member(request, session)
    if denied:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    return JsonResponse(get_chart_data(session))


# ── CSV Export ────────────────────────────────────────────────────────────────
@login_required
def export_csv(request, session_id):
    """
    Download all WeeklyState rows for a session as a CSV file.
    Columns: week, role, inventory, backlog, order_placed, order_received,
             shipment_sent, shipment_received, cost_this_week, cumulative_cost,
             customer_demand.
    """
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_member(request, session)
    if denied:
        return denied

    demand_by_week = {
        d.week: d.quantity
        for d in CustomerDemand.objects.filter(session=session)
    }

    response = HttpResponse(content_type='text/csv')
    filename = f"beergame_{session.id}_w{session.current_week}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        'week', 'role', 'inventory', 'backlog',
        'order_placed', 'order_received',
        'shipment_sent', 'shipment_received',
        'cost_this_week', 'cumulative_cost',
        'customer_demand',
    ])

    for player in _sorted_players(session.players.prefetch_related('history').all()):
        for state in player.history.order_by('week'):
            writer.writerow([
                state.week,
                player.role,
                state.inventory,
                state.backlog,
                state.order_placed,
                state.order_received,
                state.shipment_sent,
                state.shipment_received,
                round(state.cost_this_week, 2),
                round(state.cumulative_cost, 2),
                demand_by_week.get(state.week, ''),
            ])

    return response


# ── Instructor Live Overview ───────────────────────────────────────────────────
@login_required
def instructor_view(request, session_id):
    """
    Read-only live overview of all roles for the instructor / session creator.
    The page auto-refreshes via JavaScript polling of lobby_status.
    """
    session = get_object_or_404(GameSession, id=session_id)
    denied  = _require_creator(request, session)
    if denied:
        return denied

    players   = _sorted_players(session.players.all())
    bullwhip  = get_bullwhip_data(session)
    bullwhip_max = max(max(bullwhip.values(), default=1), 5) if bullwhip else 5

    # Build per-role AI status map
    ai_roles = {
        ps.role: ps.is_ai
        for ps in session.player_sessions.all()
    }

    return render(request, 'game/instructor.html', {
        'session':       session,
        'players':       players,
        'bullwhip':      bullwhip,
        'bullwhip_max':  bullwhip_max,
        'ai_roles':      ai_roles,
        'supply_roles':  SUPPLY_ROLES,
        'role_emojis':   ROLE_EMOJIS,
    })


# ── AI Replace Role ───────────────────────────────────────────────────────────
@require_POST
@login_required
def ai_replace_role(request, session_id, role):
    """
    Host replaces a stuck supply-chain role with the AI base-stock policy.
    Marks the PlayerSession as is_ai=True and auto-completes any pending phases
    for the current week.  After completing, broadcasts updated status to all
    connected players via the channel layer.
    """
    session = get_object_or_404(GameSession, id=session_id)
    if session.created_by != request.user:
        return JsonResponse({'error': 'Only the host can replace a role with AI.'}, status=403)
    if session.status != GameSession.STATUS_PLAYING:
        return JsonResponse({'error': 'Game is not in progress.'}, status=400)
    if role not in SUPPLY_ROLES:
        return JsonResponse({'error': 'Invalid role.'}, status=400)

    ps = get_object_or_404(PlayerSession, game_session=session, role=role)
    ps.is_ai = True
    ps.save(update_fields=['is_ai'])

    # Auto-complete current week's phases using AI policy
    ai_complete_role(session, role)

    # Broadcast updated status via channel layer
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    channel_layer = get_channel_layer()
    group_name    = f"game_{session_id}"

    all_ps        = list(session.player_sessions.all())
    required_roles = {p.role for p in all_ps}
    done_roles     = {p.role for p in all_ps if p.turn_phase == PlayerSession.PHASE_DONE}
    week_ready_roles = {p.role for p in all_ps if p.pending_order == -1}
    connected      = [p.role for p in all_ps if p.is_connected]
    phases         = {p.role: p.turn_phase for p in all_ps}

    async_to_sync(channel_layer.group_send)(group_name, {
        'type':      'broadcast_ready_status',
        'submitted': session.submitted_role_list,
        'connected': connected,
        'ready':     session.ready_role_list,
        'status':    session.status,
        'total':     5,
        'phases':    phases,
    })

    if required_roles <= done_roles:
        async_to_sync(channel_layer.group_send)(group_name, {
            'type': 'broadcast_all_done',
        })

    # If all week_ready flags are set, ask consumers to check and close the week.
    if required_roles <= done_roles and required_roles <= week_ready_roles:
        async_to_sync(channel_layer.group_send)(group_name, {
            'type': 'trigger_week_advance',
        })

    return JsonResponse({'ok': True, 'role': role, 'is_ai': True})
