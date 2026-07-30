import hashlib

from django.core.management.base import BaseCommand, CommandError

from api import storage_backend, storage_client
from api.models import Project


class Command(BaseCommand):
    """
    Migration de données à usage unique — chantier « accès direct storage »
    (2026-07-30, Option A) : bascule les fichiers déjà uploadés (photos,
    maillages, glTF, STEP) de l'ANCIEN partage global fixe ``atelier-3d`` vers
    UN PARTAGE PAR PROJET RACINE (``atelier-3d-project-<id>``, cf.
    api/storage_client.namespace_for_project).

    Nécessaire car les octets ne bougent JAMAIS tout seuls : ``LabStorage``
    (api/storage_backend.py) résout désormais le namespace de chaque FileField
    dynamiquement depuis son chemin (``projects/<id>/...``) — sans cette
    migration, tout fichier uploadé AVANT ce chantier reste introuvable
    (404) : il existe toujours côté storage, mais sous l'ancien namespace que
    plus aucun code de cette app ne consulte.

    Méthode (même principe que la migration d'origine vers storage, cf.
    CLAUDE.md — « transferer_vers_storage » : téléchargement, vérification par
    checksum, dépôt à la nouvelle adresse, SEULEMENT ALORS suppression de
    l'ancienne) : passe exclusivement par l'API publique de storage (jamais de
    SQL direct sur sa base) — téléchargement depuis l'ancien namespace,
    upload vers le nouveau, vérification SHA-256 avant toute suppression.

    Prérequis (à vérifier AVANT de lancer cette commande) :
      - storage/.env : KEYCLOAK_SERVICE_WRITE_PREFIXES doit contenir
        'atelier-3d-admin:atelier-3d-project-' (sinon 403 sur l'upload vers le
        nouveau namespace — vérifié en conditions réelles lors de ce
        chantier, cf. rapport).
      - Le compte de service garde par ailleurs accès à l'ancien namespace fixe
        'atelier-3d' via KEYCLOAK_SERVICE_WRITE_SHARES le temps de cette
        migration (à retirer seulement APRÈS, cf. rapport de chantier).

    Idempotent : un fichier déjà absent de l'ancien namespace (donc déjà migré
    par un run précédent) est simplement ignoré.
    """

    help = "Bascule les fichiers atelier-3d de l'ancien partage global vers un partage par projet racine."

    OLD_NAMESPACE = 'atelier-3d'

    def handle(self, *args, **options):
        entries = storage_client.listing(namespace=self.OLD_NAMESPACE)
        if not entries:
            self.stdout.write("Rien à migrer : l'ancien namespace 'atelier-3d' est déjà vide.")
            return

        self.stdout.write(f'{len(entries)} fichier(s) à examiner dans « {self.OLD_NAMESPACE} ».')

        # Regroupe par projet racine pour ne provisionner/synchroniser chaque
        # partage cible qu'une fois (ensure_share + sync_project_share),
        # plutôt qu'à chaque fichier.
        by_root: dict[int, list[dict]] = {}
        skipped = []
        for entry in entries:
            project_id = storage_backend.project_id_from_path(entry['relative_path'])
            if project_id is None:
                skipped.append(entry['relative_path'])
                continue
            try:
                project = Project.objects.only('id', 'parent_project_id').get(pk=project_id)
            except Project.DoesNotExist:
                skipped.append(entry['relative_path'])
                continue
            by_root.setdefault(project.root_project.pk, []).append(entry)

        if skipped:
            self.stdout.write(self.style.WARNING(
                f"{len(skipped)} chemin(s) ignoré(s) (aucun projet identifiable) : {skipped}",
            ))

        total_ok, total_failed = 0, []
        for root_id, files in sorted(by_root.items()):
            root = Project.objects.get(pk=root_id)
            new_namespace = storage_client.namespace_for_project(root_id)
            self.stdout.write(
                f'\n-- Projet racine #{root_id} « {root.name} » '
                f'({len(files)} fichier(s)) -> {new_namespace}',
            )
            try:
                storage_client.sync_project_share(root)
            except storage_client.StorageClientError as exc:
                self.stderr.write(self.style.ERROR(f'   partage storage indisponible pour #{root_id} : {exc}'))
                total_failed.extend(f['relative_path'] for f in files)
                continue

            for entry in files:
                path = entry['relative_path']
                try:
                    self._migrate_one(path, new_namespace)
                except storage_client.StorageClientError as exc:
                    self.stderr.write(self.style.ERROR(f'   ÉCHEC {path} : {exc}'))
                    total_failed.append(path)
                    continue
                total_ok += 1
                self.stdout.write(f'   OK    {path}')

        self.stdout.write(self.style.SUCCESS(f'\n{total_ok} fichier(s) migré(s).'))
        if total_failed:
            raise CommandError(f'{len(total_failed)} échec(s) : {total_failed}')

    def _migrate_one(self, path: str, new_namespace: str) -> None:
        resp = storage_client.download(path, namespace=self.OLD_NAMESPACE)
        content = resp.content
        checksum_before = hashlib.sha256(content).hexdigest()

        filename = path.rsplit('/', 1)[-1]
        content_type = resp.headers.get('Content-Type', 'application/octet-stream')
        storage_client.upload(path, content, filename, content_type, namespace=new_namespace)

        # Vérification indépendante avant toute suppression, comme documenté
        # pour les migrations précédentes vers storage (cf. CLAUDE.md).
        verify = storage_client.download(path, namespace=new_namespace)
        checksum_after = hashlib.sha256(verify.content).hexdigest()
        if checksum_after != checksum_before:
            raise storage_client.StorageClientError(
                f"Checksum différent après copie ({checksum_before} != {checksum_after}) — "
                "ancienne copie CONSERVÉE, à examiner manuellement.",
            )

        storage_client.delete(path, namespace=self.OLD_NAMESPACE)
