"""Range sous le conteneur ``atelier-3d`` les partages storage des projets
racines créés à la racine (chantier « storage-hierarchie », contrat v2, I3).

    python manage.py rattacher_partages_storage [--check]

Idempotente et rejouable après une interruption. ``--check`` ne modifie rien,
affiche ce qui serait fait et sort en code 1 s'il reste quelque chose à faire.
Sans option : s'assure du conteneur, puis ``PATCH {parent}`` sur chaque
partage de projet racine qui n'est pas rangé dessous. Un partage absent de
storage (projet sans fichier, ou partage d'un autre propriétaire) est ignoré,
jamais créé. Un échec (PATCH 400/403, storage injoignable) est compté et fait
sortir en code 1 ; la commande continue pour les autres.

Aucun message ne contient de jeton ni d'URL.
"""
from django.core.management.base import BaseCommand

from api import storage_client
from api.models import Project


def wanted_children() -> list[str]:
    """Partages d'enfant connus des données : un par projet RACINE (un
    sous-projet vit dans le partage de sa racine)."""
    ids = Project.objects.filter(parent_project__isnull=True).order_by('pk').values_list('pk', flat=True)
    return [storage_client.namespace_for_project(pk) for pk in ids]


class Command(BaseCommand):
    help = "Range les partages storage des projets sous le conteneur de l'app (--check : lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true',
                            help="Lecture seule : affiche ce qui serait fait, code 1 s'il reste à faire.")

    def handle(self, *args, check=False, **options):
        container = storage_client.CONTAINER
        try:
            owned = storage_client.list_owned_shares()
        except storage_client.StorageClientError:
            self.stderr.write('storage injoignable ou refus : liste des partages illisible.')
            raise SystemExit(1)

        to_do = [name for name in wanted_children()
                 if name in owned and owned[name] != container]
        ignored = sum(1 for name in wanted_children() if name not in owned)
        container_missing = container not in owned

        if check:
            if container_missing:
                self.stdout.write(f'à faire : créer le conteneur {container}')
            for name in to_do:
                self.stdout.write(f'à faire : {name} (parent {owned[name] or "aucun"}) → {container}')
            self.stdout.write(
                f'bilan : {len(to_do)} partage(s) à rattacher, {ignored} absent(s) de storage (ignorés)'
            )
            if to_do or container_missing:
                raise SystemExit(1)
            return

        if not storage_client.ensure_container():
            self.stderr.write(f'conteneur {container} indisponible (absent et non créable, ou à un autre propriétaire).')
            raise SystemExit(1)

        done = failed = vanished = 0
        for name in to_do:
            try:
                code = storage_client.set_parent(name, container)
            except storage_client.StorageClientError:
                code = None
            if code == 200:
                done += 1
                self.stdout.write(f'rattaché : {name} → {container}')
            elif code == 404:
                vanished += 1
            else:
                failed += 1
                self.stderr.write(f'échec : {name} (HTTP {code if code is not None else "injoignable"})')
        self.stdout.write(
            f'bilan : {done} rattaché(s), {failed} échec(s), {ignored + vanished} absent(s) de storage (ignorés)'
        )
        if failed:
            raise SystemExit(1)
