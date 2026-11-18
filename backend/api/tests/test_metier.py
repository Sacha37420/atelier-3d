"""Règles métier : indices de faces d'une partie, géométrie d'une jointure,
échelle de calibration, message rendu quand storage est en panne."""
from django.test import TestCase
from rest_framework.test import APIClient

from api import storage_backend, tasks
from api.models import Job, Joint, Part, Project

from .helpers import ALICE, LabMixin, boite_ply


class Base(LabMixin, TestCase):
    def setUp(self):
        self.monter_le_lab()
        self.alice = self.client_de(ALICE)
        self.p = self.projet(ALICE, scale_meters_per_unit=1.0)
        self.mesh = self.maillage(self.p, face_count=12)      # boîte : 12 triangles
        self.a = Part.objects.create(mesh=self.mesh, name='A', face_ids=[0, 1])
        self.b = Part.objects.create(mesh=self.mesh, name='B', face_ids=[2, 3])


class IndicesDeFacesTests(Base):
    """Lot 2 : `face_ids` mal formés ou hors du maillage → 400, rien n'est écrit."""

    def test_indices_mal_formes_ou_hors_du_maillage_refuses(self):
        for valeur in (['a'], [1.5], [-1], [True], [12], [0, 99], [None]):
            with self.subTest(face_ids=valeur):
                resp = self.alice.post(f'/api/meshes/{self.mesh.id}/parts/',
                                       {'name': 'C', 'face_ids': valeur}, format='json')
                self.assertEqual(resp.status_code, 400)
                resp = self.alice.patch(f'/api/parts/{self.a.id}/', {'face_ids': valeur}, format='json')
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(Part.objects.count(), 2)
        self.a.refresh_from_db()
        self.assertEqual(self.a.face_ids, [0, 1])


class GeometrieDeJointureTests(Base):
    """Un axe à trois nombres, une direction non nulle, des bornes dans l'ordre."""

    def lier(self, **corps):
        return self.alice.post(f'/api/meshes/{self.mesh.id}/joints/', {
            'parent_part': self.a.id, 'child_part': self.b.id, 'joint_type': 'revolute', **corps,
        }, format='json')

    def test_creation_refusee_si_l_axe_ou_les_bornes_sont_incoherents(self):
        for corps in ({'axis_direction': [0, 0, 0]}, {'axis_origin': [1, 2]},
                      {'axis_origin': ['a', 0, 0]}, {'axis_direction': 'z'},
                      {'limit_min': 10, 'limit_max': -10}, {'limit_min': 'abc'}):
            with self.subTest(corps=corps):
                self.assertEqual(self.lier(**corps).status_code, 400)
        self.assertEqual(Joint.objects.count(), 0)
        resp = self.lier(axis_origin=[0.5, 0, 1], axis_direction=[0, 1, 0], limit_min=-90, limit_max=90)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual((resp.json()['limit_min'], resp.json()['limit_max']), (-90, 90))

    def test_modification_refusee_si_elle_inverse_les_bornes(self):
        joint = Joint.objects.create(parent_part=self.a, child_part=self.b, joint_type='revolute',
                                     axis_origin=[0, 0, 0], axis_direction=[0, 0, 1],
                                     limit_min=-90, limit_max=90)
        url = f'/api/joints/{joint.id}/'
        self.assertEqual(self.alice.patch(url, {'limit_min': 100}, format='json').status_code, 400)
        self.assertEqual(self.alice.patch(url, {'axis_direction': [0, 0, 0]}, format='json').status_code, 400)
        joint.refresh_from_db()
        self.assertEqual((joint.limit_min, joint.axis_direction), (-90, [0, 0, 1]))
        # Retirer une borne reste possible : le curseur reprend alors sa plage par défaut.
        self.assertEqual(self.alice.patch(url, {'limit_max': None}, format='json').status_code, 200)
        joint.refresh_from_db()
        self.assertIsNone(joint.limit_max)


class EchelleTests(Base):
    """L'échelle multiplie chaque coordonnée à l'export : strictement positive, ou vide."""

    def test_echelle_nulle_ou_negative_refusee(self):
        url = f'/api/projects/{self.p.id}/'
        for valeur in (0, -0.01):
            with self.subTest(echelle=valeur):
                resp = self.alice.patch(url, {'scale_meters_per_unit': valeur}, format='json')
                self.assertEqual(resp.status_code, 400)
        self.p.refresh_from_db()
        self.assertEqual(self.p.scale_meters_per_unit, 1.0)
        self.assertEqual(self.alice.patch(url, {'scale_meters_per_unit': 0.002}, format='json').status_code, 200)
        self.assertEqual(self.alice.patch(url, {'scale_meters_per_unit': None}, format='json').status_code, 200)
        self.p.refresh_from_db()
        self.assertIsNone(self.p.scale_meters_per_unit)


class StoragePanneMessageTests(Base):
    """L'erreur de `requests` porte l'adresse interne de storage : le corps rendu
    reste un message fixe, y compris sur la route publique."""

    def test_message_fixe(self):
        asm, piece, mesh, inst = self.assemblage(ALICE, statut=Project.SOLVED)
        self.maillage(asm, gltf=b'glTF')
        self.storage.arrete = True
        attendu = (502, {'detail': 'Stockage indisponible.'})
        for client, chemin in ((self.alice, f'/api/meshes/{self.mesh.id}/export/'),
                               (self.alice, f'/media/{self.mesh.file.name}'),
                               (APIClient(), f'/api/public/assemblies/{asm.id}/gltf/')):
            with self.subTest(chemin=chemin):
                resp = client.get(chemin)
                self.assertEqual((resp.status_code, resp.json()), attendu)

    def test_job_interrompu_par_storage_message_fixe(self):
        job = Job.objects.create(project=self.p, kind=Job.MESH_IMPORT, owner_email=ALICE,
                                 params={'primary_filename': 'boite.ply'})
        travail = storage_backend.scratch_dir('projects', str(job.project_id), f'work_{job.id}')
        (travail / 'boite.ply').write_bytes(boite_ply())
        self.storage.arrete = True
        tasks.run_mesh_import.apply(args=[job.id])
        job.refresh_from_db()
        self.assertEqual((job.status, job.message), (Job.ERROR, tasks.STORAGE_FAILURE_MESSAGE))
