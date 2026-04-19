"""
Add demand_schedule to GameSession and is_ai to PlayerSession.

  GameSession.demand_schedule  — JSONField; null = manual, "classic" = MIT step,
                                  list[int] = custom per-week values.
  PlayerSession.is_ai          — BooleanField; when True the server auto-completes
                                  all phases each week using the AI base-stock policy.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('game', '0008_add_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='gamesession',
            name='demand_schedule',
            field=models.JSONField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='playersession',
            name='is_ai',
            field=models.BooleanField(default=False),
        ),
    ]
