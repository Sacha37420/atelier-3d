"""Backend de fichiers Django adossé à l'API de l'app ``storage``.

Branché sur ``STORAGES['default']`` (cf. ``config/settings.py``), il fait que
**les modèles n'ont pas changé** : ``Photo.file``, ``Project.region_map``,
``Mesh.file``/``gltf_file``/``step_file`` restent des ``FileField``/``ImageField``
avec leurs ``upload_to``, et tout le plumbing Django habituel continue de
fonctionner (``field.save(nom, contenu)``, ``field.open()``, ``field.name``,
``field.url``, ``field.delete()``).

Pourquoi un backend ``Storage`` plutôt que des ``CharField`` de chemin, comme
pour les trois autres apps migrées : ici il y a **six champs fichier** et huit
appels ``field.save(...)`` répartis dans le worker (``tasks.py``, ``facade.py``,
``video_import.py``). Surtout, ``FileField.name`` contenait *déjà* le chemin
logique (``projects/<id>/photos/<nom>``) — qui est exactement un
``relative_path`` valide côté storage. Le basculement ne demande donc **aucune
migration de schéma ni de données en base** : seuls les octets changent de place,
aux mêmes chemins logiques (cf. la commande ``transferer_vers_storage``).

⚠ Ce qui **ne** marche plus, à dessein : ``FileField.path``. Un stockage distant
n'a pas de chemin local, et Django lève ``NotImplementedError`` — c'est le
comportement voulu, il rend visible chaque endroit qui avait besoin d'un vrai
fichier sur disque pour un outil natif (COLMAP, trimesh, FreeCAD, build123d).
Ces endroits utilisent ``local_copy()`` ci-dessous, qui matérialise une copie
temporaire explicite : le coût du transfert reste ainsi lisible dans le code,
au lieu d'être caché derrière un accès d'attribut anodin.
"""
from __future__ import annotations

import contextlib
import mimetypes
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

from . import storage_client


@deconstructible
class LabStorage(Storage):
    """Stockage distant : chaque fichier est un objet du partage storage de l'app."""

    def _open(self, name, mode='rb'):
        if 'w' in mode:
            raise ValueError(
                "LabStorage ne sait pas ouvrir en écriture : passer par "
                "field.save(nom, contenu) ou storage.save(nom, contenu)."
            )
        resp = storage_client.download(name)
        # Rapatriement intégral en mémoire : les appelants qui manipulent de gros
        # fichiers (maillages) passent par local_copy(), qui streame sur disque.
        return ContentFile(resp.content, name=name)

    def _save(self, name, content):
        content_type = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        # Django peut passer un fichier déjà partiellement lu (validation d'image,
        # calcul de dimensions…) : repartir du début, sinon l'upload serait tronqué.
        with contextlib.suppress(Exception):
            content.seek(0)
        storage_client.upload(name, content, Path(name).name, content_type)
        # storage écrase sur (namespace, relative_path) : le nom demandé est
        # toujours le nom retenu, d'où get_available_name() ci-dessous.
        return name

    def get_available_name(self, name, max_length=None):
        """Ne suffixe jamais le nom en cas de collision.

        Comportement délibérément différent du stockage local de Django (qui
        ajoute un suffixe aléatoire) : les noms sont déjà déterministes et
        porteurs de sens (``reconstruction_v3.ply``, ``<photo_id>_regions.npz``),
        et chaque version de maillage a son propre numéro. Un suffixe aléatoire
        ne ferait qu'accumuler des doublons silencieux à chaque réécriture.
        """
        return name

    def delete(self, name):
        if not name:
            return
        storage_client.delete(name)

    def exists(self, name):
        return self._entry(name) is not None

    def size(self, name):
        entry = self._entry(name)
        if entry is None:
            raise FileNotFoundError(name)
        return entry['size']

    def _entry(self, name) -> dict | None:
        """Métadonnées du fichier `name`, ou None. Le listing est filtré par
        préfixe côté storage, donc une seule requête et une seule ligne."""
        for entry in storage_client.listing(name):
            if entry['relative_path'] == name:
                return entry
        return None

    def url(self, name):
        # Chemin storage DIRECT (chantier « accès direct frontend → storage »,
        # 2026-07-30) — plus un chemin `MEDIA_URL` proxié par api.views.MediaView.
        # Le token n'est jamais concaténé ici : ce champ n'est qu'un chemin
        # relatif au serveur storage (pas de domaine, pas de préfixe Caddy
        # '/storage-api' — Caddy le retire déjà via handle_path avant que le
        # conteneur storage-backend ne le voie, exactement comme
        # FORCE_SCRIPT_NAME/'atelier-3d-api' pour cette app, cf. settings.py).
        # C'est ApiService.mediaUrl() côté frontend (storageApiUrl, cf.
        # nginx-entrypoint.sh) qui préfixe par l'origine correcte et ajoute
        # `?token=` (le navigateur ne peut jamais poser d'en-tête Authorization
        # sur un <img>/GLTFLoader). Sécurité : c'est désormais storage lui-même
        # qui est le rempart (KEYCLOAK_TRUSTED_CLIENTS + Share.required_groups
        # sur le partage 'atelier-3d', PAS un contrôle par projet — cf. CLAUDE.md
        # et le rapport de ce chantier : aucun ProjectShare n'existe, cette app
        # n'a jamais restreint l'accès par owner_email, seulement par groupe
        # Keycloak — le partage storage réplique exactement ce même périmètre).
        # api.views.MediaView reste en place (fallback authentifié par le
        # backend de l'app), mais n'est plus utilisé par défaut.
        return f"/api/files/{settings.STORAGE_NAMESPACE}/content/{name}"

    def listdir(self, path):
        prefix = path.rstrip('/') + '/' if path else ''
        dirs, files = set(), []
        for entry in storage_client.listing(prefix):
            reste = entry['relative_path'][len(prefix):]
            if '/' in reste:
                dirs.add(reste.split('/', 1)[0])
            else:
                files.append(reste)
        return sorted(dirs), sorted(files)

    def get_modified_time(self, name):
        from django.utils.dateparse import parse_datetime
        entry = self._entry(name)
        if entry is None:
            raise FileNotFoundError(name)
        return parse_datetime(entry['updated_at'])


@contextlib.contextmanager
def local_copy(fieldfile, suffix: str = ''):
    """Matérialise `fieldfile` dans un fichier temporaire local, le temps du bloc.

    Nécessaire partout où un **outil natif** doit lire un vrai chemin : COLMAP,
    trimesh, FreeCAD, build123d, numpy.load… Remplace l'ancien
    ``Path(champ.path)``, qui ne peut plus fonctionner avec un stockage distant.

    Le suffixe par défaut reprend l'extension du nom stocké : plusieurs de ces
    outils choisissent leur parseur d'après l'extension (``.ply``, ``.step``,
    ``.npz``), un temporaire sans extension casserait la lecture.

        with local_copy(mesh.file) as chemin:
            geom = trimesh.load(str(chemin))
    """
    if not fieldfile:
        raise ValueError('Champ fichier vide : rien à matérialiser.')
    suffix = suffix or Path(fieldfile.name).suffix
    tmp = tempfile.NamedTemporaryFile(prefix='atelier3d-', suffix=suffix, delete=False)
    try:
        resp = storage_client.download(fieldfile.name)
        with tmp:
            for morceau in resp.iter_content(chunk_size=1024 * 1024):
                tmp.write(morceau)
        yield Path(tmp.name)
    finally:
        with contextlib.suppress(OSError):
            Path(tmp.name).unlink()


def download_to(fieldfile, dest: Path) -> Path:
    """Matérialise `fieldfile` à l'emplacement local `dest`, qui doit survivre
    plus longtemps qu'un simple bloc `with` (variante persistante de
    `local_copy()`).

    Cas d'usage : ``cad_assemble.resolve_assembly`` doit rassembler *plusieurs*
    fichiers STEP dans le répertoire de travail avant de lancer un unique
    sous-processus ``freecadcmd`` qui les lit tous par chemin — impossible
    d'utiliser un context manager par fichier, il faudrait les imbriquer.
    L'appelant est responsable du nettoyage (cf. `scratch_dir`/`purge_scratch` :
    `dest` vit déjà dans un répertoire de travail purgé en fin de job).
    """
    resp = storage_client.download(fieldfile.name)
    with open(dest, 'wb') as fh:
        for morceau in resp.iter_content(chunk_size=1024 * 1024):
            fh.write(morceau)
    return dest


def upload_local_file(fieldfile, chemin: Path, nom: str) -> None:
    """Dépose le fichier local `chemin` dans `fieldfile` sous le nom `nom`.

    Équivalent de ``fieldfile.save(nom, File(fh), save=False)`` mais en streamant
    depuis le disque sans charger le fichier en mémoire — les maillages denses
    pèsent plusieurs centaines de Mo.
    """
    from django.core.files import File
    with open(chemin, 'rb') as fh:
        fieldfile.save(nom, File(fh), save=False)


def scratch_dir(*parties: str) -> Path:
    """Répertoire de travail local d'un job, créé si besoin.

    **Jamais dans storage** : ce sont les intermédiaires de calcul (bases COLMAP,
    nuages de points denses, images redimensionnées) — plusieurs Go par job, lus
    et réécrits en boucle par des outils natifs. Les faire transiter par HTTP
    serait absurde, et ils n'ont aucune valeur une fois le job terminé. Même
    raisonnement que le volume `downloads` de robot-lab (cf. CLAUDE.md).

    Vit sous ``settings.SCRATCH_ROOT``, un volume Docker local non-`external`
    que `clean2.sh` peut vider sans rien perdre d'important.
    """
    chemin = Path(settings.SCRATCH_ROOT).joinpath(*parties)
    chemin.mkdir(parents=True, exist_ok=True)
    return chemin


def purge_scratch(*parties: str) -> None:
    """Supprime un répertoire de travail. Best-effort : l'échec du nettoyage ne
    doit jamais faire échouer un job qui a réussi."""
    with contextlib.suppress(OSError):
        shutil.rmtree(Path(settings.SCRATCH_ROOT).joinpath(*parties), ignore_errors=True)
