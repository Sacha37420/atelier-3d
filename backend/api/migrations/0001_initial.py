from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Department',
            fields=[
                ('id',          models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('name',        models.CharField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True)),
            ],
            options={
                'db_table': 'departments',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='UserRecord',
            fields=[
                ('email',         models.EmailField(max_length=255, primary_key=True, serialize=False)),
                ('display_name',  models.CharField(blank=True, max_length=200)),
                ('registered_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('department',    models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='members',
                    to='api.department',
                )),
            ],
            options={
                'db_table': 'user_records',
                'ordering': ['email'],
            },
        ),
    ]
