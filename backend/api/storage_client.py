"""Client minimal de l'API fichiers de l'app storage.

Le backend agit via le **service account** du client confidentiel compagnon
``atelier-3d-admin`` (flux ``client_credentials``, créé par
la simple présence de ``.keycloak-service-account-roles`` — voir ce fichier :
aucun rôle Keycloak n'y est demandé, cette app n'administre pas le realm).

Pourquoi un compte de service et non le forward du token de l'utilisateur
courant (variante ``carto-lab``) : **le worker Celery écrit sans utilisateur
connecté**. Une reconstruction dure des minutes à des heures et produit
plusieurs centaines de Mo (maillages, glTF, STEP) longtemps après la fin de la
requête HTTP qui l'a déclenchée — il n'y a aucun token à forwarder à ce
moment-là. C'est le cas d'usage d'origine de
``KEYCLOAK_SERVICE_WRITE_SHARES`` (cf. CLAUDE.md, « Écriture sans utilisateur
connecté »).

Ce module n'est **pas appelé directement** par le code de l'app : il est le
transport de ``api/storage_backend.LabStorage``, le backend de fichiers Django
branché sur ``STORAGES['default']``. Les ``FileField``/``ImageField`` des modèles
sont donc inchangés, et tout le plumbing habituel (``.save()``, ``.open()``,
``.name``, ``.url``, ``.delete()``) continue de fonctionner.

── Un partage storage par projet TOP-LEVEL, pas un seul partage global ───────

Chantier « accès direct storage » (2026-07-30, Option A) : ``Project`` est
désormais privé par défaut (``owner_email`` + ``ProjectShare``, cf.
``api/models.py``/``api/permissions.py``). Ce module réplique maintenant
``ProjectShare`` en de vrais ``Share``/``ShareMember`` par projet racine
(``namespace_for_project``), exactement comme ``arbre-genealogique`` l'a fait
pour ``TreeShare`` (voir ``arbre-genealogique/backend/api/storage_client.py``,
dont ce module reprend le pattern) — c'est cette réplication qui rend sûr
l'accès direct frontend → storage (``storage_backend.LabStorage.url()``) :
storage devient lui-même le rempart, plus seulement les querysets de
``api/views.py``.

Convention de nommage : ``<STORAGE_NAMESPACE>-project-<id>`` (ex.
``atelier-3d-project-5``) — toujours l'id du projet RACINE, jamais celui d'un
sous-projet CAO (Lot 5.2) : un sous-projet hérite entièrement du namespace de
son parent (cf. ``Project.root_project``), aucun partage storage indépendant
pour lui. Pas d'``<owner_username>`` dans le nom : comme pour
``arbre-genealogique``, un ``Project`` n'a qu'un ``owner_email``, sans
dérivation stable vers un username Keycloak.

Chaque partage est créé et administré par le compte de service lui-même
(``owner_username`` = ``service:atelier-3d-admin`` côté storage) via
``POST /api/shares/`` — pas seulement via le mécanisme de préfixe
auto-vivifiant (``KEYCLOAK_SERVICE_WRITE_PREFIXES``), car ``ShareMember`` doit
pouvoir être peuplé (propriétaire + invités ``ProjectShare``) **avant** même
qu'un premier fichier existe (``resolve_namespace`` n'auto-crée le partage
qu'au premier upload, trop tard pour ``POST /api/shares/<name>/members/``, qui
exige un partage déjà existant). ``KEYCLOAK_SERVICE_WRITE_PREFIXES`` reste
nécessaire pour autoriser l'upload/téléchargement sur ces namespaces
(``resolve_namespace`` pour un compte de service) ::

    KEYCLOAK_SERVICE_WRITE_PREFIXES=…,atelier-3d-admin:atelier-3d-project-

L'ancien partage global fixe ``atelier-3d`` (``required_groups``) n'a plus de
fichier ni de groupe : il est devenu le **conteneur** sous lequel chaque
partage de projet est rangé (chantier « storage-hierarchie », 2026-11-15 —
``ensure_container``/``ensure_share`` ci-dessous et la commande
``rattacher_partages_storage``). Organisation pure : storage ne fait hériter
aucun droit d'un parent, le cloisonnement reste par projet.
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Deux délais distincts : obtenir un token est une requête courte, alors qu'un
# transfert peut porter plusieurs centaines de Mo (maillage dense, glTF, STEP)
# sur le réseau Docker. Un timeout de 30 s tuerait un upload de maillage.
_TIMEOUT = 15
_TRANSFER_TIMEOUT = 900


class StorageClientError(RuntimeError):
    """Erreur de communication ou de configuration avec l'API storage."""


def _split_issuer() -> tuple[str, str]:
    issuer = (settings.KEYCLOAK_ISSUER_URI or '').rstrip('/')
    if '/realms/' not in issuer:
        raise StorageClientError(
            f"KEYCLOAK_ISSUER_URI invalide : {issuer!r} (attendu …/realms/<realm>)."
        )
    base, realm = issuer.split('/realms/', 1)
    return base, realm.split('/', 1)[0]


# Cache de token au niveau process (chaque worker gunicorn a le sien).
_token_cache: dict[str, float | str] = {'value': '', 'exp': 0.0}


def _token() -> str:
    now = time.time()
    if _token_cache['value'] and float(_token_cache['exp']) > now + 5:
        return str(_token_cache['value'])

    client_id = getattr(settings, 'KEYCLOAK_ADMIN_CLIENT_ID', '') or ''
    secret = getattr(settings, 'KEYCLOAK_ADMIN_CLIENT_SECRET', '') or ''
    if not client_id or not secret:
        raise StorageClientError(
            "KEYCLOAK_ADMIN_CLIENT_ID / KEYCLOAK_ADMIN_CLIENT_SECRET manquants. "
            "Lancez setup2.sh atelier-3d (provisionne le "
            "client atelier-3d-admin)."
        )

    base, realm = _split_issuer()
    try:
        resp = requests.post(
            f'{base}/realms/{realm}/protocol/openid-connect/token',
            data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': secret,
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'Keycloak injoignable : {exc}') from exc
    if resp.status_code != 200:
        raise StorageClientError(
            f"Auth service account échouée (HTTP {resp.status_code}) : {resp.text[:200]}"
        )
    data = resp.json()
    _token_cache['value'] = data['access_token']
    _token_cache['exp'] = now + int(data.get('expires_in', 60))
    return str(_token_cache['value'])


def _headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {_token()}'}


_PROJECT_PREFIX = f'{settings.STORAGE_NAMESPACE}-project-'


def namespace_for_project(project_id: int) -> str:
    """Nom du partage storage dédié à CE projet — toujours l'id d'un projet
    RACINE (cf. Project.root_project) — voir le docstring du module."""
    return f'{_PROJECT_PREFIX}{project_id}'


def _files_url(namespace: str, suffix: str = '') -> str:
    return f'{settings.STORAGE_INTERNAL_URL}/api/files/{namespace}/{suffix}'


def upload(relative_path: str, fileobj, filename: str, content_type: str = '',
           namespace: str | None = None) -> dict:
    """Dépose `fileobj` sous `relative_path` dans `namespace` (défaut : l'ancien
    partage global fixe, conservé uniquement pour compatibilité de signature —
    plus utilisé en pratique, cf. storage_backend._namespace_for_name). Écrase
    un fichier déjà présent au même chemin. Retourne le JSON de
    StoredFileSerializer (voir storage/backend/api/views.py)."""
    namespace = namespace or settings.STORAGE_NAMESPACE
    files = {'file': (filename, fileobj, content_type or 'application/octet-stream')}
    try:
        resp = requests.post(
            _files_url(namespace), headers=_headers(),
            data={'relative_path': relative_path}, files=files,
            timeout=_TRANSFER_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code != 201:
        raise StorageClientError(
            f'Upload storage échoué (HTTP {resp.status_code}) : {resp.text[:200]}'
        )
    return resp.json()


def download(relative_path: str, namespace: str | None = None) -> requests.Response:
    """Retourne la réponse streamée (`stream=True`) — à l'appelant de la
    consommer et de la fermer (cf. api/storage_backend.py)."""
    namespace = namespace or settings.STORAGE_NAMESPACE
    try:
        resp = requests.get(
            _files_url(namespace, f'content/{relative_path}'), headers=_headers(),
            stream=True, timeout=_TRANSFER_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code == 404:
        raise FileNotFoundError(relative_path)
    if resp.status_code != 200:
        raise StorageClientError(f'Téléchargement storage échoué (HTTP {resp.status_code}).')
    return resp


def delete(relative_path: str, namespace: str | None = None) -> None:
    namespace = namespace or settings.STORAGE_NAMESPACE
    try:
        resp = requests.delete(
            _files_url(namespace, f'content/{relative_path}'), headers=_headers(), timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code not in (204, 404):
        raise StorageClientError(f'Suppression storage échouée (HTTP {resp.status_code}).')


def listing(prefix: str = '', namespace: str | None = None) -> list[dict]:
    """Métadonnées des fichiers du partage sous `prefix` (relative_path, size,
    content_type…). Sert à `exists()`, `size()` et `listdir()` du backend Django."""
    namespace = namespace or settings.STORAGE_NAMESPACE
    try:
        resp = requests.get(
            _files_url(namespace), headers=_headers(),
            params={'prefix': prefix} if prefix else None, timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code != 200:
        raise StorageClientError(f'Listing storage échoué (HTTP {resp.status_code}).')
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Synchronisation Share/ShareMember — réplique ProjectShare (+ propriétaire)
# dans storage, pour que l'accès direct (LabStorage.url()) soit sûr : storage
# devient lui-même le rempart, plus seulement api/permissions.py.
# ─────────────────────────────────────────────────────────────────────────────

def _shares_url(suffix: str = '') -> str:
    return f'{settings.STORAGE_INTERNAL_URL}/api/shares/{suffix}'


# ── Hiérarchie : conteneur de l'app et rattachement des partages de projet ──
#
# Chantier « storage-hierarchie » (contrat v2, I1/I3) : chaque partage de
# projet est rangé sous le partage d'app ``atelier-3d`` (le « conteneur »),
# qui appartient au même compte de service — storage exige que parent et
# enfant aient le même propriétaire. C'est de l'organisation pure (storage ne
# fait hériter aucun droit d'un parent) : la hiérarchie ne doit JAMAIS faire
# échouer la création d'un partage, donc d'un projet ou d'un dépôt. Un
# conteneur indisponible ou un parent refusé ⇒ partage créé à la racine,
# avertissement à message fixe, et la commande ``rattacher_partages_storage``
# le rangera plus tard.

CONTAINER = 'atelier-3d'

# Conteneur prêt (il nous appartient) — mémorisé par processus. Un échec n'est
# pas mémorisé : la prochaine création réessaie.
_container_ready = False

_MSG_SANS_PARENT = (
    "storage : partage de projet créé sans rattachement au conteneur de l'app "
    "(conteneur indisponible ou parent refusé) — à ranger par rattacher_partages_storage"
)


def container_is_ours() -> bool:
    """Lecture seule : le conteneur existe-t-il et nous appartient-il ?"""
    try:
        list_members(CONTAINER)
        return True
    except StorageClientError:
        return False


def ensure_container() -> bool:
    """S'assure que le conteneur existe et nous appartient (I1, étape 1).
    Rend False s'il est indisponible (nom pris par un autre propriétaire,
    storage en erreur) — ne lève jamais."""
    global _container_ready
    if _container_ready:
        return True
    if container_is_ours():
        _container_ready = True
        return True
    try:
        resp = requests.post(
            _shares_url(), headers=_headers(), data={'name': CONTAINER}, timeout=_TIMEOUT,
        )
    except (requests.RequestException, StorageClientError):
        return False
    if resp.status_code == 201:
        _container_ready = True
        return True
    return False


def _error_keys(resp) -> set:
    if resp.status_code != 400:
        return set()
    try:
        body = resp.json()
    except ValueError:
        return set()
    return set(body) if isinstance(body, dict) else set()


def _is_parent_refusal(resp) -> bool:
    return 'parent' in _error_keys(resp)


def _is_name_taken(resp) -> bool:
    return 'name' in _error_keys(resp)


def _post_share(data: dict):
    try:
        return requests.post(_shares_url(), headers=_headers(), data=data, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc


def ensure_share(namespace: str) -> None:
    """Crée le partage s'il n'existe pas encore (idempotent), rattaché au
    conteneur (I1) — seule fonction de création de partage de l'app. Voir le
    docstring équivalent de arbre-genealogique/backend/api/storage_client.py
    pour le détail de la course/du filet de rattrapage reproduits ici."""
    try:
        list_members(namespace)
        return  # existe déjà et nous appartient : rien à faire (I3 le range).
    except StorageClientError:
        pass

    if ensure_container():
        resp = _post_share({'name': namespace, 'parent': CONTAINER})
        if _is_parent_refusal(resp):
            logger.warning(_MSG_SANS_PARENT)
            resp = _post_share({'name': namespace})
    else:
        logger.warning(_MSG_SANS_PARENT)
        resp = _post_share({'name': namespace})
    if resp.status_code == 201 or _is_name_taken(resp):
        return  # créé, ou 400 `name` : il existe déjà (course) — succès.

    try:
        list_members(namespace)
        return
    except StorageClientError:
        pass
    raise StorageClientError(f'Création du partage storage échouée (HTTP {resp.status_code}) : {resp.text[:200]}')


def list_owned_shares() -> dict[str, str | None]:
    """``{nom: parent}`` de chaque partage du compte de service
    (``GET /api/shares/``, clé ``owned``) — lecture de l'état pour I3."""
    try:
        resp = requests.get(_shares_url(), headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise StorageClientError('storage injoignable (liste des partages).') from exc
    if resp.status_code != 200:
        raise StorageClientError(f'Liste des partages storage échouée (HTTP {resp.status_code}).')
    return {s['name']: s.get('parent') for s in resp.json().get('owned', [])}


def set_parent(namespace: str, parent: str) -> int:
    """``PATCH /api/shares/<namespace>/ {parent}`` — rend le code HTTP, sans
    l'interpréter (I3 décide : 200 fait, 404 ignoré, 400/403 échec)."""
    try:
        resp = requests.patch(
            _shares_url(f'{namespace}/'), headers=_headers(), data={'parent': parent},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError('storage injoignable (rattachement).') from exc
    return resp.status_code


def delete_share(namespace: str) -> None:
    """Supprime le partage `namespace` et, côté storage, tous ses fichiers
    (cascade de DELETE /api/shares/<nom>/). Un partage déjà absent n'est pas
    une erreur. Appelé à la suppression d'un projet RACINE."""
    try:
        resp = requests.delete(_shares_url(f'{namespace}/'), headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code not in (204, 404):
        raise StorageClientError(f'Suppression du partage storage échouée (HTTP {resp.status_code}).')


def list_members(namespace: str) -> list[dict]:
    try:
        resp = requests.get(
            _shares_url(f'{namespace}/members/'), headers=_headers(), timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code != 200:
        raise StorageClientError(f'Lecture des membres storage échouée (HTTP {resp.status_code}).')
    return resp.json()


def set_member(namespace: str, email: str, role: str) -> None:
    try:
        resp = requests.post(
            _shares_url(f'{namespace}/members/'), headers=_headers(),
            data={'member_email': email, 'role': role}, timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code != 201:
        raise StorageClientError(f'Ajout de membre storage échoué (HTTP {resp.status_code}) : {resp.text[:200]}')


def remove_member(namespace: str, email: str) -> None:
    try:
        resp = requests.delete(
            _shares_url(f'{namespace}/members/{email}/'), headers=_headers(), timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code not in (204, 404):
        raise StorageClientError(f'Retrait de membre storage échoué (HTTP {resp.status_code}).')


def sync_project_share(project) -> None:
    """Aligne le partage storage du projet RACINE `project` sur son état DB
    actuel (propriétaire + ProjectShare).

    `project` DOIT être top-level (`parent_project` nul) — appeler avec
    `project.root_project` sinon (cf. api/permissions.py). Un sous-projet n'a
    pas de partage storage propre : ses fichiers vivent déjà dans le namespace
    de son parent (cf. api/storage_backend.py), rien à synchroniser pour lui.

    Recalcule l'ensemble complet des membres à chaque appel plutôt que
    d'incrémenter à partir de l'événement déclencheur : plus simple à
    raisonner, et auto-corrige toute dérive d'un appel précédent resté partiel
    (storage temporairement injoignable en cours de synchronisation).

    Le propriétaire du projet est toujours membre en écriture : c'est lui qui
    dépose les photos/lance les jobs, et le partage storage appartient au
    compte de service (jamais à lui) — sans cette ligne son propre token
    n'aurait aucun accès direct à ses propres fichiers.
    """
    assert project.parent_project_id is None, (
        "sync_project_share() attend un projet racine — utiliser project.root_project."
    )
    namespace = namespace_for_project(project.pk)
    ensure_share(namespace)

    wanted: dict[str, str] = {project.owner_email: 'write'}
    for share in project.shares.all():
        wanted[share.email] = 'write' if share.role == 'EDITOR' else 'read'
    wanted.pop('', None)  # owner_email vide (ne devrait plus arriver après la migration 0009)

    existing = {m['member_email']: m['role'] for m in list_members(namespace)}

    for email, role in wanted.items():
        if existing.get(email) != role:
            set_member(namespace, email, role)
    for email in existing:
        if email not in wanted:
            remove_member(namespace, email)
