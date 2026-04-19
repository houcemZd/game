from django.db import models
from django.contrib.auth.models import User
import secrets


class GameSession(models.Model):
    STATUS_LOBBY    = 'lobby'
    STATUS_PLAYING  = 'playing'
    STATUS_FINISHED = 'finished'

    name         = models.CharField(max_length=100, default="Beer Game")
    current_week = models.IntegerField(default=0)
    max_weeks    = models.IntegerField(default=20)
    is_active    = models.BooleanField(default=True)
    status       = models.CharField(max_length=20, default=STATUS_LOBBY)
    created_at   = models.DateTimeField(auto_now_add=True)

    # Who created this session
    created_by   = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='created_games',
    )

    submitted_roles         = models.TextField(default='')
    ready_roles             = models.TextField(default='')
    pending_customer_demand = models.IntegerField(null=True, blank=True)

    # Demand schedule: None = manual (customer role enters demand each week);
    # "classic" = MIT step pattern (4 units weeks 1-4, then 8);
    # list of ints = custom per-week schedule (index 0 = week 1).
    demand_schedule = models.JSONField(null=True, blank=True)

    ALL_ROLES = ['customer', 'retailer', 'wholesaler', 'distributor', 'factory']

    def __str__(self):
        return f"{self.name} — Week {self.current_week}"

    @property
    def is_finished(self):
        return self.current_week >= self.max_weeks

    @property
    def submitted_role_list(self):
        return [r for r in self.submitted_roles.split(',') if r]

    def mark_submitted(self, role):
        roles = self.submitted_role_list
        if role not in roles:
            roles.append(role)
        self.submitted_roles = ','.join(roles)
        self.save(update_fields=['submitted_roles'])

    def all_submitted(self):
        required = set(self.player_sessions.values_list('role', flat=True))
        if not required:
            return True
        return required <= set(self.submitted_role_list)

    def reset_submissions(self):
        self.submitted_roles = ''
        self.pending_customer_demand = None
        self.save(update_fields=['submitted_roles', 'pending_customer_demand'])

    @property
    def ready_role_list(self):
        return [r for r in self.ready_roles.split(',') if r]

    def mark_ready(self, role):
        roles = self.ready_role_list
        if role not in roles:
            roles.append(role)
        self.ready_roles = ','.join(roles)
        self.save(update_fields=['ready_roles'])

    def all_ready(self):
        required = set(self.player_sessions.values_list('role', flat=True))
        if not required:
            return True
        return required <= set(self.ready_role_list)

    @property
    def channel_group_name(self):
        return f"game_{self.id}"


class Player(models.Model):
    ROLE_CHOICES = [
        ('retailer',    'Retailer'),
        ('wholesaler',  'Wholesaler'),
        ('distributor', 'Distributor'),
        ('factory',     'Factory'),
    ]
    ROLE_ORDER = {'retailer': 1, 'wholesaler': 2, 'distributor': 3, 'factory': 4}

    session      = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name='players')
    name         = models.CharField(max_length=100)
    role         = models.CharField(max_length=20, choices=ROLE_CHOICES)
    inventory    = models.IntegerField(default=12)
    backlog      = models.IntegerField(default=0)
    total_cost   = models.FloatField(default=0.0)
    holding_cost = models.FloatField(default=0.5)
    backlog_cost = models.FloatField(default=1.0)

    class Meta:
        ordering = ['role']

    def __str__(self):
        return f"{self.name} ({self.role})"

    def get_downstream(self):
        role_map = {'wholesaler':'retailer','distributor':'wholesaler','factory':'distributor'}
        r = role_map.get(self.role)
        return self.session.players.filter(role=r).first() if r else None

    def get_upstream(self):
        role_map = {'retailer':'wholesaler','wholesaler':'distributor','distributor':'factory'}
        r = role_map.get(self.role)
        return self.session.players.filter(role=r).first() if r else None


class WeeklyState(models.Model):
    player            = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='history')
    week              = models.IntegerField()
    inventory         = models.IntegerField()
    backlog           = models.IntegerField()
    order_placed      = models.IntegerField(default=0)
    order_received    = models.IntegerField(default=0)
    shipment_sent     = models.IntegerField(default=0)
    shipment_received = models.IntegerField(default=0)
    cost_this_week    = models.FloatField(default=0.0)
    cumulative_cost   = models.FloatField(default=0.0)

    class Meta:
        ordering = ['week']
        unique_together = ('player', 'week')

    def __str__(self):
        return f"{self.player} W{self.week}"


class PipelineOrder(models.Model):
    sender          = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='sent_orders')
    quantity        = models.IntegerField()
    placed_on_week  = models.IntegerField()
    arrives_on_week = models.IntegerField(db_index=True)
    fulfilled       = models.BooleanField(default=False, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['sender', 'arrives_on_week', 'fulfilled'],
                         name='po_sender_arrives_idx'),
        ]

    def __str__(self):
        return f"Order {self.quantity} from {self.sender} arriving W{self.arrives_on_week}"


class PipelineShipment(models.Model):
    receiver        = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='incoming_shipments')
    quantity        = models.IntegerField()
    shipped_on_week = models.IntegerField()
    arrives_on_week = models.IntegerField(db_index=True)
    delivered       = models.BooleanField(default=False, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['receiver', 'arrives_on_week', 'delivered'],
                         name='ps_recv_arrives_idx'),
        ]

    def __str__(self):
        return f"Shipment {self.quantity} to {self.receiver} arriving W{self.arrives_on_week}"


class CustomerDemand(models.Model):
    session  = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name='demands')
    week     = models.IntegerField()
    quantity = models.IntegerField()

    class Meta:
        ordering = ['week']

    def __str__(self):
        return f"Demand W{self.week}: {self.quantity}"


class PlayerSession(models.Model):
    PHASE_IDLE    = 'idle'
    PHASE_RECEIVE = 'receive'
    PHASE_SHIP    = 'ship'
    PHASE_ORDER   = 'order'
    PHASE_DONE    = 'done'

    ROLE_CHOICES = [
        ('customer',    'Customer'),
        ('retailer',    'Retailer'),
        ('wholesaler',  'Wholesaler'),
        ('distributor', 'Distributor'),
        ('factory',     'Factory'),
    ]

    game_session  = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name='player_sessions')
    role          = models.CharField(max_length=20, choices=ROLE_CHOICES, db_index=True)
    token         = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    name          = models.CharField(max_length=100, blank=True, default='')
    is_connected  = models.BooleanField(default=False)

    # Link to a registered user (set when they claim the role via join link)
    user          = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='player_sessions',
    )

    # Reconnection: when they last disconnected
    disconnected_at = models.DateTimeField(null=True, blank=True)

    # Week summary stored after close_week so reconnecting players can see it
    last_week_summary = models.JSONField(null=True, blank=True)

    turn_phase           = models.CharField(max_length=10, default=PHASE_IDLE)
    pending_received_qty = models.IntegerField(default=0)
    pending_order_qty    = models.IntegerField(default=0)
    pending_ship_qty     = models.IntegerField(null=True, blank=True)
    pending_order        = models.IntegerField(null=True, blank=True)
    # When True the server auto-completes all phases each week using the AI policy.
    is_ai                = models.BooleanField(default=False)

    class Meta:
        unique_together = ('game_session', 'role')

    def __str__(self):
        return f"{self.role} @ {self.game_session.name} ({self.token[:8]}…)"


class LobbyMessage(models.Model):
    """Simple chat message for the pre-game lobby."""
    game_session = models.ForeignKey(
        GameSession, on_delete=models.CASCADE, related_name='lobby_messages',
    )
    author_name = models.CharField(max_length=100)
    author_role = models.CharField(max_length=20, blank=True, default='')
    body        = models.CharField(max_length=300)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"[{self.author_role}] {self.author_name}: {self.body[:40]}"
