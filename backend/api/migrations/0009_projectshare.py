# Chantier « accès direct frontend → storage » (2026-07-30) — Option A retenue
# par l'utilisateur : Project devient privé par défaut (owner_email +
# ProjectShare), calqué sur TreeShare (arbre-genealogique).

import django.db.models.deletion
from django.db import migrations, models


def assign_orphan_owners(apps, schema_editor):
    """
    Le passage en privé par défaut rend tout projet TOP-LEVEL sans
    `owner_email` invisible à quiconque (aucun `ProjectShare` possible sur une
    adresse vide, et api.permissions.visible_projects() ne matche jamais une
    chaîne vide). Vérifié en base au moment d'écrire cette migration : un seul
    cas réel, id=1 « Test E2E Lot 1 » (parent_project nul, owner_email vide).

    Les SOUS-projets orphelins (ex. constatés en base : id=13 « boitier »,
    id=14 « capot », tous deux parent_project=5) ne sont volontairement PAS
    concernés par cette correction : leur accès vient entièrement de leur
    projet racine (cf. Project.root_project / api/permissions.py), jamais de
    leur propre owner_email — ce champ y est purement informatif (l'auteur de
    la création, cf. SubPartListCreateView.post).

    Seule adresse propriétaire déjà présente dans cette table au moment
    d'écrire cette migration (lab mono-utilisateur jusqu'ici) : réassignée à
    la main si ce n'est pas la bonne personne pour le cas ci-dessus.
    """
    Project = apps.get_model('api', 'Project')
    fallback_owner = 'sacha.mailler@gmail.com'
    Project.objects.filter(parent_project__isnull=True, owner_email='').update(
        owner_email=fallback_owner,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0008_project_parent_project'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectShare',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('email', models.EmailField(db_index=True, max_length=255)),
                ('role', models.CharField(
                    choices=[('VIEWER', 'Lecture'), ('EDITOR', 'Édition')],
                    default='VIEWER', max_length=6,
                )),
                ('invited_by', models.EmailField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='shares', to='api.project',
                )),
            ],
            options={
                'db_table': 'project_shares',
                'ordering': ['email'],
            },
        ),
        migrations.AddConstraint(
            model_name='projectshare',
            constraint=models.UniqueConstraint(
                fields=('project', 'email'), name='uniq_project_share_per_project',
            ),
        ),
        # Doit courir APRÈS la création du modèle (rien à faire avec lui ici),
        # mais AVANT que le passage en privé par défaut ne soit exploité par du
        # trafic réel — cf. docstring de la fonction. RunPython.noop en retour
        # arrière : rétablir des chaînes vides ne serait pas une vraie
        # annulation (le propriétaire d'origine, s'il y en avait un, n'a jamais
        # été perdu — cette migration ne touche que des lignes qui étaient
        # déjà vides).
        migrations.RunPython(assign_orphan_owners, migrations.RunPython.noop),
    ]
