"""Outils communs aux tests d'atelier-3d.

Trois simulations, et rien d'autre :

* **Keycloak, côté vérification** — seul le TÉLÉCHARGEMENT du JWKS est remplacé
  par une clé RSA locale ; signature, expiration, `azp`, groupes et e-mail sont
  réellement contrôlés par `api.authentication.KeycloakJWTAuthentication`.
* **Keycloak, côté compte de service, et l'app `storage`** — `FauxStorage`
  remplace `requests.get/post/delete` : tout le code de `api.storage_client` et
  de `api.storage_backend.LabStorage` s'exécute, seul le fil est coupé.
* **Le répertoire de travail des jobs** — un dossier temporaire par test
  (`SCRATCH_ROOT`), `/app` étant en lecture seule sous le lanceur.
"""
from __future__ import annotations

import functools
import io
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from api import storage_client
from api.authentication import KeycloakJWTAuthentication
from api.models import (
    CadAssemblyConstraint, CadAssemblyInstance, Mesh, Project, ProjectShare,
)

ALICE = 'alice@ssolab.local'    # propriétaire
BOB = 'bob@ssolab.local'        # invité en édition
CHLOE = 'chloe@ssolab.local'    # invitée en lecture
DAVID = 'david@ssolab.local'    # membre du lab, étranger au projet

REGLAGES = dict(
    KEYCLOAK_CLIENT_ID='atelier-3d',
    KEYCLOAK_REQUIRED_GROUPS='developers,famille,amis',
    KEYCLOAK_ISSUER_URI='http://keycloak.test/realms/ssolab',
    KEYCLOAK_ADMIN_CLIENT_ID='atelier-3d-admin',
    KEYCLOAK_ADMIN_CLIENT_SECRET='secret-de-test',
    STORAGE_INTERNAL_URL='http://storage.test',
)

JETON_DE_SERVICE = 'jeton-du-compte-de-service'

_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ABSENT = object()


class JwksLocal:
    """Remplace le téléchargement du JWKS : rend toujours la clé publique locale."""

    class _Cle:
        key = _RSA.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._Cle()


def jeton(email=ALICE, groups=('developers',), cle=None, **claims) -> str:
    """Jeton RS256 signé localement. Un claim passé à `None` est retiré."""
    payload = {
        'preferred_username': (email or 'anonyme').split('@')[0],
        'email': email,
        'groups': list(groups) if groups is not None else None,
        'azp': 'atelier-3d',
        'exp': int(time.time()) + 300,
    }
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, cle or _RSA, algorithm='RS256')


# ── Faux storage ──────────────────────────────────────────────────────────────
class FausseReponse:
    def __init__(self, status=200, data=None, content=b'', headers=None):
        self.status_code = status
        self._data = data
        self.content = content
        self.headers = headers or {}
        self.text = json.dumps(data) if data is not None else content.decode('utf-8', 'replace')

    def json(self):
        return self._data

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class FauxStorage:
    """L'app storage (fichiers + partages) et le point de jeton de Keycloak,
    en mémoire. `fichiers` : {(partage, chemin): octets} ; `partages` :
    {partage: {email: rôle}}."""

    def __init__(self):
        self.fichiers: dict[tuple[str, str], bytes] = {}
        self.partages: dict[str, dict[str, str]] = {}
        self.appels: list[tuple[str, str]] = []
        self.arrete = False            # storage injoignable
        self.keycloak_arrete = False   # Keycloak injoignable
        self.demandes_de_jeton = 0
        self.dernier_secret = None

    def brancher(self, cas) -> 'FauxStorage':
        for verbe in ('get', 'post', 'delete'):
            patcher = mock.patch.object(
                storage_client.requests, verbe,
                side_effect=functools.partial(self._servir, verbe.upper()),
            )
            patcher.start()
            cas.addCleanup(patcher.stop)
        return self

    def appels_vers(self, fragment: str, verbe: str | None = None) -> list:
        return [a for a in self.appels if fragment in a[1] and (verbe is None or a[0] == verbe)]

    def _servir(self, verbe, url, headers=None, data=None, files=None, params=None, **_):
        self.appels.append((verbe, url))
        if url.endswith('/protocol/openid-connect/token'):
            if self.keycloak_arrete:
                raise requests.ConnectionError('keycloak arrêté')
            self.demandes_de_jeton += 1
            self.dernier_secret = (data or {}).get('client_secret')
            if self.dernier_secret != REGLAGES['KEYCLOAK_ADMIN_CLIENT_SECRET']:
                return FausseReponse(401, {'error': 'unauthorized_client'})
            return FausseReponse(200, {'access_token': JETON_DE_SERVICE, 'expires_in': 300})

        if self.arrete:
            raise requests.ConnectionError('storage arrêté')
        if not url.startswith(REGLAGES['STORAGE_INTERNAL_URL']):
            raise AssertionError(f'appel réseau inattendu pendant un test : {verbe} {url}')
        if (headers or {}).get('Authorization') != f'Bearer {JETON_DE_SERVICE}':
            return FausseReponse(401, {'detail': 'jeton absent ou inconnu'})
        chemin = url[len(REGLAGES['STORAGE_INTERNAL_URL']):]

        if m := re.fullmatch(r'/api/files/([^/]+)/', chemin):
            ns = m.group(1)
            if verbe == 'POST':
                rel = data['relative_path']
                octets = files['file'][1].read()
                self.fichiers[(ns, rel)] = octets
                return FausseReponse(201, {'relative_path': rel, 'size': len(octets)})
            if verbe == 'GET':
                prefixe = (params or {}).get('prefix', '')
                return FausseReponse(200, [
                    {'relative_path': rel, 'size': len(o), 'updated_at': '2026-10-11T10:00:00Z'}
                    for (n, rel), o in sorted(self.fichiers.items())
                    if n == ns and rel.startswith(prefixe)
                ])
        if m := re.fullmatch(r'/api/files/([^/]+)/content/(.+)', chemin):
            cle = (m.group(1), m.group(2))
            if cle not in self.fichiers:
                return FausseReponse(404)
            if verbe == 'GET':
                return FausseReponse(200, content=self.fichiers[cle],
                                     headers={'Content-Type': 'application/x-essai'})
            if verbe == 'DELETE':
                del self.fichiers[cle]
                return FausseReponse(204)
        if chemin == '/api/shares/' and verbe == 'POST':
            if data['name'] in self.partages:
                return FausseReponse(400, {'name': ['existe déjà']})
            self.partages[data['name']] = {}
            return FausseReponse(201, {'name': data['name']})
        if m := re.fullmatch(r'/api/shares/([^/]+)/members/', chemin):
            ns = m.group(1)
            if ns not in self.partages:
                return FausseReponse(404)
            if verbe == 'GET':
                return FausseReponse(200, [
                    {'member_email': e, 'role': r} for e, r in sorted(self.partages[ns].items())
                ])
            if verbe == 'POST':
                self.partages[ns][data['member_email']] = data['role']
                return FausseReponse(201, {})
        if m := re.fullmatch(r'/api/shares/([^/]+)/members/(.+)/', chemin):
            if verbe == 'DELETE':
                existait = self.partages.get(m.group(1), {}).pop(m.group(2), _ABSENT)
                return FausseReponse(404 if existait is _ABSENT else 204)
        raise AssertionError(f'route storage non simulée : {verbe} {chemin}')


# ── Maillages d'essai (une boîte 1 × 2 × 3 : 8 sommets, 12 faces) ──────────────
@functools.lru_cache(maxsize=None)
def boite_ply() -> bytes:
    import trimesh
    return trimesh.creation.box(extents=(1.0, 2.0, 3.0)).export(file_type='ply')


def temporaires() -> set:
    """Fichiers temporaires laissés par `storage_backend.local_copy`."""
    return set(Path(tempfile.gettempdir()).glob('atelier3d-*'))


class LabMixin:
    """À combiner avec TestCase / TransactionTestCase ; appeler `monter_le_lab()`
    dans `setUp`."""

    def monter_le_lab(self) -> None:
        self.scratch = tempfile.mkdtemp(prefix='essai-scratch-')
        self.addCleanup(shutil.rmtree, self.scratch, True)
        reglages = override_settings(SCRATCH_ROOT=self.scratch, **REGLAGES)
        reglages.enable()
        self.addCleanup(reglages.disable)
        jwks = mock.patch.object(KeycloakJWTAuthentication, '_get_jwks_client',
                                 classmethod(lambda cls: JwksLocal()))
        jwks.start()
        self.addCleanup(jwks.stop)
        self.storage = FauxStorage().brancher(self)
        self._oublier_le_jeton_de_service()
        self.addCleanup(self._oublier_le_jeton_de_service)
        cache.clear()   # compteur du débit des routes publiques

    @staticmethod
    def _oublier_le_jeton_de_service() -> None:
        storage_client._token_cache.update(value='', exp=0.0)

    # ── identités ────────────────────────────────────────────────────────────
    def client_de(self, email=ALICE, groups=('developers',), **claims) -> APIClient:
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {jeton(email, groups, **claims)}')
        return client

    # ── données ──────────────────────────────────────────────────────────────
    def projet(self, owner=ALICE, name='Projet', **champs) -> Project:
        return Project.objects.create(name=name, owner_email=owner, **champs)

    def projet_partage(self, **champs) -> Project:
        """Projet d'Alice, Bob en édition, Chloé en lecture."""
        projet = self.projet(**champs)
        ProjectShare.objects.create(project=projet, email=BOB, role=ProjectShare.Role.EDITOR)
        ProjectShare.objects.create(project=projet, email=CHLOE, role=ProjectShare.Role.VIEWER)
        return projet

    def espace(self, projet: Project) -> str:
        """Nom du partage storage qui porte les fichiers de `projet`."""
        return storage_client.namespace_for_project(projet.root_project.pk)

    def deposer(self, projet: Project, chemin: str, octets: bytes = b'octets') -> str:
        self.storage.fichiers[(self.espace(projet), chemin)] = octets
        return chemin

    def maillage(self, projet: Project, version=1, ply=None, step=None, gltf=None, **champs) -> Mesh:
        base = f'projects/{projet.pk}/meshes/m_v{version}'
        mesh = Mesh(project=projet, version=version, **champs)
        mesh.file = self.deposer(projet, f'{base}.ply', ply if ply is not None else boite_ply())
        if step is not None:
            mesh.step_file = self.deposer(projet, f'{base}.step', step)
        if gltf is not None:
            mesh.gltf_file = self.deposer(projet, f'{base}.glb', gltf)
        mesh.save()
        return mesh

    def assemblage(self, owner=ALICE, statut=Project.DRAFT, contrainte=True):
        """Assemblage + une sous-partie + son maillage (STEP) + l'instance qui le
        place (+ la contrainte FIXED qui l'ancre)."""
        asm = self.projet(owner, name='Assemblage', project_type=Project.ASSEMBLY,
                          assembly_status=statut)
        piece = self.projet(owner, name='Pièce', parent_project=asm)
        mesh = self.maillage(piece, step=b'STEP de la piece')
        inst = CadAssemblyInstance.objects.create(
            assembly_project=asm, source_project=piece, source_mesh=mesh, label='piece-1',
        )
        if contrainte:
            CadAssemblyConstraint.objects.create(
                assembly_project=asm, constraint_type=CadAssemblyConstraint.FIXED,
                instance_a=inst, reference_a={'kind': 'face', 'index': 0},
            )
        return asm, piece, mesh, inst
