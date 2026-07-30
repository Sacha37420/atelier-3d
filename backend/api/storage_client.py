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

Un unique partage (``settings.STORAGE_NAMESPACE``) pour toute l'app : les projets
appartiennent à un ``owner_email`` mais le cloisonnement reste porté par les
querysets de ``api/views.py``, exactement comme quand les octets étaient sur le
volume local.

Le partage doit exister au préalable, côté storage ::

    python manage.py create_group_share atelier-3d \\
        --owner sacha --required-groups developers

et le compte de service y être autorisé via
``KEYCLOAK_SERVICE_WRITE_SHARES=atelier-3d-admin:atelier-3d``
(``storage/.env``).
"""
from __future__ import annotations

import time

import requests
from django.conf import settings

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


def _files_url(suffix: str = '') -> str:
    return (
        f'{settings.STORAGE_INTERNAL_URL}/api/files/'
        f'{settings.STORAGE_NAMESPACE}/{suffix}'
    )


def upload(relative_path: str, fileobj, filename: str, content_type: str = '') -> dict:
    """Dépose `fileobj` sous `relative_path`. Écrase un fichier déjà présent au
    même chemin. Retourne le JSON de StoredFileSerializer (voir
    storage/backend/api/views.py)."""
    files = {'file': (filename, fileobj, content_type or 'application/octet-stream')}
    try:
        resp = requests.post(
            _files_url(), headers=_headers(),
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


def download(relative_path: str) -> requests.Response:
    """Retourne la réponse streamée (`stream=True`) — à l'appelant de la
    consommer et de la fermer (cf. api/storage_backend.py)."""
    try:
        resp = requests.get(
            _files_url(f'content/{relative_path}'), headers=_headers(),
            stream=True, timeout=_TRANSFER_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code == 404:
        raise FileNotFoundError(relative_path)
    if resp.status_code != 200:
        raise StorageClientError(f'Téléchargement storage échoué (HTTP {resp.status_code}).')
    return resp


def delete(relative_path: str) -> None:
    try:
        resp = requests.delete(
            _files_url(f'content/{relative_path}'), headers=_headers(), timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code not in (204, 404):
        raise StorageClientError(f'Suppression storage échouée (HTTP {resp.status_code}).')


def listing(prefix: str = '') -> list[dict]:
    """Métadonnées des fichiers du partage sous `prefix` (relative_path, size,
    content_type…). Sert à `exists()`, `size()` et `listdir()` du backend Django."""
    try:
        resp = requests.get(
            _files_url(), headers=_headers(),
            params={'prefix': prefix} if prefix else None, timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StorageClientError(f'storage injoignable : {exc}') from exc
    if resp.status_code != 200:
        raise StorageClientError(f'Listing storage échoué (HTTP {resp.status_code}).')
    return resp.json()
