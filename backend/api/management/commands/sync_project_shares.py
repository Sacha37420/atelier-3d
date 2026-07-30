from django.core.management.base import BaseCommand, CommandError

from api.models import Project
from api import storage_client


class Command(BaseCommand):
    """
    Provisionne/synchronise le partage storage (namespace_for_project) de
    chaque projet TOP-LEVEL existant — à lancer UNE FOIS après le déploiement
    du chantier « accès direct storage » (2026-07-30, Option A : Project privé
    par défaut).

    Pourquoi ce rattrapage est nécessaire : ProjectListCreateView.perform_create
    ne provisionne le partage storage d'un projet (Share + ShareMember du
    propriétaire) qu'à SA création. Les projets créés avant ce chantier n'ont
    donc aucun partage storage par-projet — leur accès direct frontend →
    storage (ApiService.mediaUrl()) échouerait (403/404) tant que cette
    commande n'a pas tourné, même si le proxy MediaView, lui, continue de
    fonctionner (compte de service, indifférent à ShareMember).

    Idempotent : peut être relancé sans risque (sync_project_share() recalcule
    l'ensemble complet des membres à chaque appel) — même sémantique que
    `create_group_share` côté storage.
    """

    help = "Synchronise le partage storage de tous les projets top-level existants."

    def handle(self, *args, **options):
        projects = Project.objects.filter(parent_project__isnull=True).order_by('pk')
        total = projects.count()
        if total == 0:
            self.stdout.write('Aucun projet top-level en base.')
            return

        ok = 0
        failed = []
        for project in projects:
            label = f"#{project.pk} « {project.name} » ({project.owner_email or 'SANS PROPRIÉTAIRE'})"
            try:
                storage_client.sync_project_share(project)
            except storage_client.StorageClientError as exc:
                failed.append(project.pk)
                self.stderr.write(self.style.ERROR(f'ÉCHEC  {label} : {exc}'))
                continue
            ok += 1
            self.stdout.write(f'  OK   {label}')

        self.stdout.write(self.style.SUCCESS(f'{ok}/{total} projet(s) synchronisé(s).'))
        if failed:
            raise CommandError(f"Échec pour {len(failed)} projet(s) : {failed}")
