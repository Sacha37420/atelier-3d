from django.db import migrations

DEPARTMENTS = [
    ('Développement',  'Conception et développement des applications'),
    ('Infrastructure', 'Gestion des serveurs, réseaux et déploiements'),
    ('Qualité',        'Tests, recette et assurance qualité'),
]

USERS = [
    ('alice@example.com',   'Alice Martin',   'Développement'),
    ('bob@example.com',     'Bob Dupont',     'Infrastructure'),
    ('charlie@example.com', 'Charlie Durand', 'Qualité'),
]


def load_data(apps, schema_editor):
    Department = apps.get_model('api', 'Department')
    UserRecord  = apps.get_model('api', 'UserRecord')

    depts = {}
    for name, description in DEPARTMENTS:
        dept = Department.objects.create(name=name, description=description)
        depts[name] = dept

    for email, display_name, dept_name in USERS:
        UserRecord.objects.create(
            email=email,
            display_name=display_name,
            department=depts[dept_name],
        )


def unload_data(apps, schema_editor):
    Department = apps.get_model('api', 'Department')
    UserRecord  = apps.get_model('api', 'UserRecord')

    UserRecord.objects.filter(email__in=[u[0] for u in USERS]).delete()
    Department.objects.filter(name__in=[d[0] for d in DEPARTMENTS]).delete()


class Migration(migrations.Migration):

    dependencies = [('api', '0001_initial')]

    operations = [
        migrations.RunPython(load_data, unload_data),
    ]
