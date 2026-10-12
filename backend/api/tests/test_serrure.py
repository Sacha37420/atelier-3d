"""I3.1 — Serrure de l'API (Verrou 2 de CLAUDE.md) : de vrais jetons RS256,
signés par une clé locale, passent par la classe d'authentification réelle."""
import re
import time
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import TestCase
from rest_framework.test import APIClient

from api import urls as api_urls
from api.authentication import KeycloakJWTAuthentication
from api.models import UserRecord

from .helpers import ALICE, LabMixin, _RSA, jeton

REFUS = (401, 403)


class SerrureTests(LabMixin, TestCase):

    def setUp(self):
        self.monter_le_lab()

    def appel(self, tok=None, chemin='/api/me/', en_parametre=False):
        client = APIClient()
        if tok is None:
            return client.get(chemin)
        if en_parametre:
            return client.get(chemin, {'token': tok})
        return client.get(chemin, HTTP_AUTHORIZATION=f'Bearer {tok}')

    def test_sans_jeton_refuse(self):
        self.assertIn(self.appel().status_code, REFUS)

    def test_membre_accepte_et_identite_tiree_du_jeton(self):
        resp = self.appel(jeton(ALICE, ['developers', 'amis']))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['email'], ALICE)
        self.assertEqual(resp.json()['groups'], ['developers', 'amis'])
        self.assertTrue(resp.json()['is_new'])
        self.assertTrue(UserRecord.objects.filter(email=ALICE).exists())

    def test_chacun_des_groupes_requis_suffit(self):
        for groupe in ('developers', 'famille', 'amis'):
            with self.subTest(groupe=groupe):
                self.assertEqual(self.appel(jeton(groups=['autre', groupe])).status_code, 200)

    def test_jeton_d_un_autre_client_refuse(self):
        # admin-cli : client public du realm, password grant ouvert à tout compte.
        for azp in ('admin-cli', 'storage', 'atelier-3d-admin', 'account', None):
            with self.subTest(azp=azp):
                resp = self.appel(jeton(azp=azp))
                self.assertEqual(resp.status_code, 403)
                self.assertIn('autre client', resp.json()['detail'])

    def test_aucun_groupe_requis_refuse(self):
        for groups in (None, [], ['admins'], ['Developers'], ['developers2']):
            with self.subTest(groups=groups):
                resp = self.appel(jeton(groups=groups))
                self.assertEqual(resp.status_code, 403)
                self.assertIn('groupe(s)', resp.json()['detail'])

    def test_jeton_expire_refuse(self):
        resp = self.appel(jeton(exp=int(time.time()) - 60))
        self.assertEqual(resp.status_code, 403)
        self.assertIn('Token invalide', resp.json()['detail'])

    def test_signature_d_une_autre_cle_refusee(self):
        autre = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertEqual(self.appel(jeton(cle=autre)).status_code, 403)

    def test_jeton_non_signe_ou_illisible_refuse(self):
        sans_signature = jwt.encode(
            {'email': ALICE, 'groups': ['developers'], 'azp': 'atelier-3d',
             'exp': int(time.time()) + 300}, None, algorithm='none')
        tete, corps, _ = jeton().split('.')
        for nom, tok in (('alg none', sans_signature), ('signature retirée', f'{tete}.{corps}.'),
                         ('charabia', 'pas-un-jeton')):
            with self.subTest(cas=nom):
                self.assertEqual(self.appel(tok).status_code, 403)

    def test_corps_modifie_apres_signature_refuse(self):
        tete, _, signature = jeton(groups=['developers']).split('.')
        _, corps_etranger, _ = jeton('mallory@ssolab.local').split('.')
        self.assertEqual(self.appel(f'{tete}.{corps_etranger}.{signature}').status_code, 403)

    def _vrai_client_jwks(self, jwks=None, panne=None):
        """Un vrai `PyJWKClient` (recherche de la clé par `kid` comprise) : seul
        l'appel réseau `fetch_data` est remplacé."""
        client = jwt.PyJWKClient('http://keycloak.test/realms/ssolab/protocol/openid-connect/certs')
        fetch = mock.patch.object(client, 'fetch_data', side_effect=panne, return_value=jwks)
        fetch.start()
        self.addCleanup(fetch.stop)
        patcher = mock.patch.object(KeycloakJWTAuthentication, '_get_jwks_client',
                                    classmethod(lambda cls: client))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cle_de_signature_inconnue_refusee_sans_erreur_500(self):
        """`kid` absent du JWKS : PyJWKClientError n'hérite pas d'InvalidTokenError.
        Un jeton forgé avec un `kid` quelconque faisait répondre 500 (corrigé par
        l'architecte dans le gabarit, commit e1bb26b ici)."""
        cle = jwt.algorithms.RSAAlgorithm.to_jwk(_RSA.public_key(), as_dict=True)
        self._vrai_client_jwks({'keys': [{**cle, 'kid': 'cle-du-realm', 'use': 'sig', 'alg': 'RS256'}]})
        entetes = lambda kid: jwt.encode(jwt.decode(jeton(), options={'verify_signature': False}),
                                         _RSA, algorithm='RS256', headers={'kid': kid})
        self.assertEqual(self.appel(entetes('cle-du-realm')).status_code, 200)     # le chemin réel fonctionne
        for kid in ('cle-forgee', ''):
            with self.subTest(kid=kid):
                resp = self.appel(entetes(kid))
                self.assertEqual(resp.status_code, 403)
                self.assertIn('Token invalide', resp.json()['detail'])
        self.assertEqual(self.appel(jeton()).status_code, 403)                     # jeton sans `kid` du tout

    def test_keycloak_injoignable_refus_et_non_erreur_500(self):
        self._vrai_client_jwks(panne=jwt.exceptions.PyJWKClientConnectionError('keycloak arrêté'))
        resp = self.appel(jwt.encode({'email': ALICE, 'azp': 'atelier-3d', 'groups': ['developers'],
                                      'exp': int(time.time()) + 300}, _RSA, algorithm='RS256',
                                     headers={'kid': 'cle-du-realm'}))
        self.assertEqual(resp.status_code, 403)

    def test_sans_email_refuse(self):
        resp = self.appel(jeton(email=None))
        self.assertEqual(resp.status_code, 403)
        self.assertIn("'email'", resp.json()['detail'])

    def test_le_jeton_en_parametre_d_url_subit_les_memes_controles(self):
        self.assertEqual(self.appel(jeton(), en_parametre=True).status_code, 200)
        for nom, tok in (('admin-cli', jeton(azp='admin-cli')), ('sans groupe', jeton(groups=[])),
                         ('expiré', jeton(exp=int(time.time()) - 60))):
            with self.subTest(cas=nom):
                self.assertEqual(self.appel(tok, en_parametre=True).status_code, 403)

    def test_aucune_route_n_est_ouverte_sauf_les_trois_publiques(self):
        """Inventaire tiré de api/urls.py : toute route ajoutée sans protection
        fait rougir ce test, sans qu'il faille penser à l'y inscrire."""
        routes = [str(p.pattern) for p in api_urls.urlpatterns]
        publiques = [r for r in routes if r.startswith('public/')]
        self.assertEqual(publiques, [
            'public/assemblies/', 'public/assemblies/<int:pk>/', 'public/assemblies/<int:pk>/<str:fmt>/',
        ])
        etranger = jeton(azp='admin-cli')
        chemins = ['/api/' + re.sub(r'<\w+:\w+>', '1', r) for r in routes if r not in publiques]
        chemins.append('/media/projects/1/photos/a.jpg')
        self.assertGreater(len(chemins), 35)
        for chemin in chemins:
            for verbe in ('get', 'post', 'patch', 'delete'):
                with self.subTest(chemin=chemin, verbe=verbe):
                    self.assertIn(getattr(APIClient(), verbe)(chemin).status_code, REFUS)
                    resp = getattr(APIClient(), verbe)(chemin, HTTP_AUTHORIZATION=f'Bearer {etranger}')
                    self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.storage.appels, [])   # rien n'a atteint storage

    def test_la_documentation_de_l_api_n_existe_pas_en_service(self):
        for chemin in ('/api/schema/', '/api/docs/', '/'):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel(jeton(), chemin).status_code, 404)
