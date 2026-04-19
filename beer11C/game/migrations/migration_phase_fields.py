# migrations/XXXX_add_playersession_phase_fields.py
# Place this in your game/migrations/ folder and number it appropriately.
# Run: python manage.py migrate

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        # Replace with your last migration number, e.g.:
        ('game', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='playersession',
            name='turn_phase',
            field=models.CharField(max_length=10, default='idle'),
        ),
        migrations.AddField(
            model_name='playersession',
            name='pending_received_qty',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='playersession',
            name='pending_order_qty',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='playersession',
            name='pending_ship_qty',
            field=models.IntegerField(null=True, blank=True),
        ),
    ]
