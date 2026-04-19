from django.db import migrations, models
import django.db.models.deletion
import secrets


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='GameSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(default='Beer Game', max_length=100)),
                ('current_week', models.IntegerField(default=0)),
                ('max_weeks', models.IntegerField(default=20)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('submitted_roles', models.TextField(default='')),
            ],
        ),
        migrations.CreateModel(
            name='Player',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('role', models.CharField(choices=[
                    ('retailer', 'Retailer'), ('wholesaler', 'Wholesaler'),
                    ('distributor', 'Distributor'), ('factory', 'Factory'),
                ], max_length=20)),
                ('inventory', models.IntegerField(default=12)),
                ('backlog', models.IntegerField(default=0)),
                ('total_cost', models.FloatField(default=0.0)),
                ('holding_cost', models.FloatField(default=0.5)),
                ('backlog_cost', models.FloatField(default=1.0)),
                ('session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='players',
                    to='game.gamesession',
                )),
            ],
            options={'ordering': ['role']},
        ),
        migrations.CreateModel(
            name='WeeklyState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('week', models.IntegerField()),
                ('inventory', models.IntegerField()),
                ('backlog', models.IntegerField()),
                ('order_placed', models.IntegerField(default=0)),
                ('order_received', models.IntegerField(default=0)),
                ('shipment_sent', models.IntegerField(default=0)),
                ('shipment_received', models.IntegerField(default=0)),
                ('cost_this_week', models.FloatField(default=0.0)),
                ('cumulative_cost', models.FloatField(default=0.0)),
                ('player', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='history',
                    to='game.player',
                )),
            ],
            options={'ordering': ['week'], 'unique_together': {('player', 'week')}},
        ),
        migrations.CreateModel(
            name='PipelineOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity', models.IntegerField()),
                ('placed_on_week', models.IntegerField()),
                ('arrives_on_week', models.IntegerField()),
                ('fulfilled', models.BooleanField(default=False)),
                ('sender', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sent_orders',
                    to='game.player',
                )),
            ],
        ),
        migrations.CreateModel(
            name='PipelineShipment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity', models.IntegerField()),
                ('shipped_on_week', models.IntegerField()),
                ('arrives_on_week', models.IntegerField()),
                ('delivered', models.BooleanField(default=False)),
                ('receiver', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='incoming_shipments',
                    to='game.player',
                )),
            ],
        ),
        migrations.CreateModel(
            name='CustomerDemand',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('week', models.IntegerField()),
                ('quantity', models.IntegerField()),
                ('session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='demands',
                    to='game.gamesession',
                )),
            ],
            options={'ordering': ['week']},
        ),
        migrations.CreateModel(
            name='PlayerSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[
                    ('retailer', 'Retailer'), ('wholesaler', 'Wholesaler'),
                    ('distributor', 'Distributor'), ('factory', 'Factory'),
                ], max_length=20)),
                ('token', models.CharField(default=secrets.token_urlsafe, max_length=64, unique=True)),
                ('name', models.CharField(blank=True, default='', max_length=100)),
                ('is_connected', models.BooleanField(default=False)),
                ('pending_order', models.IntegerField(blank=True, null=True)),
                ('game_session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='player_sessions',
                    to='game.gamesession',
                )),
            ],
            options={'unique_together': {('game_session', 'role')}},
        ),
    ]
