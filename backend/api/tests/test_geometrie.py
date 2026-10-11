"""Fonctions pures : estimations, lecture des sorties COLMAP, géométrie
(numpy / trimesh sur une boîte). Rapides, sans base ni réseau."""
import io
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import trimesh
from django.test import SimpleTestCase

from api import facade, reconstruction, repair, tasks

from .helpers import boite_ply


class EstimationsTests(SimpleTestCase):

    def test_reconstruction_calee_sur_la_mesure_de_reference(self):
        # 18 photos de 2816 px : 1071 s mesurées (Lot 0). Même jeu, même résolution → même durée.
        avec_reference = {**reconstruction.PRESETS, 'reference': {'max_image_size': 2816}}
        with mock.patch.dict(reconstruction.PRESETS, avec_reference):
            self.assertAlmostEqual(reconstruction.estimate_duration_seconds(18, 'reference'), 1071)
            # L'appariement exhaustif croît en n², la densification en n.
            double = reconstruction.estimate_duration_seconds(36, 'reference')
            self.assertAlmostEqual(double, 4 * 395 + 2 * 662 + 14)

    def test_reconstruction_croissante_et_seuil_d_avertissement(self):
        durees = [reconstruction.estimate_duration_seconds(40, p) for p in ('rapide', 'equilibre', 'precis')]
        self.assertEqual(durees, sorted(durees))
        self.assertLess(reconstruction.estimate_duration_seconds(18, 'equilibre'),
                        reconstruction.DURATION_WARNING_THRESHOLD_S)
        self.assertGreater(reconstruction.estimate_duration_seconds(300, 'equilibre'),
                           reconstruction.DURATION_WARNING_THRESHOLD_S)
        self.assertGreater(reconstruction.estimate_duration_seconds(0, 'rapide'), 0)

    def test_cible_de_decimation_pour_un_poids_de_stl(self):
        faces = repair.estimate_target_faces_for_size_mb(5)
        self.assertAlmostEqual(84 + 50 * faces, 5 * 1024 * 1024, delta=50)
        self.assertEqual(repair.estimate_target_faces_for_size_mb(0.0001), repair.MIN_TARGET_FACES)

    def test_segmentation_lineaire_en_photos(self):
        self.assertEqual(facade.estimate_duration_seconds(10) - facade.estimate_duration_seconds(0),
                         10 * facade.FASTSAM_SECONDS_PER_PHOTO)


class SortiesColmapTests(SimpleTestCase):

    def setUp(self):
        self.dossier = Path(tempfile.mkdtemp(prefix='essai-colmap-'))
        self.addCleanup(__import__('shutil').rmtree, self.dossier, True)

    def test_images_txt_une_ligne_de_pose_sur_deux(self):
        (self.dossier / 'images.txt').write_text(
            '# Image list with two lines of data per image:\n'
            '1 0.9 0.1 0.2 0.3 1.5 -2.5 3.5 7 0.jpg\n'
            '100.0 200.0 -1 300.0 400.0 12\n'
            '3 1 0 0 0 4 5 6 7 2.jpg\n'
            '1.0 2.0 3\n')
        poses = tasks._parse_images_txt(self.dossier / 'images.txt')
        self.assertEqual(poses['0.jpg'], {'quaternion_wxyz': [0.9, 0.1, 0.2, 0.3],
                                          'translation': [1.5, -2.5, 3.5], 'camera_id': 7})
        self.assertEqual(sorted(poses), ['0.jpg', '2.jpg'])      # 1.jpg : non enregistrée par le SfM

    def test_cameras_txt(self):
        (self.dossier / 'cameras.txt').write_text(
            '# Camera list\n\n7 SIMPLE_RADIAL 1200 900 1000.5 600 450 0.01\n')
        self.assertEqual(tasks._parse_cameras_txt(self.dossier / 'cameras.txt'), {
            7: {'model': 'SIMPLE_RADIAL', 'width': 1200, 'height': 900, 'params': [1000.5, 600.0, 450.0, 0.01]},
        })

    def test_le_modele_qui_enregistre_le_plus_de_photos_est_retenu(self):
        """sparse/0 n'est pas forcément le plus complet (constaté : 6 images
        contre 10 dans sparse/1, la moitié des données perdue)."""
        for nom in ('0', '1', '2', 'journal'):
            (self.dossier / nom).mkdir()
        compte = {'0': 6, '1': 10, '2': 3}

        def analyser(commande, **_):
            self.assertEqual(commande[:2], ['colmap', 'model_analyzer'])
            return mock.Mock(stdout='', stderr=f'Registered images: {compte[Path(commande[-1]).name]}\n')

        with mock.patch('api.tasks.subprocess.run', side_effect=analyser):
            self.assertEqual(tasks._select_best_sparse_model(self.dossier).name, '1')

    def test_aucun_modele_erreur_lisible(self):
        with self.assertRaisesMessage(tasks.ReconstructionError, 'Aucune reconstruction exploitable'):
            tasks._select_best_sparse_model(self.dossier)

    def test_une_commande_en_echec_rend_la_fin_de_sa_sortie_d_erreur(self):
        rate = mock.Mock(returncode=1, stdout='', stderr='x' * 3000 + ' FIN')
        with mock.patch('api.tasks.subprocess.run', return_value=rate):
            with self.assertRaises(tasks.ReconstructionError) as contexte:
                tasks._run(['colmap', 'mapper'])
        message = str(contexte.exception)
        self.assertTrue(message.startswith('colmap a échoué'))
        self.assertTrue(message.endswith('FIN'))
        self.assertLess(len(message), 1600)


class GeometrieTests(SimpleTestCase):

    def test_quaternion_colmap_vers_matrice(self):
        rng = np.random.default_rng(7)
        for _ in range(5):
            q = rng.normal(size=4)
            q /= np.linalg.norm(q)
            attendu = trimesh.transformations.quaternion_matrix(q)[:3, :3]     # convention w, x, y, z
            np.testing.assert_allclose(facade._quat_to_matrix(*q), attendu, atol=1e-9)
        np.testing.assert_array_equal(facade._quat_to_matrix(0, 0, 0, 0), np.eye(3))

    def test_projection_simple_radial(self):
        intrinseques = {'model': 'SIMPLE_RADIAL', 'params': [1000.0, 600.0, 450.0, 0.1]}
        points = np.array([[0.0, 0.0, 2.0], [1.0, 0.0, 2.0]])
        pixels = facade._project(points, points[:, 2], intrinseques)
        np.testing.assert_allclose(pixels[0], [600, 450])                      # axe optique → centre
        np.testing.assert_allclose(pixels[1], [600 + 1000 * 0.5 * (1 + 0.1 * 0.25), 450])
        with self.assertRaises(facade.FacadeError):
            facade._project(points, points[:, 2], {'model': 'OPENCV', 'params': []})

    def test_clic_normalise_vers_region_avec_bords(self):
        regions = np.array([[0, 0, 1, 1], [2, 2, -1, -1]])
        self.assertEqual(facade.region_at(regions, 0.0, 0.0), 0)
        self.assertEqual(facade.region_at(regions, 0.99, 0.0), 1)
        self.assertEqual(facade.region_at(regions, 0.1, 0.99), 2)
        self.assertEqual(facade.region_at(regions, 1.0, 1.0), -1)             # bord droit/bas : pas de débordement
        self.assertEqual(facade.region_at(regions, -3.0, 7.0), 2)

    def test_export_d_impression_echelle_et_format(self):
        with tempfile.NamedTemporaryFile(suffix='.ply') as fichier:
            fichier.write(boite_ply())
            fichier.flush()
            chemin = Path(fichier.name)
            stl = repair.export_print_file(chemin, [0, 0, 0, 1], 0.002, 'stl')
            troismf = repair.export_print_file(chemin, [0, 0, 0, 1], 0.002, '3mf')
            with self.assertRaises(repair.RepairError):
                repair.export_print_file(chemin, [0, 0, 0, 1], 1, 'gcode')
        with self.assertRaises(repair.RepairError):
            repair.export_print_file(chemin, [0, 0, 0, 1], 1, 'stl')         # fichier disparu
        np.testing.assert_allclose(trimesh.load(io.BytesIO(stl), file_type='stl').extents, [2, 4, 6], atol=1e-5)
        self.assertEqual(troismf[:2], b'PK')                                    # un 3MF est une archive zip

    def test_orientation_d_impression_coherente_avec_le_surplomb_annonce(self):
        """Le quaternion rendu ([x, y, z, w], la convention que l'export attend)
        doit amener le maillage dans la pose dont le taux de surplomb est annoncé :
        on le recalcule ici, indépendamment, sur le maillage tourné."""
        cone = trimesh.creation.cone(radius=1.0, height=3.0, sections=24)
        with tempfile.NamedTemporaryFile(suffix='.ply') as fichier:
            fichier.write(cone.export(file_type='ply'))
            fichier.flush()
            suggestion = repair.suggest_print_orientation(Path(fichier.name))
            geom = trimesh.load(fichier.name, process=False)
        qx, qy, qz, qw = suggestion['quaternion']
        self.assertAlmostEqual(qx * qx + qy * qy + qz * qz + qw * qw, 1.0)
        geom.apply_transform(trimesh.transformations.quaternion_matrix([qw, qx, qy, qz]))
        vers_le_bas = geom.face_normals[:, 2] < -np.cos(np.radians(repair.OVERHANG_ANGLE_DEG))
        surplomb = geom.area_faces[vers_le_bas].sum() / geom.area_faces.sum()
        self.assertAlmostEqual(suggestion['overhang_ratio'], surplomb, places=6)
        # Posé sur sa base, le cône aurait toute sa base en surplomb : l'heuristique fait mieux.
        self.assertLess(surplomb, np.pi * 1.0 ** 2 / cone.area)
