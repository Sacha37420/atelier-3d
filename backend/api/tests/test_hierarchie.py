"""Chantier « storage-hierarchie » (contrat v2) : partages de projet rangés
sous le conteneur ``atelier-3d`` (I1) et commande de rattachement de
l'existant (I3), contre le storage simulé de ``helpers``."""
import io
from contextlib import redirect_stderr, redirect_stdout

from unittest import mock

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase

from api import storage_client
from api.models import Photo

from .helpers import ALICE, BOB, LabMixin

C = storage_client.CONTAINER


class Base(LabMixin, TestCase):
    def setUp(self):
        self.monter_le_lab()

    def commande(self, *args):
        """(code de sortie, sortie) — 0 si la commande se termine normalement."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                call_command('rattacher_partages_storage', *args, stdout=out, stderr=err)
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue() + err.getvalue()


class CreationRattacheeTests(Base):

    def test_creation_avec_parent_conteneur_cree_au_besoin(self):
        p = self.projet(ALICE)
        storage_client.sync_project_share(p)
        ns = self.espace(p)
        self.assertEqual(self.storage.parents, {C: None, ns: C})
        self.assertEqual(self.storage.partages[ns], {ALICE: 'write'})

    def test_conteneur_existant_et_memorise_par_processus(self):
        self.storage.partages[C] = {}
        storage_client.sync_project_share(self.projet(ALICE))
        storage_client.sync_project_share(self.projet(BOB))
        self.assertEqual(len(self.storage.appels_vers(f'/api/shares/{C}/members/', 'GET')), 1)
        self.assertEqual({n: par for n, par in self.storage.parents.items() if n != C},
                         {n: C for n in self.storage.partages if n != C})

    def test_repli_sans_parent_sur_400_parent(self):
        self.storage.refuser_parent = True
        p = self.projet(ALICE)
        with self.assertLogs('api.storage_client', 'WARNING') as logs:
            storage_client.sync_project_share(p)
        ns = self.espace(p)
        self.assertIn(ns, self.storage.partages)
        self.assertIsNone(self.storage.parents[ns])
        self.assertNotIn('http', ''.join(logs.output))
        self.assertNotIn('jeton', ''.join(logs.output).lower())

    def test_repli_quand_le_conteneur_appartient_a_un_autre(self):
        self.storage.etrangers.add(C)
        p = self.projet(ALICE)
        with self.assertLogs('api.storage_client', 'WARNING'):
            storage_client.sync_project_share(p)
        ns = self.espace(p)
        self.assertIsNone(self.storage.parents[ns])
        self.assertEqual(self.storage.partages[ns], {ALICE: 'write'})
        # Indisponible n'est pas mémorisé : la création suivante réessaie.
        self.assertFalse(storage_client._container_ready)

    def test_enfant_deja_existant_400_name_est_un_succes(self):
        p = self.projet(ALICE)
        ns = self.espace(p)
        reel = storage_client.list_members
        sondes = []

        def list_members(nom):
            # Course : le partage apparaît entre la sonde et la création.
            if nom == ns and not sondes:
                sondes.append(nom)
                self.storage.partages[ns] = {}
                self.storage.parents[ns] = None
                raise storage_client.StorageClientError('absent')
            return reel(nom)
        with mock.patch.object(storage_client, 'list_members', side_effect=list_members):
            storage_client.sync_project_share(p)        # ne lève pas
        self.assertEqual(self.storage.partages[ns], {ALICE: 'write'})

    def test_echec_du_rattachement_ne_fait_pas_echouer_la_creation_du_projet(self):
        self.storage.refuser_parent = True
        self.storage.etrangers.add(C)
        with self.assertLogs('api.storage_client', 'WARNING'):
            r = self.client_de(ALICE).post('/api/projects/', {'name': 'Neuf'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        ns = storage_client.namespace_for_project(r.json()['id'])
        self.assertEqual(self.storage.partages[ns], {ALICE: 'write'})

    def test_echec_du_rattachement_ne_fait_pas_echouer_un_depot(self):
        self.storage.refuser_parent = True
        p = self.projet(ALICE)
        with self.assertLogs('api.storage_client', 'WARNING'):
            storage_client.sync_project_share(p)
        photo = Photo(project=p)
        photo.file.save('vue.jpg', ContentFile(b'jpeg'), save=True)
        self.assertEqual(self.storage.fichiers[(self.espace(p), photo.file.name)], b'jpeg')


class CommandeRattachementTests(Base):

    def setUp(self):
        super().setUp()
        self.p36 = self.projet(ALICE, name='36')
        self.p38 = self.projet(BOB, name='38')
        self.range = self.projet(ALICE, name='rangé')
        self.sans_fichier = self.projet(ALICE, name='sans partage')
        self.piece = self.projet(ALICE, name='pièce', parent_project=self.p36)
        for p in (self.p36, self.p38):
            self.storage.partages[self.espace(p)] = {}
            self.storage.parents[self.espace(p)] = None
        self.storage.partages[C] = {}
        self.storage.parents[C] = None
        self.storage.partages[self.espace(self.range)] = {}
        self.storage.parents[self.espace(self.range)] = C
        # Partage étranger aux données de l'app : jamais touché.
        self.storage.partages['atelier-3d-project-999'] = {}

    def test_check_est_en_lecture_seule_et_sort_en_1(self):
        code, sortie = self.commande('--check')
        self.assertEqual(code, 1)
        self.assertIn(self.espace(self.p36), sortie)
        self.assertIn(self.espace(self.p38), sortie)
        self.assertNotIn(self.espace(self.range), sortie)
        self.assertEqual(self.storage.appels_vers('/api/shares/', 'PATCH'), [])
        self.assertEqual(self.storage.appels_vers('/api/shares/', 'POST'), [])
        self.assertIsNone(self.storage.parents[self.espace(self.p36)])
        self.assertNotIn('http', sortie)

    def test_rattache_puis_rien_a_faire_au_second_passage(self):
        code, _ = self.commande()
        self.assertEqual(code, 0)
        self.assertEqual(self.storage.parents[self.espace(self.p36)], C)
        self.assertEqual(self.storage.parents[self.espace(self.p38)], C)
        self.assertEqual(len(self.storage.appels_vers('/api/shares/', 'PATCH')), 2)
        # Absent de storage : ignoré, pas créé ; sous-partie : pas de partage à elle.
        self.assertNotIn(self.espace(self.sans_fichier), self.storage.partages)
        self.assertNotIn(f'atelier-3d-project-{self.piece.pk}', self.storage.partages)
        self.assertIsNone(self.storage.parents.get('atelier-3d-project-999'))
        self.assertEqual(self.commande('--check')[0], 0)
        self.assertEqual(self.commande()[0], 0)
        self.assertEqual(len(self.storage.appels_vers('/api/shares/', 'PATCH')), 2)

    def test_cree_le_conteneur_s_il_manque(self):
        del self.storage.partages[C]
        self.assertEqual(self.commande('--check')[0], 1)
        self.assertEqual(self.commande()[0], 0)
        self.assertIn(C, self.storage.partages)
        self.assertEqual(self.storage.parents[self.espace(self.p36)], C)

    def test_conteneur_a_un_autre_code_1_rien_touche(self):
        del self.storage.partages[C]
        self.storage.etrangers.add(C)
        code, _ = self.commande()
        self.assertEqual(code, 1)
        self.assertEqual(self.storage.appels_vers('/api/shares/', 'PATCH'), [])

    def test_patch_refuse_compte_en_echec_et_continue(self):
        self.storage.refuser_parent = True
        code, sortie = self.commande()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.storage.appels_vers('/api/shares/', 'PATCH')), 2)
        self.assertIn('2 échec', sortie)

    def test_patch_404_ignore(self):
        reel = storage_client.requests.patch.side_effect
        ns = self.espace(self.p36)

        def patch(url, **kw):
            if url.endswith(f'/api/shares/{ns}/'):
                self.storage.partages.pop(ns, None)     # disparu entre la lecture et le PATCH
            return reel(url, **kw)
        storage_client.requests.patch.side_effect = patch
        code, _ = self.commande()
        self.assertEqual(code, 0)
        self.assertEqual(self.storage.parents[self.espace(self.p38)], C)

    def test_storage_injoignable_code_1(self):
        self.storage.arrete = True
        self.assertEqual(self.commande('--check')[0], 1)
        self.assertEqual(self.commande()[0], 1)
