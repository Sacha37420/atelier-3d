"""Entrées mal formées et pannes de storage : une réponse lisible (400, 404,
502), jamais une erreur 500."""
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
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
