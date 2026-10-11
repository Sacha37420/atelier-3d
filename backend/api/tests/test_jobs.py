"""Orchestration des jobs lourds : préconditions de lancement, verrou global
« un seul job à la fois », envoi à Celery au commit seulement, transitions
PENDING → RUNNING → DONE / ERROR jouées en local sur les vraies tâches.

Aucun calcul lourd : COLMAP, OpenMVS, FreeCAD et pymeshlab sont remplacés là où
la tâche les appellerait ; seuls trimesh (une boîte) et build123d (une boîte)
tournent réellement.
"""
import functools
import io
import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import TestCase, TransactionTestCase
from PIL import Image

from api import cad_assemble, storage_backend, tasks
from api.models import (
    CadAssemblyConstraint, CadAssemblyInstance, CadOperation, Job, Mesh, Photo, PhotoLabel,
    Project,
)

from .helpers import ALICE, BOB, CHLOE, DAVID, LabMixin, boite_ply, temporaires

TACHES = ('run_reconstruction', 'run_mesh_import', 'run_repair', 'run_facade_segmentation',
          'run_cad_build', 'run_cad_assemble')


@functools.lru_cache(maxsize=None)
def boite_step() -> bytes:
    import tempfile
    import build123d as bd
    with tempfile.TemporaryDirectory() as dossier:
        chemin = Path(dossier) / 'boite.step'
        bd.export_step(bd.Box(10, 20, 30), str(chemin))
        return chemin.read_bytes()


def jpeg(largeur=64, hauteur=48) -> bytes:
    tampon = io.BytesIO()
    Image.new('RGB', (largeur, hauteur), (120, 90, 60)).save(tampon, format='JPEG')
    return tampon.getvalue()


class Base(LabMixin, TestCase):
    def setUp(self):
        self.monter_le_lab()
        self.alice, self.bob = self.client_de(ALICE), self.client_de(BOB)
        self.chloe, self.david = self.client_de(CHLOE), self.client_de(DAVID)
        self.p = self.projet_partage(name='Moulin')
        # Les six `.delay` : aucun message ne part vers un courtier absent.
        self.envois = {}
        for nom in TACHES:
            patcher = mock.patch(f'api.views.{nom}.delay')
            self.envois[nom] = patcher.start()
            self.addCleanup(patcher.stop)

    def envoyes(self):
        return {nom: m.call_args_list for nom, m in self.envois.items() if m.called}

    def trois_photos(self, projet=None):
        projet = projet or self.p
        return [Photo.objects.create(project=projet, order=i,
                                     file=f'projects/{projet.id}/photos/{i}.jpg') for i in range(3)]

    def pret_pour_la_facade(self):
        self.maillage(self.p)
        photo = Photo.objects.create(project=self.p, file=f'projects/{self.p.id}/photos/a.jpg',
                                     camera_pose={'quaternion_wxyz': [1, 0, 0, 0]})
        PhotoLabel.objects.create(photo=photo, semantic_class='mur', region_index=0)

    def lancements(self):
        """(nom de la tâche, type de job, fonction qui lance) pour les six jobs
        lourds, chacun avec ses préconditions remplies."""
        self.trois_photos()
        self.pret_pour_la_facade()
        CadOperation.objects.create(project=self.p, order=1, operation_type='PRIMITIVE_BOX')
        asm, *_ = self.assemblage(ALICE)
        base = f'/api/projects/{self.p.id}'
        return [
            ('run_reconstruction', Job.RECONSTRUCTION, lambda: self.alice.post(f'{base}/reconstruct/', {}, format='json')),
            ('run_mesh_import', Job.MESH_IMPORT, lambda: self.alice.post(
                f'{base}/mesh-import/', {'files': [SimpleUploadedFile('m.ply', boite_ply())]}, format='multipart')),
            ('run_repair', Job.REPAIR, lambda: self.alice.post(f'{base}/repair/', {}, format='json')),
            ('run_facade_segmentation', Job.SEGMENTATION_FACADE, lambda: self.alice.post(f'{base}/facade/', {}, format='json')),
            ('run_cad_build', Job.CAD_BUILD, lambda: self.alice.post(f'{base}/cad-build/', {}, format='json')),
            ('run_cad_assemble', Job.CAD_ASSEMBLE, lambda: self.alice.post(
                f'/api/projects/{asm.id}/cad-assemble/', {}, format='json')),
        ]


class LancementTests(Base):

    def test_chaque_job_nait_en_attente_et_ne_part_qu_au_commit(self):
        for nom, kind, lancer in self.lancements():
            with self.subTest(job=kind):
                with self.captureOnCommitCallbacks() as rappels:
                    resp = lancer()
                self.assertEqual(resp.status_code, 202)
                job = Job.objects.get(pk=resp.json()['id'])
                self.assertEqual((job.kind, job.status, job.progress, job.owner_email),
                                 (kind, Job.PENDING, 0, ALICE))
                # Rien n'est parti avant le commit : la tâche lirait un job pas encore visible.
                self.assertEqual(self.envoyes(), {})
                self.assertEqual(len(rappels), 1)
                rappels[0]()
                self.assertEqual(self.envoyes(), {nom: [mock.call(job.id)]})
                self.envois[nom].reset_mock()
                job.set_state(status=Job.DONE)

    def test_verrou_global_un_job_actif_bloque_tout_tous_projets_confondus(self):
        ailleurs = Job.objects.create(project=self.projet(DAVID), kind=Job.REPAIR, status=Job.PENDING)
        lancements = self.lancements()
        for statut in (Job.PENDING, Job.RUNNING):
            ailleurs.set_state(status=statut)
            for nom, kind, lancer in lancements:
                with self.subTest(statut=statut, job=kind):
                    with self.captureOnCommitCallbacks(execute=True):
                        resp = lancer()
                    self.assertEqual(resp.status_code, 409)
                    self.assertIn('un seul à la fois', resp.json()['detail'])
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(self.envoyes(), {})
        self.assertEqual(list(Path(self.scratch).rglob('*.ply')), [])   # import refusé : rien d'écrit

    def test_verrou_libere_par_la_fin_ou_l_echec_du_job(self):
        ancien = Job.objects.create(project=self.projet(DAVID), kind=Job.REPAIR, status=Job.RUNNING)
        url = f'/api/projects/{self.p.id}/repair/'
        self.maillage(self.p)
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 409)
        for statut in (Job.DONE, Job.ERROR):
            ancien.set_state(status=statut)
            resp = self.alice.post(url, {}, format='json')
            self.assertEqual(resp.status_code, 202)
            Job.objects.filter(pk=resp.json()['id']).update(status=Job.DONE)

    def test_l_etat_du_projet_reflete_son_job_actif(self):
        self.maillage(self.p)
        url = f'/api/projects/{self.p.id}/'
        self.assertFalse(self.alice.get(url).json()['has_active_job'])
        job_id = self.alice.post(f'{url}repair/', {}, format='json').json()['id']
        self.assertTrue(self.chloe.get(url).json()['has_active_job'])
        self.assertEqual(self.chloe.get(f'/api/jobs/{job_id}/').json()['status'], 'PENDING')
        Job.objects.get(pk=job_id).set_state(status=Job.ERROR, message='boum')
        self.assertFalse(self.chloe.get(url).json()['has_active_job'])
        self.assertEqual(self.chloe.get(f'/api/jobs/{job_id}/').json()['message'], 'boum')

    def test_reconstruction_preconditions(self):
        url = f'/api/projects/{self.p.id}/reconstruct/'
        Photo.objects.create(project=self.p, file='x/1.jpg')
        Photo.objects.create(project=self.p, file='x/2.jpg')
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 400)        # 2 photos
        Photo.objects.create(project=self.p, file='x/3.jpg')
        self.assertEqual(self.alice.post(url, {'preset': 'ultra'}, format='json').status_code, 400)
        self.assertEqual(Job.objects.count(), 0)
        resp = self.bob.post(url, {'preset': 'rapide'}, format='json')                   # un éditeur lance
        self.assertEqual(resp.status_code, 202)
        job = Job.objects.get()
        self.assertEqual((job.params, job.owner_email), ({'preset': 'rapide'}, BOB))

    def test_import_de_maillage_fichiers_controles_puis_poses_dans_le_repertoire_de_travail(self):
        url = f'/api/projects/{self.p.id}/mesh-import/'
        f = lambda nom, octets=b'x': SimpleUploadedFile(nom, octets)
        for nom, fichiers in (('aucun', []), ('extension', [f('m.obj'), f('virus.exe')]),
                              ('deux principaux', [f('a.obj'), f('b.ply')]),
                              ('aucun principal', [f('a.mtl'), f('t.png')])):
            with self.subTest(cas=nom):
                self.assertEqual(self.alice.post(url, {'files': fichiers}, format='multipart').status_code, 400)
        self.assertEqual(Job.objects.count(), 0)
        self.assertEqual([p for p in Path(self.scratch).rglob('*') if p.is_file()], [])

        resp = self.alice.post(url, {'files': [f('piece.OBJ', b'obj'), f('piece.mtl', b'mtl'), f('t.png', b'png')]},
                               format='multipart')
        self.assertEqual(resp.status_code, 202)
        job = Job.objects.get()
        self.assertEqual(job.params, {'primary_filename': 'piece.OBJ'})
        travail = Path(self.scratch) / 'projects' / str(self.p.id) / f'work_{job.id}'
        self.assertEqual(sorted(p.name for p in travail.iterdir()), ['piece.OBJ', 'piece.mtl', 't.png'])
        self.assertEqual((travail / 'piece.mtl').read_bytes(), b'mtl')
        self.assertEqual(self.storage.fichiers, {})     # le répertoire de travail n'est jamais dans storage

    def test_reparation_preconditions_et_cible_de_decimation(self):
        url = f'/api/projects/{self.p.id}/repair/'
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 400)        # aucun maillage
        self.maillage(self.p)
        for corps in ({'target_triangles': 0}, {'target_triangles': 'beaucoup'}, {'target_size_mb': -1},
                      {'target_size_mb': 'gros'}):
            with self.subTest(corps=corps):
                self.assertEqual(self.alice.post(url, corps, format='json').status_code, 400)
        self.assertEqual(Job.objects.count(), 0)
        cas = (({'target_triangles': '5000'}, 5000), ({}, None),
               ({'target_size_mb': 1}, round((1024 * 1024 - 84) / 50)))    # STL binaire : 84 o + 50 o/face
        for corps, attendu in cas:
            with self.subTest(corps=corps):
                resp = self.alice.post(url, corps, format='json')
                self.assertEqual(resp.status_code, 202)
                self.assertEqual(resp.json()['params'], {'target_faces': attendu})
                Job.objects.update(status=Job.DONE)

    def test_facade_exige_maillage_pose_et_label(self):
        url = f'/api/projects/{self.p.id}/facade/'
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 400)        # pas de maillage
        self.maillage(self.p)
        photo = Photo.objects.create(project=self.p, file='x/a.jpg')
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 400)        # pas de pose
        photo.camera_pose = {'quaternion_wxyz': [1, 0, 0, 0]}
        photo.save()
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 400)        # pas de label
        PhotoLabel.objects.create(photo=photo, semantic_class='toit', region_index=1)
        self.assertEqual(Job.objects.count(), 0)
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 202)

    def test_construction_cao_exige_un_historique_et_des_deflexions_numeriques(self):
        url = f'/api/projects/{self.p.id}/cad-build/'
        self.assertEqual(self.alice.post(url, {}, format='json').status_code, 400)
        CadOperation.objects.create(project=self.p, order=1, operation_type='PRIMITIVE_BOX')
        self.assertEqual(self.alice.post(url, {'linear_deflection': 'fin'}, format='json').status_code, 400)
        resp = self.alice.post(url, {'linear_deflection': '0.05', 'angular_deflection': 0.2}, format='json')
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()['params'], {'linear_deflection': 0.05, 'angular_deflection': 0.2})

    def test_estimations_avant_lancement(self):
        self.trois_photos()
        base = f'/api/projects/{self.p.id}'
        self.assertEqual(self.chloe.get(f'{base}/reconstruct/estimate/', {'preset': 'ultra'}).status_code, 400)
        rapide = self.chloe.get(f'{base}/reconstruct/estimate/', {'preset': 'rapide'}).json()
        precis = self.chloe.get(f'{base}/reconstruct/estimate/', {'preset': 'precis'}).json()
        self.assertEqual((rapide['n_photos'], rapide['warning_threshold_exceeded']), (3, False))
        self.assertGreater(precis['estimated_seconds'], rapide['estimated_seconds'])
        self.assertEqual(Job.objects.count(), 0)        # estimer ne lance rien
        facade = self.chloe.get(f'{base}/facade/estimate/').json()
        self.assertEqual((facade['n_photos'], facade['estimated_seconds']), (0, 30))     # aucune pose résolue


class AssemblageTests(Base):
    """Lancement de CAD_ASSEMBLE, instances épinglées sur une version de maillage."""

    def setUp(self):
        super().setUp()
        self.asm, self.piece, self.mesh, self.inst = self.assemblage(ALICE)
        self.url = f'/api/projects/{self.asm.id}/cad-assemble/'

    def test_reserve_aux_assemblages_qui_ont_une_contrainte(self):
        self.assertEqual(self.alice.post(f'/api/projects/{self.p.id}/cad-assemble/', {}, format='json').status_code, 404)
        CadAssemblyConstraint.objects.all().delete()
        self.assertEqual(self.alice.post(self.url, {}, format='json').status_code, 400)
        self.assertEqual(Job.objects.count(), 0)

    def test_instance_perimee_bloque_la_resolution_sauf_confirmation(self):
        self.maillage(self.piece, version=2, step=b'step v2')      # la pièce a été reconstruite depuis
        resp = self.alice.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['outdated_instances'], [{
            'instance_id': self.inst.id, 'label': 'piece-1', 'source_project': self.piece.id,
            'pinned_mesh_version': 1, 'latest_mesh_version': 2,
        }])
        self.assertEqual(Job.objects.count(), 0)
        fiche = self.alice.get(f'/api/projects/{self.asm.id}/cad-instances/').json()[0]
        self.assertEqual((fiche['is_outdated'], fiche['latest_mesh_version']), (True, 2))
        self.assertEqual(self.alice.post(self.url, {'confirm_outdated': True}, format='json').status_code, 202)

    def test_repointer_une_instance_remet_l_assemblage_en_brouillon(self):
        v2 = self.maillage(self.piece, version=2, step=b'step v2')
        self.inst.placement = {'position': [0, 0, 0], 'quaternion_xyzw': [0, 0, 0, 1]}
        self.inst.save()
        Project.objects.filter(pk=self.asm.pk).update(assembly_status=Project.SOLVED)
        resp = self.alice.patch(f'/api/cad-instances/{self.inst.id}/', {'source_mesh': v2.id}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('warning', resp.json())           # une contrainte référence cette instance
        self.assertEqual((resp.json()['is_outdated'], resp.json()['placement']), (False, None))
        self.asm.refresh_from_db()
        self.assertEqual(self.asm.assembly_status, Project.DRAFT)
        # Brouillon : il a quitté l'API publique.
        self.assertEqual(self.client.get(f'/api/public/assemblies/{self.asm.id}/').status_code, 404)
        self.assertEqual(self.alice.patch(f'/api/cad-instances/{self.inst.id}/', {}, format='json').status_code, 400)

    def test_ajout_d_instance_epingle_le_dernier_maillage_de_la_piece(self):
        v2 = self.maillage(self.piece, version=2, step=b'step v2')
        resp = self.alice.post(f'/api/projects/{self.asm.id}/cad-instances/',
                               {'source_project': self.piece.id, 'label': 'bis'}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual((resp.json()['source_mesh'], resp.json()['assembly_project']), (v2.id, self.asm.id))
        nue = self.projet(ALICE, name='Nue', parent_project=self.asm)
        resp = self.alice.post(f'/api/projects/{self.asm.id}/cad-instances/', {'source_project': nue.id}, format='json')
        self.assertEqual(resp.status_code, 400)         # pas encore de maillage : CAD_BUILD d'abord


class TachesTests(Base):
    """Les tâches Celery, jouées en local : toute issue laisse le job dans un
    état final (jamais bloqué en RUNNING, ce qui fermerait le verrou global)."""

    def job(self, kind, projet=None, **params):
        return Job.objects.create(project=projet or self.p, kind=kind, params=params, owner_email=ALICE)

    def jouer(self, tache, job):
        tache.apply(args=[job.id])
        job.refresh_from_db()
        return job

    def travail(self, job) -> Path:
        return storage_backend.scratch_dir('projects', str(job.project_id), f'work_{job.id}')

    # ── MESH_IMPORT (trimesh réel sur une boîte) ─────────────────────────────
    def test_import_reussi_produit_un_maillage_versionne_aux_chemins_logiques(self):
        self.maillage(self.p, version=4)
        job = self.job(Job.MESH_IMPORT, primary_filename='boite.ply')
        (self.travail(job) / 'boite.ply').write_bytes(boite_ply())
        job = self.jouer(tasks.run_mesh_import, job)
        self.assertEqual((job.status, job.progress), (Job.DONE, 100))
        self.assertTrue(job.celery_task_id)
        self.assertIsNotNone(job.duration_seconds)
        self.assertIn('8 sommets / 12 faces', job.message)
        self.assertIn('Échelle non calibrée', job.message)
        mesh = Mesh.objects.get(job=job)
        self.assertEqual((mesh.version, mesh.vertex_count, mesh.face_count, mesh.is_watertight), (5, 8, 12, True))
        self.assertEqual(mesh.file.name, f'projects/{self.p.id}/meshes/import_v5.ply')
        self.assertEqual(mesh.gltf_file.name, f'projects/{self.p.id}/meshes/import_v5.glb')
        espace = self.espace(self.p)
        self.assertTrue(self.storage.fichiers[(espace, mesh.file.name)].startswith(b'ply'))
        self.assertTrue(self.storage.fichiers[(espace, mesh.gltf_file.name)].startswith(b'glTF'))
        self.assertFalse(self.p.has_active_job)

    def test_import_dans_une_sous_partie_ecrit_dans_le_partage_de_la_racine(self):
        piece = self.projet(BOB, parent_project=self.p)
        job = self.job(Job.MESH_IMPORT, projet=piece, primary_filename='boite.ply')
        (self.travail(job) / 'boite.ply').write_bytes(boite_ply())
        self.assertEqual(self.jouer(tasks.run_mesh_import, job).status, Job.DONE)
        self.assertIn((self.espace(self.p), f'projects/{piece.id}/meshes/import_v1.ply'), self.storage.fichiers)

    def test_import_en_erreur_fichier_absent_ou_illisible(self):
        job = self.jouer(tasks.run_mesh_import, self.job(Job.MESH_IMPORT, primary_filename='absent.ply'))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('introuvable', job.message)
        job = self.job(Job.MESH_IMPORT, primary_filename='casse.ply')
        (self.travail(job) / 'casse.ply').write_bytes(b'ceci n est pas un maillage')
        self.assertEqual(self.jouer(tasks.run_mesh_import, job).status, Job.ERROR)
        self.assertEqual(Mesh.objects.count(), 0)
        self.assertEqual(self.storage.fichiers, {})

    def test_storage_arrete_pendant_l_export_le_job_finit_en_erreur_et_libere_le_verrou(self):
        job = self.job(Job.MESH_IMPORT, primary_filename='boite.ply')
        (self.travail(job) / 'boite.ply').write_bytes(boite_ply())
        self.storage.arrete = True
        job = self.jouer(tasks.run_mesh_import, job)
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('storage injoignable', job.message)
        self.assertEqual(Mesh.objects.count(), 0)
        self.maillage(self.p)
        self.storage.arrete = False
        self.assertEqual(self.alice.post(f'/api/projects/{self.p.id}/repair/', {}, format='json').status_code, 202)

    # ── RECONSTRUCTION (COLMAP remplacé) ─────────────────────────────────────
    def test_reconstruction_sans_trois_photos_finit_en_erreur_sans_lancer_colmap(self):
        Photo.objects.create(project=self.p, file=self.deposer(self.p, f'projects/{self.p.id}/photos/a.jpg', jpeg()))
        with mock.patch('api.tasks._run') as colmap:
            job = self.jouer(tasks.run_reconstruction, self.job(Job.RECONSTRUCTION))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('3 photos', job.message)
        colmap.assert_not_called()

    def test_reconstruction_lit_les_photos_dans_storage_et_s_arrete_a_la_premiere_etape_en_echec(self):
        for i in range(3):
            chemin = self.deposer(self.p, f'projects/{self.p.id}/photos/{i}.jpg', jpeg(2400, 100))
            Photo.objects.create(project=self.p, order=i, file=chemin)
        echec = tasks.ReconstructionError('colmap a échoué : pas de points')
        with mock.patch('api.tasks._run', side_effect=echec) as colmap:
            job = self.jouer(tasks.run_reconstruction, self.job(Job.RECONSTRUCTION, preset='rapide'))
        self.assertEqual((job.status, job.message), (Job.ERROR, 'colmap a échoué : pas de points'))
        self.assertEqual(colmap.call_count, 1)
        commande = [str(c) for c in colmap.call_args[0][0]]
        self.assertEqual(commande[:2], ['colmap', 'feature_extractor'])
        # Sans ce drapeau COLMAP tente un contexte OpenGL et s'arrête net (machine sans écran).
        self.assertEqual(commande[commande.index('--SiftExtraction.use_gpu') + 1], '0')
        images = sorted((self.travail(job) / 'images').iterdir())
        self.assertEqual([p.name for p in images], ['0.jpg', '1.jpg', '2.jpg'])
        self.assertEqual(Image.open(images[0]).size, (1200, 50))    # préréglage « rapide » : 1200 px au plus
        self.assertEqual(Mesh.objects.count(), 0)
        self.assertFalse(Photo.objects.filter(camera_pose__isnull=False).exists())

    # ── REPAIR (pymeshlab remplacé) ──────────────────────────────────────────
    def test_reparation_sans_maillage_finit_en_erreur(self):
        job = self.jouer(tasks.run_repair, self.job(Job.REPAIR))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('Aucun maillage', job.message)

    def test_reparation_travaille_sur_une_copie_locale_du_dernier_maillage(self):
        self.maillage(self.p, version=1, ply=b'ancien')
        self.maillage(self.p, version=2)
        avant = temporaires()
        vus = {}

        def reparer(entree, sortie, cible):
            vus.update(entree=entree, existait=entree.exists(), octets=entree.read_bytes(), cible=cible)
            sortie.write_bytes(entree.read_bytes())
            return {'method': 'repair', 'target_faces': cible,
                    'before': {'number_holes': 3, 'is_watertight': False},
                    'after': {'number_holes': 0, 'is_watertight': True}}

        with mock.patch('api.tasks.repair_module.repair_mesh', side_effect=reparer):
            job = self.jouer(tasks.run_repair, self.job(Job.REPAIR, target_faces=500))
        self.assertEqual(job.status, Job.DONE, job.message)
        self.assertEqual((vus['existait'], vus['octets'], vus['cible']), (True, boite_ply(), 500))
        self.assertEqual(vus['entree'].suffix, '.ply')
        self.assertFalse(vus['entree'].exists())        # la copie locale ne survit pas au bloc
        self.assertEqual(temporaires(), avant)
        mesh = Mesh.objects.get(job=job)
        self.assertEqual((mesh.version, mesh.is_watertight, mesh.face_count), (3, True, 12))
        self.assertEqual(mesh.file.name, f'projects/{self.p.id}/meshes/repaired_v3.ply')
        self.assertEqual(mesh.repair_report['before']['number_holes'], 3)
        self.assertIn('3 trou(s) → 0', job.message)
        self.assertEqual(Mesh.objects.filter(project=self.p).count(), 3)    # versionné, jamais écrasé

    def test_reparation_qui_echoue_nettoie_sa_copie_et_finit_en_erreur(self):
        self.maillage(self.p)
        avant = temporaires()
        with mock.patch('api.tasks.repair_module.repair_mesh', side_effect=RuntimeError('pymeshlab a planté')):
            job = self.jouer(tasks.run_repair, self.job(Job.REPAIR))
        self.assertEqual((job.status, job.message), (Job.ERROR, 'pymeshlab a planté'))
        self.assertEqual(temporaires(), avant)
        self.assertEqual(Mesh.objects.filter(project=self.p).count(), 1)

    # ── SEGMENTATION_FACADE ──────────────────────────────────────────────────
    def test_segmentation_preconditions_revérifiees_par_la_tache(self):
        job = self.jouer(tasks.run_facade_segmentation, self.job(Job.SEGMENTATION_FACADE))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('Aucun maillage', job.message)
        self.maillage(self.p)
        Photo.objects.create(project=self.p, file='x/a.jpg', camera_pose={'quaternion_wxyz': [1, 0, 0, 0]})
        job = self.jouer(tasks.run_facade_segmentation, self.job(Job.SEGMENTATION_FACADE))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('Aucune région labellisée', job.message)

    # ── CAD_BUILD (build123d réel sur une boîte) ─────────────────────────────
    def test_construction_cao_sans_operation_finit_en_erreur(self):
        job = self.jouer(tasks.run_cad_build, self.job(Job.CAD_BUILD))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('Aucune opération', job.message)

    def test_construction_cao_parametre_manquant_nomme_l_operation_fautive(self):
        CadOperation.objects.create(project=self.p, order=1, operation_type='PRIMITIVE_BOX',
                                    params={'length': 10, 'width': 20})
        job = self.jouer(tasks.run_cad_build, self.job(Job.CAD_BUILD))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('#1 (PRIMITIVE_BOX)', job.message)
        self.assertEqual(Mesh.objects.count(), 0)

    def test_construction_cao_d_une_boite_step_ply_gltf(self):
        CadOperation.objects.create(project=self.p, order=1, operation_type='PRIMITIVE_BOX',
                                    params={'length': 10, 'width': 20, 'height': 30})
        job = self.jouer(tasks.run_cad_build, self.job(Job.CAD_BUILD, linear_deflection=0.2))
        self.assertEqual(job.status, Job.DONE, job.message)
        mesh = Mesh.objects.get(job=job)
        base = f'projects/{self.p.id}/meshes/cad_v1'
        self.assertEqual((mesh.step_file.name, mesh.file.name, mesh.gltf_file.name),
                         (f'{base}.step', f'{base}.ply', f'{base}.glb'))
        self.assertEqual((mesh.vertex_count, mesh.face_count, mesh.is_watertight), (8, 12, True))
        self.assertEqual((mesh.linear_deflection, mesh.angular_deflection), (0.2, 0.3))
        self.assertTrue(self.storage.fichiers[(self.espace(self.p), f'{base}.step')].startswith(b'ISO-10303-21'))

    # ── CAD_ASSEMBLE (FreeCAD remplacé) ──────────────────────────────────────
    def test_assemblage_en_echec_marque_le_job_et_le_projet(self):
        asm, *_ = self.assemblage(ALICE, statut=Project.SOLVED)
        echec = cad_assemble.CadAssembleError('Échec du solveur FreeCAD (code 1) : surcontraint')
        with mock.patch('api.tasks.cad_assemble_module.resolve_assembly', side_effect=echec):
            job = self.jouer(tasks.run_cad_assemble, self.job(Job.CAD_ASSEMBLE, projet=asm))
        self.assertEqual(job.status, Job.ERROR)
        self.assertIn('surcontraint', job.message)      # le message du solveur n'est jamais avalé
        asm.refresh_from_db()
        self.assertEqual(asm.assembly_status, Project.ASSEMBLY_ERROR)
        # Il n'est donc plus publié (il l'était : SOLVED).
        self.assertEqual(self.client.get(f'/api/public/assemblies/{asm.id}/').status_code, 404)

    def test_assemblage_resolu_placements_enregistres_maillage_produit_projet_publie(self):
        asm, piece, mesh_piece, inst = self.assemblage(ALICE)
        self.storage.fichiers[(self.espace(asm), mesh_piece.step_file.name)] = boite_step()
        self.assertEqual(self.client.get(f'/api/public/assemblies/{asm.id}/').status_code, 404)
        avant = temporaires()

        def resoudre(projet, travail):
            (travail / 'assembly_result.step').write_bytes(boite_step())
            return {str(inst.id): {'position': [100.0, 0.0, 0.0], 'quaternion_xyzw': [0.0, 0.0, 0.0, 1.0]}}

        with mock.patch('api.tasks.cad_assemble_module.resolve_assembly', side_effect=resoudre):
            job = self.jouer(tasks.run_cad_assemble, self.job(Job.CAD_ASSEMBLE, projet=asm))
        self.assertEqual(job.status, Job.DONE, job.message)
        self.assertIn('1 instance(s) placée(s)', job.message)
        inst.refresh_from_db()
        asm.refresh_from_db()
        self.assertEqual(inst.placement['position'], [100.0, 0.0, 0.0])
        self.assertEqual(asm.assembly_status, Project.SOLVED)
        mesh = Mesh.objects.get(job=job)
        self.assertEqual((mesh.project_id, mesh.face_count, mesh.is_watertight), (asm.id, 12, True))
        self.assertTrue(mesh.step_file and mesh.gltf_file)
        self.assertEqual(temporaires(), avant)          # le STEP de chaque instance, relu en copie locale
        # Résolu : le voilà publié, avec son glTF, sans authentification.
        fiche = self.client.get(f'/api/public/assemblies/{asm.id}/').json()
        self.assertEqual(fiche['latest_mesh']['id'], mesh.id)
        glb = b''.join(self.client.get(f'/api/public/assemblies/{asm.id}/gltf/').streaming_content)
        self.assertTrue(glb.startswith(b'glTF'))


class PreparationAssemblageTests(Base):
    """cad_assemble : ce qui est envoyé à FreeCAD, et ce qui en revient."""

    def setUp(self):
        super().setUp()
        self.asm, self.piece, self.mesh, self.inst = self.assemblage(ALICE)
        self.travail = Path(self.scratch) / 'travail'
        self.travail.mkdir()

    def test_refus_avant_tout_calcul(self):
        with mock.patch('api.cad_assemble.subprocess.run') as freecad:
            CadAssemblyConstraint.objects.update(constraint_type='COINCIDENT')
            with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'FIXED'):
                cad_assemble.resolve_assembly(self.asm, self.travail)
            CadAssemblyConstraint.objects.all().delete()
            with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'rien à résoudre'):
                cad_assemble.resolve_assembly(self.asm, self.travail)
            CadAssemblyInstance.objects.all().delete()
            with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'Aucune CadAssemblyInstance'):
                cad_assemble.resolve_assembly(self.asm, self.travail)
        freecad.assert_not_called()

    def test_maillage_sans_step_refuse(self):
        photogrammetrie = self.maillage(self.piece, version=2)
        CadAssemblyInstance.objects.filter(pk=self.inst.pk).update(source_mesh=photogrammetrie)
        with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'sans STEP'):
            cad_assemble.resolve_assembly(self.asm, self.travail)

    def test_les_step_sont_rapatries_puis_freecad_recoit_des_fichiers_sans_extension(self):
        def freecad(commande, **_):
            entree, sortie = Path(commande[2]), Path(commande[3])
            charge = json.loads(entree.read_text())
            self.assertEqual(Path(charge['instances'][0]['step_path']).read_bytes(), b'STEP de la piece')
            self.assertEqual(charge['constraints'][0]['type'], 'FIXED')
            sortie.write_text(json.dumps({'placements': {str(self.inst.id): {'position': [1, 2, 3]}}}))
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('api.cad_assemble.subprocess.run', side_effect=freecad) as lance:
            placements = cad_assemble.resolve_assembly(self.asm, self.travail)
        self.assertEqual(placements, {str(self.inst.id): {'position': [1, 2, 3]}})
        commande = lance.call_args[0][0]
        self.assertEqual(commande[0], 'freecadcmd')
        # Piège documenté : freecadcmd ouvre comme un document tout argument en .json / .txt.
        self.assertEqual([Path(a).suffix for a in commande[2:]], ['', ''])

    def test_echec_du_solveur_rendu_tel_quel(self):
        rate = mock.Mock(returncode=1, stdout='', stderr='Solver failed: redundant constraints')
        with mock.patch('api.cad_assemble.subprocess.run', return_value=rate):
            with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'redundant constraints'):
                cad_assemble.resolve_assembly(self.asm, self.travail)

        def erreur_metier(commande, **_):
            Path(commande[3]).write_text(json.dumps({'error': 'Référence de face introuvable'}))
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('api.cad_assemble.subprocess.run', side_effect=erreur_metier):
            with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'Référence de face introuvable'):
                cad_assemble.resolve_assembly(self.asm, self.travail)

    def test_rayon_primitif_d_un_engrenage(self):
        with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'GEAR_TEETH'):
            cad_assemble._gear_pitch_radius(self.piece)
        denture = CadOperation.objects.create(project=self.piece, order=1, operation_type='GEAR_TEETH',
                                              params={'module': 2})
        with self.assertRaisesMessage(cad_assemble.CadAssembleError, 'incomplète'):
            cad_assemble._gear_pitch_radius(self.piece)
        denture.params = {'module': 2, 'teeth_number': 30}
        denture.save()
        self.assertEqual(cad_assemble._gear_pitch_radius(self.piece), 30.0)    # module × dents / 2


class CourseDuVerrouTests(LabMixin, TransactionTestCase):
    """Deux lancements simultanés, sur deux connexions réelles à la base."""

    def setUp(self):
        self.monter_le_lab()
        patcher = mock.patch('api.views.run_cad_build.delay')
        patcher.start()
        self.addCleanup(patcher.stop)

    # DÉFAUT CONNU, laissé tel quel par décision du propriétaire — ne pas le
    # « corriger » ici. Le verrou global est un `select_for_update()` sur les
    # jobs actifs : quand il n'y en a AUCUN, la requête ne rend aucune ligne,
    # donc ne verrouille rien, et deux lancements simultanés passent tous deux le
    # contrôle avant que l'un ait créé son job. `CELERY_WORKER_CONCURRENCY=1`
    # reste le filet : les deux jobs s'exécutent l'un après l'autre.
    # Le test est déterministe : une barrière retient chaque requête juste après
    # son contrôle, jusqu'à ce que l'autre l'ait passé aussi. Avec un vrai verrou,
    # la seconde attendrait la première, la barrière expirerait (2 s) et la
    # seconde recevrait 409.
    @unittest.expectedFailure
    def test_deux_lancements_simultanes_un_seul_job(self):
        projets = []
        for email in (ALICE, BOB):
            projet = self.projet(email)
            CadOperation.objects.create(project=projet, order=1, operation_type='PRIMITIVE_BOX')
            projets.append((email, projet))
        barriere = threading.Barrier(2, timeout=2)
        creer = Job.objects.create

        def creer_apres_la_barriere(**champs):
            try:
                barriere.wait()
            except threading.BrokenBarrierError:
                pass
            return creer(**champs)

        codes = []

        def lancer(email, projet):
            try:
                resp = self.client_de(email).post(f'/api/projects/{projet.id}/cad-build/', {}, format='json')
                codes.append(resp.status_code)
            finally:
                connections.close_all()

        with mock.patch.object(Job.objects, 'create', side_effect=creer_apres_la_barriere):
            fils = [threading.Thread(target=lancer, args=couple) for couple in projets]
            for fil in fils:
                fil.start()
            for fil in fils:
                fil.join(timeout=30)
        self.assertEqual(sorted(codes), [202, 409])
        self.assertEqual(Job.objects.filter(status__in=[Job.PENDING, Job.RUNNING]).count(), 1)
