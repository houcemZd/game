"""
Add database indexes to frequently queried fields for improved query performance.

Indexes added:
  - PipelineOrder(sender, arrives_on_week, fulfilled)  — compound
  - PipelineOrder.arrives_on_week                      — simple
  - PipelineOrder.fulfilled                            — simple
  - PipelineShipment(receiver, arrives_on_week, delivered) — compound
  - PipelineShipment.arrives_on_week                   — simple
  - PipelineShipment.delivered                         — simple
  - PlayerSession.role                                 — simple
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('game', '0007_lobby_message'),
    ]

    operations = [
        # ── PipelineOrder: simple indexes ─────────────────────────────────────
        migrations.AlterField(
            model_name='pipelineorder',
            name='arrives_on_week',
            field=models.IntegerField(db_index=True),
        ),
        migrations.AlterField(
            model_name='pipelineorder',
            name='fulfilled',
            field=models.BooleanField(default=False, db_index=True),
        ),
        # ── PipelineOrder: compound index ─────────────────────────────────────
        migrations.AddIndex(
            model_name='pipelineorder',
            index=models.Index(
                fields=['sender', 'arrives_on_week', 'fulfilled'],
                name='po_sender_arrives_idx',
            ),
        ),
        # ── PipelineShipment: simple indexes ──────────────────────────────────
        migrations.AlterField(
            model_name='pipelineshipment',
            name='arrives_on_week',
            field=models.IntegerField(db_index=True),
        ),
        migrations.AlterField(
            model_name='pipelineshipment',
            name='delivered',
            field=models.BooleanField(default=False, db_index=True),
        ),
        # ── PipelineShipment: compound index ──────────────────────────────────
        migrations.AddIndex(
            model_name='pipelineshipment',
            index=models.Index(
                fields=['receiver', 'arrives_on_week', 'delivered'],
                name='ps_recv_arrives_idx',
            ),
        ),
        # ── PlayerSession.role ────────────────────────────────────────────────
        migrations.AlterField(
            model_name='playersession',
            name='role',
            field=models.CharField(
                choices=[
                    ('customer',    'Customer'),
                    ('retailer',    'Retailer'),
                    ('wholesaler',  'Wholesaler'),
                    ('distributor', 'Distributor'),
                    ('factory',     'Factory'),
                ],
                db_index=True,
                max_length=20,
            ),
        ),
    ]
