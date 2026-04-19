from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('game', '0002_gamesession_status_ready')]
    operations = [
        migrations.AddField(
            model_name='gamesession',
            name='pending_customer_demand',
            field=models.IntegerField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='playersession',
            name='role',
            field=models.CharField(max_length=20, choices=[
                ('customer','Customer'),('retailer','Retailer'),
                ('wholesaler','Wholesaler'),('distributor','Distributor'),
                ('factory','Factory'),
            ]),
        ),
    ]
