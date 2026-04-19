from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('game', '0001_initial')]
    operations = [
        migrations.AddField(
            model_name='gamesession',
            name='status',
            field=models.CharField(default='lobby', max_length=20),
        ),
        migrations.AddField(
            model_name='gamesession',
            name='ready_roles',
            field=models.TextField(default=''),
        ),
    ]
