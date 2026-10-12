"""Entrées mal formées et pannes de storage : une réponse lisible (400, 404,
502), jamais une erreur 500."""
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase

from api.models import CadAssemblyInstance, Job, Part, Photo

from .helpers import ALICE, LabMixin, temporaires


class Base(LabMixin, TestCase):
    def setUp(self):
        self.monter_le_lab()
        self.alice = self.client_de(ALICE)
        # Le client de test relance par défaut l'exception d'une vue : on veut ici
        # voir la réponse 500 qu'un vrai client recevrait.
        self.alice.raise_request_exception = False
        self.p = self.projet(ALICE, scale_meters_per_unit=1.0)
        self.mesh = self.maillage(self.p)
        self.a = Part.objects.create(mesh=self.mesh, name='A', face_ids=[0, 1])
        self.b = Part.objects.create(mesh=self.mesh, name='B', face_ids=[2, 3])


class IdentifiantsMalFormesTests(Base):

    def test_instance_d_assemblage(self):
        asm, piece, mesh, inst = self.assemblage(ALICE)
        for valeur in ('abc', '', None, [1], {'id': 1}, 1.5, '12; drop'):
            with self.subTest(source_project=valeur):
                resp = self.alice.post(f'/api/projects/{asm.id}/cad-instances/',
                                       {'source_project': valeur}, format='json')
                self.assertEqual(resp.status_code, 404)
        for valeur in ('abc', [1], {'id': 1}):
            with self.subTest(source_mesh=valeur):
                resp = self.alice.patch(f'/api/cad-instances/{inst.id}/', {'source_mesh': valeur}, format='json')
                self.assertEqual(resp.status_code, 404)
                resp = self.alice.post(f'/api/projects/{asm.id}/cad-instances/',
                                       {'source_project': piece.id, 'source_mesh': valeur}, format='json')
                self.assertEqual(resp.status_code, 404)
        self.assertEqual(CadAssemblyInstance.objects.count(), 1)
        inst.refresh_from_db()
        self.assertEqual(inst.source_mesh_id, mesh.id)

    def test_axe_de_jointure(self):
        for valeur in ('abc', '', '1.5'):
            with self.subTest(other=valeur):
                resp = self.alice.get(f'/api/parts/{self.a.id}/suggest-axis/', {'other': valeur})
                self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.alice.get(f'/api/parts/{self.a.id}/suggest-axis/').status_code, 404)

    def test_filtre_de_la_liste_des_jobs(self):
        Job.objects.create(project=self.p, kind=Job.REPAIR, status=Job.DONE)
        for valeur in ('abc', '1.5', '1 or 1=1'):
            with self.subTest(project=valeur):
                self.assertEqual(self.alice.get('/api/jobs/', {'project': valeur}).status_code, 400)
        self.assertEqual(len(self.alice.get('/api/jobs/', {'project': self.p.id}).json()), 1)
        self.assertEqual(len(self.alice.get('/api/jobs/', {'project': ''}).json()), 1)     # filtre vide = pas de filtre


class FichierDeMaillageDisparuTests(Base):
    """Le maillage existe en base, son fichier n'est plus dans storage : 404,
    comme le font déjà l'export et les suggestions."""

    def setUp(self):
        super().setUp()
        self.storage.fichiers.clear()

    def test_creation_et_retouche_d_une_partie(self):
        avant = temporaires()
        resp = self.alice.post(f'/api/meshes/{self.mesh.id}/parts/', {'name': 'C', 'face_ids': [4]}, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('introuvable', resp.json()['detail'])
        resp = self.alice.patch(f'/api/parts/{self.a.id}/', {'face_ids': [5]}, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Part.objects.count(), 2)
        self.a.refresh_from_db()
        self.assertEqual(self.a.face_ids, [0, 1])
        self.assertEqual(temporaires(), avant)
        # Renommer ne lit pas le fichier : toujours possible.
        self.assertEqual(self.alice.patch(f'/api/parts/{self.a.id}/', {'name': 'Capot'}, format='json').status_code, 200)

    def test_les_vues_voisines_rendent_deja_404(self):
        for chemin, params in ((f'/api/meshes/{self.mesh.id}/export/', {}),
                               (f'/api/meshes/{self.mesh.id}/auto-orient/', {}),
                               (f'/api/parts/{self.a.id}/suggest-axis/', {'other': self.b.id})):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.alice.get(chemin, params).status_code, 404)
        self.assertEqual(self.alice.post(f'/api/meshes/{self.mesh.id}/parts/suggest/', {}, format='json').status_code, 404)


class StorageArreteTests(Base):
    """storage injoignable : 502 « Stockage indisponible », pas 500."""

    def setUp(self):
        super().setUp()
        self.storage.arrete = True

    def assert502(self, resp):
        self.assertEqual(resp.status_code, 502)
        self.assertIn('Stockage indisponible', resp.json()['detail'])

    def test_lectures_d_un_maillage(self):
        avant = temporaires()
        for chemin, params in ((f'/api/meshes/{self.mesh.id}/export/', {}),
                               (f'/api/meshes/{self.mesh.id}/auto-orient/', {}),
                               (f'/api/parts/{self.a.id}/suggest-axis/', {'other': self.b.id})):
            with self.subTest(chemin=chemin):
                self.assert502(self.alice.get(chemin, params))
        self.assertEqual(temporaires(), avant)

    def test_parties_suggestion_creation_retouche(self):
        self.assert502(self.alice.post(f'/api/meshes/{self.mesh.id}/parts/suggest/', {}, format='json'))
        self.assert502(self.alice.post(f'/api/meshes/{self.mesh.id}/parts/', {'name': 'C', 'face_ids': [4]}, format='json'))
        self.assert502(self.alice.patch(f'/api/parts/{self.a.id}/', {'face_ids': [5]}, format='json'))
        self.assertEqual(Part.objects.count(), 2)

    def test_depot_de_photo_rien_n_est_garde(self):
        # Le bloc `atomic` n'est là que pour le test : l'échec survient pendant
        # `Model.save()`, qui marque la transaction englobante de TestCase à annuler ;
        # ce point de reprise la rend de nouveau utilisable pour compter ensuite.
        with transaction.atomic():
            resp = self.alice.post(f'/api/projects/{self.p.id}/photos/',
                                   {'files': [SimpleUploadedFile('a.jpg', b'x')]}, format='multipart')
        self.assert502(resp)
        self.assertEqual(Photo.objects.count(), 0)

    def test_label_sur_une_photo(self):
        photo = Photo.objects.create(project=self.p, file=f'projects/{self.p.id}/photos/a.jpg',
                                     region_map=f'projects/{self.p.id}/photos/regions/1_regions.npz')
        self.assert502(self.alice.post(f'/api/photos/{photo.id}/labels/',
                                       {'x': 0.5, 'y': 0.5, 'semantic_class': 'mur'}, format='json'))
