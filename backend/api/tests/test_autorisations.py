"""I3.2 — Qui lit, modifie, supprime quoi : propriétaire, invité en édition,
invité en lecture, étranger ; routes publiques délibérées.

Choix de l'app (api/permissions.py) : un projet invisible répond 404 (ne pas
laisser deviner qu'il existe) ; un projet visible mais non modifiable répond
403 sur les ressources filles, 404 sur les routes qui résolvent le projet par
`editable_projects` — les deux sont un refus, ce que `REFUS_ECRITURE` accepte.
"""
import io
from unittest import mock

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import (
    CadAssemblyConstraint, CadAssemblyInstance, CadOperation, CadSketch, Job, Joint, Part,
    Photo, PhotoLabel, Project, ProjectShare, SemanticClass,
)

from .helpers import ALICE, BOB, CHLOE, DAVID, LabMixin, temporaires

REFUS_ECRITURE = (403, 404)


class Base(LabMixin, TestCase):
    def setUp(self):
        self.monter_le_lab()
        self.alice = self.client_de(ALICE)
        self.bob = self.client_de(BOB)
        self.chloe = self.client_de(CHLOE)
        self.david = self.client_de(DAVID)
        self.p = self.projet_partage(name='Moulin')


class ProjetsTests(Base):

    def test_liste_limitee_a_ses_projets_et_a_ceux_partages(self):
        autre = self.projet(DAVID, name='À David')
        self.projet(ALICE, name='Sous-partie', parent_project=self.p)
        noms = lambda c: sorted(x['name'] for x in c.get('/api/projects/').json())
        self.assertEqual(noms(self.alice), ['Moulin'])       # la sous-partie n'est pas un projet
        self.assertEqual(noms(self.bob), ['Moulin'])
        self.assertEqual(noms(self.chloe), ['Moulin'])
        self.assertEqual(noms(self.david), [autre.name])

    def test_role_annonce_a_chacun(self):
        for client, role in ((self.alice, 'OWNER'), (self.bob, 'EDITOR'), (self.chloe, 'VIEWER')):
            with self.subTest(role=role):
                resp = client.get(f'/api/projects/{self.p.id}/')
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()['my_role'], role)

    def test_etranger_ne_voit_pas_que_le_projet_existe(self):
        for verbe, chemin in (
            ('get', ''), ('patch', ''), ('delete', ''), ('get', 'shares/'), ('post', 'shares/'),
            ('post', 'photos/'), ('post', 'reconstruct/'), ('get', 'reconstruct/estimate/'),
            ('post', 'mesh-import/'), ('post', 'repair/'), ('post', 'facade/'),
            ('get', 'facade/estimate/'), ('get', 'cad-sketches/'), ('post', 'cad-sketches/'),
            ('get', 'cad-operations/'), ('post', 'cad-operations/'), ('post', 'cad-build/'),
            ('get', 'sub-parts/'), ('post', 'sub-parts/'), ('get', 'cad-instances/'),
            ('post', 'cad-instances/'), ('get', 'cad-constraints/'), ('post', 'cad-constraints/'),
            ('post', 'video/'),
        ):
            with self.subTest(verbe=verbe, chemin=chemin):
                resp = getattr(self.david, verbe)(f'/api/projects/{self.p.id}/{chemin}', {}, format='json')
                self.assertEqual(resp.status_code, 404)
        self.assertTrue(Project.objects.filter(pk=self.p.id).exists())
        self.assertEqual(Job.objects.count(), 0)

    def test_modification_proprietaire_et_editeur_seulement(self):
        url = f'/api/projects/{self.p.id}/'
        self.assertEqual(self.alice.patch(url, {'scale_meters_per_unit': 0.5}, format='json').status_code, 200)
        self.assertEqual(self.bob.patch(url, {'name': 'Moulin 2'}, format='json').status_code, 200)
        self.assertIn(self.chloe.patch(url, {'name': 'Piraté'}, format='json').status_code, REFUS_ECRITURE)
        self.p.refresh_from_db()
        self.assertEqual((self.p.name, self.p.scale_meters_per_unit), ('Moulin 2', 0.5))

    def test_proprietaire_et_statut_d_assemblage_ne_se_modifient_pas_par_l_api(self):
        resp = self.bob.patch(f'/api/projects/{self.p.id}/', {
            'owner_email': BOB, 'assembly_status': Project.SOLVED, 'parent_project': None,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.p.refresh_from_db()
        self.assertEqual(self.p.owner_email, ALICE)
        self.assertIsNone(self.p.assembly_status)

    def test_creation_le_createur_est_proprietaire_et_seul_membre_storage(self):
        resp = self.david.post('/api/projects/', {'name': 'Neuf', 'owner_email': ALICE}, format='json')
        self.assertEqual(resp.status_code, 201)
        neuf = Project.objects.get(name='Neuf')
        self.assertEqual(neuf.owner_email, DAVID)
        self.assertEqual(self.storage.partages[self.espace(neuf)], {DAVID: 'write'})

    def test_creation_reussit_meme_si_storage_est_arrete(self):
        self.storage.arrete = True
        self.assertEqual(self.alice.post('/api/projects/', {'name': 'Neuf'}, format='json').status_code, 201)

    def test_suppression_reservee_au_proprietaire(self):
        url = f'/api/projects/{self.p.id}/'
        self.assertEqual(self.bob.delete(url).status_code, 403)
        self.assertEqual(self.chloe.delete(url).status_code, 403)
        self.assertEqual(self.david.delete(url).status_code, 404)
        self.assertTrue(Project.objects.filter(pk=self.p.id).exists())
        self.assertEqual(self.alice.delete(url).status_code, 204)
        self.assertFalse(Project.objects.filter(pk=self.p.id).exists())


class PartagesTests(Base):

    def url(self):
        return f'/api/projects/{self.p.id}/shares/'

    def test_seul_le_proprietaire_lit_et_gere_les_partages(self):
        self.assertEqual(self.alice.get(self.url()).status_code, 200)
        for client in (self.bob, self.chloe, self.david):
            self.assertEqual(client.get(self.url()).status_code, 404)
            resp = client.post(self.url(), {'email': DAVID, 'role': 'EDITOR'}, format='json')
            self.assertEqual(resp.status_code, 404)
        self.assertFalse(ProjectShare.objects.filter(email=DAVID).exists())

    def test_un_editeur_ne_peut_ni_se_promouvoir_ni_retirer_un_partage(self):
        sien = ProjectShare.objects.get(project=self.p, email=BOB)
        celui_de_chloe = ProjectShare.objects.get(project=self.p, email=CHLOE)
        self.assertEqual(self.chloe.patch(f'/api/project-shares/{celui_de_chloe.id}/',
                                          {'role': 'EDITOR'}, format='json').status_code, 403)
        self.assertEqual(self.bob.delete(f'/api/project-shares/{celui_de_chloe.id}/').status_code, 403)
        self.assertEqual(self.david.delete(f'/api/project-shares/{sien.id}/').status_code, 404)
        celui_de_chloe.refresh_from_db()
        self.assertEqual(celui_de_chloe.role, 'VIEWER')
        self.assertEqual(ProjectShare.objects.filter(project=self.p).count(), 2)

    def test_inviter_ouvre_l_acces_et_le_replique_dans_storage(self):
        self.assertEqual(self.david.get(f'/api/projects/{self.p.id}/').status_code, 404)
        resp = self.alice.post(self.url(), {'email': '  David@SSOLAB.local ', 'role': 'VIEWER'}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['email'], DAVID)          # casse et espaces normalisés
        self.assertEqual(resp.json()['invited_by'], ALICE)
        self.assertEqual(self.david.get(f'/api/projects/{self.p.id}/').status_code, 200)
        self.assertEqual(self.storage.partages[self.espace(self.p)],
                         {ALICE: 'write', BOB: 'write', CHLOE: 'read', DAVID: 'read'})

    def test_reinviter_change_le_role_sans_doublon(self):
        resp = self.alice.post(self.url(), {'email': CHLOE, 'role': 'EDITOR'}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ProjectShare.objects.filter(project=self.p, email=CHLOE).count(), 1)
        self.assertEqual(self.storage.partages[self.espace(self.p)][CHLOE], 'write')
        self.assertEqual(self.chloe.patch(f'/api/projects/{self.p.id}/', {'name': 'Ok'}, format='json').status_code, 200)

    def test_on_ne_s_invite_pas_soi_meme_et_le_role_est_controle(self):
        self.assertEqual(self.alice.post(self.url(), {'email': ALICE}, format='json').status_code, 400)
        self.assertEqual(self.alice.post(self.url(), {'email': DAVID, 'role': 'OWNER'}, format='json').status_code, 400)
        self.assertEqual(self.alice.post(self.url(), {'email': 'pas-une-adresse'}, format='json').status_code, 400)
        partage = ProjectShare.objects.get(project=self.p, email=BOB)
        self.assertEqual(self.alice.patch(f'/api/project-shares/{partage.id}/', {'role': 'OWNER'},
                                          format='json').status_code, 400)

    def test_retirer_un_partage_ferme_l_acces_ici_et_dans_storage(self):
        self.alice.post(self.url(), {'email': DAVID, 'role': 'EDITOR'}, format='json')
        partage = ProjectShare.objects.get(project=self.p, email=DAVID)
        self.assertEqual(self.alice.delete(f'/api/project-shares/{partage.id}/').status_code, 204)
        self.assertEqual(self.david.get(f'/api/projects/{self.p.id}/').status_code, 404)
        self.assertNotIn(DAVID, self.storage.partages[self.espace(self.p)])

    def test_retrograder_un_editeur_se_replique_dans_storage(self):
        self.alice.post(self.url(), {'email': DAVID, 'role': 'EDITOR'}, format='json')
        partage = ProjectShare.objects.get(project=self.p, email=BOB)
        resp = self.alice.patch(f'/api/project-shares/{partage.id}/', {'role': 'VIEWER'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.storage.partages[self.espace(self.p)][BOB], 'read')
        self.assertIn(self.bob.patch(f'/api/projects/{self.p.id}/', {'name': 'Non'}, format='json').status_code,
                      REFUS_ECRITURE)

    def test_storage_arrete_aucun_changement_de_partage_n_est_garde(self):
        """Un partage « actif » ici mais inconnu de storage (ou l'inverse)
        désynchroniserait l'accès direct aux fichiers : tout ou rien."""
        bob = ProjectShare.objects.get(project=self.p, email=BOB)
        self.storage.arrete = True
        self.assertEqual(self.alice.post(self.url(), {'email': DAVID}, format='json').status_code, 502)
        self.assertEqual(self.alice.patch(f'/api/project-shares/{bob.id}/', {'role': 'VIEWER'},
                                          format='json').status_code, 502)
        self.assertEqual(self.alice.delete(f'/api/project-shares/{bob.id}/').status_code, 502)
        self.assertFalse(ProjectShare.objects.filter(email=DAVID).exists())
        bob.refresh_from_db()
        self.assertEqual(bob.role, 'EDITOR')


class RessourcesFillesTests(Base):
    """Maillage, parties, jointures, photos, labels, CAO : lecture pour tout
    membre, écriture pour propriétaire et éditeur, 404 pour l'étranger."""

    def setUp(self):
        super().setUp()
        self.mesh = self.maillage(self.p)
        self.part_a = Part.objects.create(mesh=self.mesh, name='A', face_ids=[0, 1])
        self.part_b = Part.objects.create(mesh=self.mesh, name='B', face_ids=[2, 3])
        self.photo = Photo.objects.create(project=self.p, file=self.deposer(
            self.p, f'projects/{self.p.id}/photos/a.jpg'))
        self.sketch = CadSketch.objects.create(project=self.p, name='S')
        self.op = CadOperation.objects.create(project=self.p, order=1, operation_type='PRIMITIVE_BOX')
        self.job = Job.objects.create(project=self.p, kind=Job.REPAIR, status=Job.DONE)
        SemanticClass.objects.create(mesh=self.mesh, name='Mur', face_ids=[0])

    def test_lectures_ouvertes_aux_membres_fermees_a_l_etranger(self):
        chemins = [
            f'/api/meshes/{self.mesh.id}/parts/', f'/api/meshes/{self.mesh.id}/joints/',
            f'/api/meshes/{self.mesh.id}/semantic-classes/', f'/api/photos/{self.photo.id}/labels/',
            f'/api/jobs/{self.job.id}/', f'/api/projects/{self.p.id}/cad-sketches/',
            f'/api/projects/{self.p.id}/cad-operations/', f'/api/projects/{self.p.id}/sub-parts/',
            f'/api/projects/{self.p.id}/reconstruct/estimate/', f'/api/projects/{self.p.id}/facade/estimate/',
        ]
        for chemin in chemins:
            with self.subTest(chemin=chemin):
                for membre in (self.alice, self.bob, self.chloe):
                    self.assertEqual(membre.get(chemin).status_code, 200)
                self.assertEqual(self.david.get(chemin).status_code, 404)

    def test_ecritures_refusees_au_lecteur_et_a_l_etranger(self):
        ecritures = [
            ('post', f'/api/meshes/{self.mesh.id}/parts/', {'name': 'X', 'face_ids': [4]}),
            ('post', f'/api/meshes/{self.mesh.id}/parts/suggest/', {}),
            ('patch', f'/api/parts/{self.part_a.id}/', {'name': 'Piraté'}),
            ('delete', f'/api/parts/{self.part_a.id}/', None),
            ('post', f'/api/meshes/{self.mesh.id}/joints/',
             {'parent_part': self.part_a.id, 'child_part': self.part_b.id, 'joint_type': 'fixed'}),
            ('get', f'/api/photos/{self.photo.id}/regions/', None),     # écrit un cache : traité en écriture
            ('post', f'/api/photos/{self.photo.id}/labels/', {'x': 0.5, 'y': 0.5, 'semantic_class': 'mur'}),
            ('delete', f'/api/projects/{self.p.id}/photos/{self.photo.id}/', None),
            ('post', f'/api/projects/{self.p.id}/cad-sketches/', {'name': 'X'}),
            ('patch', f'/api/cad-sketches/{self.sketch.id}/', {'name': 'Piraté'}),
            ('delete', f'/api/cad-sketches/{self.sketch.id}/', None),
            ('post', f'/api/projects/{self.p.id}/cad-operations/', {'operation_type': 'PRIMITIVE_BOX'}),
            ('patch', f'/api/cad-operations/{self.op.id}/', {'params': {'length': 1}}),
            ('delete', f'/api/cad-operations/{self.op.id}/', None),
            ('post', f'/api/projects/{self.p.id}/sub-parts/', {'name': 'X'}),
            ('post', f'/api/projects/{self.p.id}/cad-build/', {}),
            ('post', f'/api/projects/{self.p.id}/repair/', {}),
        ]
        for verbe, chemin, corps in ecritures:
            with self.subTest(verbe=verbe, chemin=chemin):
                self.assertIn(getattr(self.chloe, verbe)(chemin, corps, format='json').status_code, REFUS_ECRITURE)
                self.assertEqual(getattr(self.david, verbe)(chemin, corps, format='json').status_code, 404)
        self.assertEqual(Part.objects.count(), 2)
        self.assertEqual((Joint.objects.count(), Photo.objects.count(), PhotoLabel.objects.count()), (0, 1, 0))
        self.assertEqual((CadSketch.objects.count(), CadOperation.objects.count()), (1, 1))
        self.assertEqual(Job.objects.exclude(pk=self.job.pk).count(), 0)
        self.assertEqual(Project.objects.filter(parent_project=self.p).count(), 0)
        self.part_a.refresh_from_db()
        self.assertEqual(self.part_a.name, 'A')
        self.assertEqual(self.storage.appels, [])   # aucun refus n'a touché storage

    def test_l_editeur_ecrit(self):
        resp = self.bob.post(f'/api/projects/{self.p.id}/cad-sketches/',
                             {'name': 'Profil', 'plane': 'XZ', 'project': 999}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['project'], self.p.id)     # jamais celui du corps de requête
        resp = self.bob.post(f'/api/projects/{self.p.id}/cad-operations/',
                             {'operation_type': 'PRIMITIVE_SPHERE', 'params': {'radius': 2}}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['order'], 2)               # à la suite de l'historique
        self.assertEqual(self.bob.delete(f'/api/parts/{self.part_b.id}/').status_code, 204)

    def test_creation_de_partie_ajuste_une_primitive_sur_une_copie_locale_nettoyee(self):
        avant = temporaires()
        vus = []

        def ajuster(chemin, face_ids):
            vus.append((chemin, chemin.exists(), chemin.suffix, face_ids))
            return {'primitive_type': 'plane', 'primitive_params': {'normal': [0, 0, 1]}}

        with mock.patch('api.views.segmentation.fit_primitive_to_faces', side_effect=ajuster):
            resp = self.bob.post(f'/api/meshes/{self.mesh.id}/parts/',
                                 {'name': ' Capot ', 'face_ids': [4, 5]}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual((resp.json()['name'], resp.json()['primitive_type']), ('Capot', 'plane'))
        chemin, existait, suffixe, face_ids = vus[0]
        self.assertTrue(existait)
        self.assertEqual((suffixe, face_ids), ('.ply', [4, 5]))
        self.assertFalse(chemin.exists())
        self.assertEqual(temporaires(), avant)
        for corps in ({'face_ids': [1]}, {'name': 'X'}, {'name': 'X', 'face_ids': []},
                      {'name': 'X', 'face_ids': 'tout'}):
            self.assertEqual(self.bob.post(f'/api/meshes/{self.mesh.id}/parts/', corps, format='json').status_code, 400)

    def test_suggestions_de_parties_puis_retouche_a_la_main(self):
        propositions = [{'face_ids': [6, 7], 'primitive_type': 'plane', 'primitive_params': {'normal': [0, 0, 1]}}]
        with mock.patch('api.views.segmentation.suggest_parts', return_value=propositions):
            resp = self.bob.post(f'/api/meshes/{self.mesh.id}/parts/suggest/', {}, format='json')
        self.assertEqual(resp.status_code, 201)
        suggestion = resp.json()[0]
        self.assertEqual((suggestion['name'], suggestion['suggested']), ('Suggestion 3', True))
        with mock.patch('api.views.segmentation.fit_primitive_to_faces', return_value=None):
            resp = self.bob.patch(f'/api/parts/{suggestion["id"]}/', {'face_ids': [6]}, format='json')
        # Repeinte à la main : ce n'est plus une suggestion, et sa primitive est réévaluée.
        self.assertEqual((resp.json()['suggested'], resp.json()['primitive_type']), (False, ''))
        self.assertEqual(self.bob.patch(f'/api/parts/{suggestion["id"]}/', {'face_ids': []},
                                        format='json').status_code, 400)

    def test_axe_de_jointure_seulement_entre_parties_du_meme_maillage(self):
        ailleurs = Part.objects.create(mesh=self.maillage(self.projet(BOB)), name='Z', face_ids=[0])
        url = f'/api/parts/{self.part_a.id}/suggest-axis/'
        self.assertEqual(self.chloe.get(url, {'other': ailleurs.id}).status_code, 404)
        axe = {'type': 'revolute', 'origin': [0, 0, 0], 'direction': [0, 0, 1]}
        with mock.patch('api.views.segmentation.suggest_joint_axis', return_value=axe) as calcul:
            resp = self.chloe.get(url, {'other': self.part_b.id})      # lecture : un lecteur peut demander
        self.assertEqual(resp.json(), {'suggestion': axe})
        self.assertEqual(calcul.call_args[0][1:], ([0, 1], [2, 3]))
        with mock.patch('api.views.segmentation.suggest_joint_axis', return_value=None):
            self.assertEqual(self.chloe.get(url, {'other': self.part_b.id}).json(), {'suggestion': None})

    def test_jobs_listes_seulement_pour_les_projets_visibles(self):
        ailleurs = Job.objects.create(project=self.projet(DAVID), kind=Job.REPAIR, status=Job.DONE)
        ids = lambda c, q='': [j['id'] for j in c.get(f'/api/jobs/{q}').json()]
        self.assertEqual(ids(self.chloe), [self.job.id])
        self.assertEqual(ids(self.david), [ailleurs.id])
        self.assertEqual(ids(self.david, f'?project={self.p.id}'), [])
        self.assertEqual(self.alice.get(f'/api/jobs/{ailleurs.id}/').status_code, 404)
        self.assertEqual(self.alice.get('/api/jobs/999999/').status_code, 404)


class JointuresTests(Base):
    """Arbre cinématique : un seul parent par partie, pas de cycle, pas de
    jointure entre deux maillages."""

    def setUp(self):
        super().setUp()
        self.mesh = self.maillage(self.p)
        self.a, self.b, self.c = (Part.objects.create(mesh=self.mesh, name=n, face_ids=[i])
                                  for i, n in enumerate('ABC'))

    def lier(self, parent, enfant, type_='revolute', client=None):
        return (client or self.bob).post(f'/api/meshes/{self.mesh.id}/joints/', {
            'parent_part': parent.id, 'child_part': enfant.id, 'joint_type': type_,
        }, format='json')

    def test_regles_de_l_arbre(self):
        resp = self.lier(self.a, self.b)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual((resp.json()['axis_origin'], resp.json()['axis_direction']), ([0, 0, 0], [0, 0, 1]))
        self.assertEqual(self.lier(self.b, self.c).status_code, 201)
        self.assertEqual(self.lier(self.a, self.a).status_code, 400)           # sur elle-même
        self.assertEqual(self.lier(self.a, self.c).status_code, 400)           # C a déjà un parent
        self.assertEqual(self.lier(self.c, self.a).status_code, 400)           # A→B→C→A : cycle
        self.assertEqual(self.lier(self.c, self.a, type_='ressort').status_code, 400)
        self.assertEqual(Joint.objects.count(), 2)

    def test_type_inconnu_refuse_meme_sans_cycle(self):
        self.assertEqual(self.lier(self.a, self.b, type_='ressort').status_code, 400)
        self.assertEqual(Joint.objects.count(), 0)

    def test_pas_de_jointure_avec_la_partie_d_un_autre_maillage(self):
        ailleurs = Part.objects.create(mesh=self.maillage(self.projet(BOB)), name='Z', face_ids=[0])
        self.assertEqual(self.lier(self.a, ailleurs).status_code, 400)
        self.assertEqual(self.lier(ailleurs, self.a).status_code, 400)
        resp = self.bob.post(f'/api/meshes/{self.mesh.id}/joints/',
                             {'parent_part': 'abc', 'child_part': None, 'joint_type': 'fixed'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_modifier_et_supprimer_une_jointure(self):
        joint = Joint.objects.create(parent_part=self.a, child_part=self.b, joint_type='revolute')
        url = f'/api/joints/{joint.id}/'
        self.assertEqual(self.chloe.patch(url, {'limit_max': 90}, format='json').status_code, 403)
        self.assertEqual(self.bob.patch(url, {'joint_type': 'ressort'}, format='json').status_code, 400)
        self.assertEqual(self.bob.patch(url, {'limit_min': -10, 'limit_max': 90}, format='json').status_code, 200)
        joint.refresh_from_db()
        self.assertEqual((joint.limit_min, joint.limit_max, joint.joint_type), (-10, 90, 'revolute'))
        self.assertEqual(self.david.delete(url).status_code, 404)
        self.assertEqual(self.bob.delete(url).status_code, 204)


class LabelsTests(Base):
    """Labellisation d'une région par un clic (coordonnées normalisées)."""

    def setUp(self):
        super().setUp()
        self.photo = Photo.objects.create(
            project=self.p, file=f'projects/{self.p.id}/photos/a.jpg', camera_pose={'x': 1})
        self.url = f'/api/photos/{self.photo.id}/labels/'

    def poser_les_regions(self):
        regions = np.full((4, 8), -1, dtype=np.int32)   # 4 lignes × 8 colonnes
        regions[:, 4:] = 3                              # moitié droite : région 3
        tampon = io.BytesIO()
        np.savez_compressed(tampon, region_ids=regions)
        self.photo.region_map = self.deposer(
            self.p, f'projects/{self.p.id}/photos/regions/{self.photo.id}_regions.npz', tampon.getvalue())
        self.photo.save()

    def test_refuse_tant_que_les_regions_ne_sont_pas_calculees(self):
        resp = self.bob.post(self.url, {'x': 0.9, 'y': 0.5, 'semantic_class': 'mur'}, format='json')
        self.assertEqual(resp.status_code, 409)

    def test_clic_resolu_en_region_puis_relabellisation_sans_doublon(self):
        self.poser_les_regions()
        resp = self.bob.post(self.url, {'x': 0.9, 'y': 0.5, 'semantic_class': 'mur'}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['region_index'], 3)
        self.bob.post(self.url, {'x': 0.6, 'y': 0.1, 'semantic_class': 'fenetre'}, format='json')
        label = PhotoLabel.objects.get(photo=self.photo)
        self.assertEqual((label.region_index, label.semantic_class), (3, 'fenetre'))
        self.assertEqual(self.chloe.delete(f'/api/photo-labels/{label.id}/').status_code, 403)
        self.assertEqual(self.bob.delete(f'/api/photo-labels/{label.id}/').status_code, 204)

    def test_clic_sur_le_fond_ou_saisie_invalide_refuses(self):
        self.poser_les_regions()
        for corps in ({'x': 0.1, 'y': 0.5, 'semantic_class': 'mur'},        # fond (-1)
                      {'x': 0.9, 'y': 0.5, 'semantic_class': 'balcon'},     # hors vocabulaire
                      {'x': 'a', 'y': 0.5, 'semantic_class': 'mur'},
                      {'semantic_class': 'mur'}):
            with self.subTest(corps=corps):
                self.assertEqual(self.bob.post(self.url, corps, format='json').status_code, 400)
        self.assertEqual(PhotoLabel.objects.count(), 0)

    def test_regions_refusees_sans_pose_camera(self):
        self.photo.camera_pose = None
        self.photo.save()
        with mock.patch('api.views.facade.ensure_photo_regions') as calcul:
            resp = self.bob.get(f'/api/photos/{self.photo.id}/regions/')
        self.assertEqual(resp.status_code, 409)
        calcul.assert_not_called()


class FichiersTests(Base):
    """Dépôt de photos et lecture des fichiers par /media/ (repli authentifié)."""

    def test_depot_de_photos_chemin_logique_et_ordre(self):
        url = f'/api/projects/{self.p.id}/photos/'
        self.assertEqual(self.bob.post(url, {}, format='multipart').status_code, 400)
        resp = self.bob.post(url, {'files': [SimpleUploadedFile('vue 1.jpg', b'un'),
                                             SimpleUploadedFile('b.jpg', b'deux')]}, format='multipart')
        self.assertEqual(resp.status_code, 201)
        espace = self.espace(self.p)
        self.assertEqual(self.storage.fichiers[(espace, f'projects/{self.p.id}/photos/vue_1.jpg')], b'un')
        self.assertEqual(self.storage.fichiers[(espace, f'projects/{self.p.id}/photos/b.jpg')], b'deux')
        self.assertEqual([p['order'] for p in resp.json()], [0, 1])
        # L'URL rendue désigne storage directement, dans le partage du projet, sans jeton.
        self.assertEqual(resp.json()[1]['file'], f'/api/files/{espace}/content/projects/{self.p.id}/photos/b.jpg')
        suite = self.alice.post(url, {'files': [SimpleUploadedFile('c.jpg', b'trois')]}, format='multipart')
        self.assertEqual(suite.json()[0]['order'], 2)

    def test_depot_de_video_les_images_extraites_prennent_la_suite(self):
        url = f'/api/projects/{self.p.id}/video/'
        Photo.objects.create(project=self.p, order=0, file='x/0.jpg')
        self.assertEqual(self.bob.post(url, {}, format='multipart').status_code, 400)
        video = lambda: SimpleUploadedFile('film.mp4', b'octets de film')
        self.assertEqual(self.bob.post(url, {'file': video(), 'fps': 'vite'}, format='multipart').status_code, 400)
        vus = {}

        def extraire(chemin, projet, fps):
            vus.update(chemin=chemin, octets=chemin.read_bytes(), fps=fps)
            return [Photo.objects.create(project=projet, order=i, file=f'projects/{projet.id}/photos/frame_{i}.jpg')
                    for i in range(2)]

        with mock.patch('api.views.video_import.extract_frames_from_video', side_effect=extraire):
            resp = self.bob.post(url, {'file': video(), 'fps': '0.5'}, format='multipart')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual((vus['octets'], vus['fps']), (b'octets de film', 0.5))
        self.assertFalse(vus['chemin'].exists())                 # la vidéo déposée n'est pas gardée
        self.assertEqual([p['order'] for p in resp.json()], [1, 2])
        with mock.patch('api.views.video_import.extract_frames_from_video', return_value=[]):
            self.assertEqual(self.bob.post(url, {'file': video()}, format='multipart').status_code, 400)

    def test_suppression_d_une_photo_purge_ses_octets(self):
        chemin = self.deposer(self.p, f'projects/{self.p.id}/photos/a.jpg')
        photo = Photo.objects.create(project=self.p, file=chemin)
        autre = Photo.objects.create(project=self.projet(DAVID), file='projects/0/photos/x.jpg')
        url = f'/api/projects/{self.p.id}/photos/'
        self.assertEqual(self.bob.delete(f'{url}{autre.id}/').status_code, 404)   # photo d'un autre projet
        self.assertEqual(self.bob.delete(f'{url}{photo.id}/').status_code, 204)
        self.assertNotIn((self.espace(self.p), chemin), self.storage.fichiers)
        self.assertFalse(Photo.objects.filter(pk=photo.id).exists())
        self.assertTrue(Photo.objects.filter(pk=autre.id).exists())

    def test_suppression_d_une_photo_purge_aussi_sa_carte_de_regions_et_son_calque(self):
        base = f'projects/{self.p.id}/photos'
        photo = Photo.objects.create(
            project=self.p, file=self.deposer(self.p, f'{base}/a.jpg'),
            region_map=self.deposer(self.p, f'{base}/regions/1_regions.npz'),
            region_overlay=self.deposer(self.p, f'{base}/regions/1_overlay.png'), region_count=3)
        PhotoLabel.objects.create(photo=photo, semantic_class='mur', region_index=0)
        voisine = Photo.objects.create(
            project=self.p, file=self.deposer(self.p, f'{base}/b.jpg'),
            region_map=self.deposer(self.p, f'{base}/regions/2_regions.npz'))
        self.assertEqual(self.bob.delete(f'/api/projects/{self.p.id}/photos/{photo.id}/').status_code, 204)
        restants = sorted(c for (_, c) in self.storage.fichiers)
        self.assertEqual(restants, [f'{base}/b.jpg', f'{base}/regions/2_regions.npz'])   # la voisine, intacte
        self.assertTrue(Photo.objects.filter(pk=voisine.id).exists())
        self.assertEqual(PhotoLabel.objects.count(), 0)

    def test_suppression_d_une_photo_storage_arrete_ni_erreur_500_ni_silence(self):
        photo = Photo.objects.create(project=self.p, file=self.deposer(self.p, f'projects/{self.p.id}/photos/a.jpg'))
        self.storage.arrete = True
        with self.assertLogs('api.views', level='ERROR') as journal:
            resp = self.bob.delete(f'/api/projects/{self.p.id}/photos/{photo.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Photo.objects.filter(pk=photo.id).exists())
        self.assertIn(f'projects/{self.p.id}/photos/a.jpg', journal.output[0])

    def test_media_sert_les_membres_et_cache_le_projet_a_l_etranger(self):
        chemin = self.deposer(self.p, f'projects/{self.p.id}/meshes/m.ply', b'maillage')
        for membre in (self.alice, self.bob, self.chloe):
            resp = membre.get(f'/media/{chemin}')
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(b''.join(resp.streaming_content), b'maillage')
        self.storage.appels.clear()
        self.assertEqual(self.david.get(f'/media/{chemin}').status_code, 404)
        self.assertEqual(self.storage.appels, [])       # refusé AVANT tout appel du compte de service
        self.assertEqual(APIClient().get(f'/media/{chemin}').status_code, 403)

    def test_media_chemins_hors_projet_absents_ou_storage_arrete(self):
        self.assertEqual(self.alice.get('/media/secrets/passwd').status_code, 404)
        self.assertEqual(self.alice.get('/media/projects/abc/x').status_code, 404)
        self.assertEqual(self.alice.get('/media/projects/999999/x').status_code, 404)
        self.assertEqual(self.alice.get(f'/media/projects/{self.p.id}/photos/absent.jpg').status_code, 404)
        self.storage.arrete = True
        self.assertEqual(self.alice.get(f'/media/projects/{self.p.id}/photos/a.jpg').status_code, 502)

    def test_media_ne_sort_pas_du_partage_du_projet(self):
        """Le chemin est rendu tel quel à storage, dans le partage du projet du
        préfixe : `..` ne peut pas désigner le partage d'un autre projet."""
        secret = self.projet(DAVID)
        self.deposer(secret, f'projects/{secret.id}/meshes/m.ply', b'secret')
        resp = self.alice.get(f'/media/projects/{self.p.id}/../{secret.id}/meshes/m.ply')
        self.assertEqual(resp.status_code, 404)
        for _, url in self.storage.appels_vers('/content/'):
            self.assertIn(f'/api/files/{self.espace(self.p)}/', url)


class ExportTests(Base):
    """Export d'impression : échelle obligatoire, orientation, unité = mm."""

    def setUp(self):
        super().setUp()
        self.mesh = self.maillage(self.p)       # boîte 1 × 2 × 3
        self.url = f'/api/meshes/{self.mesh.id}/export/'

    def charger(self, resp):
        import trimesh
        return trimesh.load(io.BytesIO(resp.content), file_type='stl')

    def test_refuse_sans_echelle_calibree(self):
        self.assertEqual(self.alice.get(self.url).status_code, 409)
        self.assertEqual(self.storage.appels, [])

    def test_stl_a_l_echelle_reelle_en_millimetres(self):
        self.p.scale_meters_per_unit = 0.01     # 1 unité = 1 cm = 10 mm
        self.p.name = 'Mou"lin\tà vent'
        self.p.save()
        avant = temporaires()
        resp = self.chloe.get(self.url)         # un lecteur peut exporter
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.content), 84 + 50 * 12)       # STL binaire : en-tête + 50 o / face
        np.testing.assert_allclose(self.charger(resp).extents, [10, 20, 30], atol=1e-4)
        disposition = resp['Content-Disposition']
        self.assertTrue(disposition.endswith(f'_v{self.mesh.version}.stl"'))
        self.assertEqual(disposition.count('"'), 2)             # le nom du projet ne casse pas l'en-tête
        self.assertEqual(temporaires(), avant)

    def test_orientation_par_quaternion_x_y_z_w(self):
        self.p.scale_meters_per_unit = 0.001
        self.p.save()
        r = 2 ** -0.5                           # quart de tour autour de X : Y et Z s'échangent
        resp = self.alice.get(self.url, {'qx': r, 'qw': r})
        np.testing.assert_allclose(self.charger(resp).extents, [1, 3, 2], atol=1e-4)

    def test_format_et_quaternion_controles(self):
        self.p.scale_meters_per_unit = 1
        self.p.save()
        self.assertEqual(self.alice.get(self.url, {'file_format': 'obj'}).status_code, 400)
        self.assertEqual(self.alice.get(self.url, {'qx': 'abc'}).status_code, 400)
        self.assertEqual(self.david.get(self.url).status_code, 404)

    def test_maillage_dont_le_fichier_a_disparu(self):
        self.p.scale_meters_per_unit = 1
        self.p.save()
        self.storage.fichiers.clear()
        avant = temporaires()
        self.assertEqual(self.alice.get(self.url).status_code, 404)
        self.assertEqual(self.alice.get(f'/api/meshes/{self.mesh.id}/auto-orient/').status_code, 404)
        self.assertEqual(temporaires(), avant)


class PubliqueTests(Base):
    """Les trois routes sans authentification (exception actée, Lot 5.3) : ce
    qu'elles laissent passer — un assemblage RÉSOLU de premier niveau, son glTF
    et son STEP — et tout ce qu'elles refusent."""

    def setUp(self):
        super().setUp()
        self.asm, self.piece, _, _ = self.assemblage(ALICE, statut=Project.SOLVED)
        self.maillage(self.asm, version=1, step=b'vieux step', gltf=b'vieux glb')
        self.maillage(self.asm, version=2, step=b'STEP v2', gltf=b'GLB v2')
        self.anonyme = APIClient()

    def test_liste_seulement_les_assemblages_resolus_de_premier_niveau(self):
        self.assemblage(ALICE, statut=Project.DRAFT)
        self.assemblage(ALICE, statut=Project.ASSEMBLY_ERROR)
        self.assemblage(ALICE, statut=None)
        self.projet(ALICE, name='Objet', assembly_status=Project.SOLVED)       # pas un assemblage
        self.projet(ALICE, name='Sous-assemblage', project_type=Project.ASSEMBLY,
                    assembly_status=Project.SOLVED, parent_project=self.asm)
        resp = self.anonyme.get('/api/public/assemblies/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([a['id'] for a in resp.json()], [self.asm.id])

    def test_aucune_donnee_de_cloisonnement_n_est_publiee(self):
        fiche = self.anonyme.get(f'/api/public/assemblies/{self.asm.id}/').json()
        self.assertEqual(set(fiche), {'id', 'name', 'description', 'assembly_status', 'latest_mesh',
                                      'created_at', 'updated_at'})
        self.assertEqual(fiche['latest_mesh']['version'], 2)
        self.assertEqual(fiche['latest_mesh']['gltf_url'], f'/api/public/assemblies/{self.asm.id}/gltf/')
        self.assertNotIn('ssolab.local', str(fiche))            # ni propriétaire ni invités
        self.assertNotIn('/api/files/', str(fiche))             # ni chemin storage

    def test_ce_qui_n_est_pas_resolu_n_existe_pas_pour_un_anonyme(self):
        brouillon, piece, _, _ = self.assemblage(ALICE, statut=Project.DRAFT)
        self.maillage(brouillon, gltf=b'glb', step=b'step')
        for projet in (brouillon, self.piece, self.p):
            for suffixe in ('', 'gltf/', 'step/'):
                with self.subTest(projet=projet.name, suffixe=suffixe):
                    resp = self.anonyme.get(f'/api/public/assemblies/{projet.id}/{suffixe}')
                    self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.storage.appels_vers('/content/'), [])

    def test_fichiers_publies_le_gltf_et_le_step_les_plus_recents_rien_d_autre(self):
        for fmt, attendu in (('gltf', b'GLB v2'), ('step', b'STEP v2')):
            resp = self.anonyme.get(f'/api/public/assemblies/{self.asm.id}/{fmt}/')
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(b''.join(resp.streaming_content), attendu)
        for fmt in ('ply', 'file', 'photos', '..'):
            self.assertEqual(self.anonyme.get(f'/api/public/assemblies/{self.asm.id}/{fmt}/').status_code, 404)

    def test_lecture_seule(self):
        for verbe in ('post', 'patch', 'put', 'delete'):
            for chemin in ('', f'{self.asm.id}/', f'{self.asm.id}/gltf/'):
                with self.subTest(verbe=verbe, chemin=chemin):
                    resp = getattr(self.anonyme, verbe)(f'/api/public/assemblies/{chemin}', {}, format='json')
                    self.assertEqual(resp.status_code, 405)
        self.assertTrue(Project.objects.filter(pk=self.asm.id).exists())

    def test_fichier_absent_ou_storage_arrete(self):
        self.storage.fichiers.clear()
        self.assertEqual(self.anonyme.get(f'/api/public/assemblies/{self.asm.id}/gltf/').status_code, 404)
        self.storage.arrete = True
        self.assertEqual(self.anonyme.get(f'/api/public/assemblies/{self.asm.id}/gltf/').status_code, 502)

    def test_debit_limite_a_soixante_par_minute(self):
        codes = [self.anonyme.get('/api/public/assemblies/').status_code for _ in range(61)]
        self.assertEqual(codes[:60], [200] * 60)
        self.assertEqual(codes[60], 429)


class AnnuaireTests(Base):
    """Les routes d'annuaire héritées du gabarit (`/api/users/`, `/api/departments/`)
    rendaient à tout membre l'adresse de quiconque s'était connecté : elles
    n'existent plus (retirées de tout le lab le 2026-10-12)."""

    def test_l_annuaire_du_gabarit_n_existe_plus(self):
        self.alice.get('/api/me/')
        for chemin in ('/api/users/', '/api/departments/'):
            self.assertEqual(self.david.get(chemin).status_code, 404)


class ReferencesCroiseesTests(Base):
    """Défauts trouvés (I4) : l'API accepte, dans un assemblage, des clés
    étrangères vers les objets d'un projet que l'appelant ne voit pas."""

    def setUp(self):
        super().setUp()
        self.asm_david, self.piece_david, _, self.inst_david = self.assemblage(DAVID)
        self.asm_alice, _, self.mesh_alice, self.inst_alice = self.assemblage(ALICE)

    # Corrigé (fix(sécurité)) : un `source_mesh` fourni n'était pas contrôlé — David
    # épinglait le maillage d'Alice dans SON assemblage, et CAD_ASSEMBLE en aurait
    # lu le STEP par le compte de service.
    def test_instance_ne_peut_pas_epingler_le_maillage_d_un_projet_etranger(self):
        resp = self.david.post(f'/api/projects/{self.asm_david.id}/cad-instances/', {
            'source_project': self.piece_david.id, 'source_mesh': self.mesh_alice.id,
        }, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(CadAssemblyInstance.objects.filter(
            assembly_project=self.asm_david, source_mesh=self.mesh_alice).exists())
        # Ni le maillage d'une AUTRE sous-partie du même assemblage (incohérent avec
        # le contrôle de version), ni un id qui n'existe pas ou n'est pas un nombre.
        autre_piece = self.projet(DAVID, name='Autre pièce', parent_project=self.asm_david)
        autre_mesh = self.maillage(autre_piece, step=b'step')
        for mesh_id in (autre_mesh.id, 999999, 'abc'):
            resp = self.david.post(f'/api/projects/{self.asm_david.id}/cad-instances/', {
                'source_project': self.piece_david.id, 'source_mesh': mesh_id,
            }, format='json')
            self.assertEqual(resp.status_code, 404)
        self.assertEqual(CadAssemblyInstance.objects.filter(assembly_project=self.asm_david).count(), 1)

    def test_instance_accepte_un_maillage_precis_de_la_piece(self):
        ancien = self.inst_david.source_mesh
        self.maillage(self.piece_david, version=2, step=b'step v2')
        resp = self.david.post(f'/api/projects/{self.asm_david.id}/cad-instances/', {
            'source_project': self.piece_david.id, 'source_mesh': ancien.id,
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['source_mesh'], ancien.id)

    # Corrigé : une contrainte acceptait les instances d'un autre assemblage.
    def test_contrainte_ne_peut_pas_viser_l_instance_d_un_autre_assemblage(self):
        resp = self.david.post(f'/api/projects/{self.asm_david.id}/cad-constraints/', {
            'constraint_type': 'COINCIDENT', 'instance_a': self.inst_david.id,
            'reference_a': {'kind': 'face', 'index': 0},
            'instance_b': self.inst_alice.id, 'reference_b': {'kind': 'face', 'index': 0},
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        resp = self.david.post(f'/api/projects/{self.asm_david.id}/cad-constraints/', {
            'constraint_type': 'FIXED', 'instance_a': self.inst_alice.id,
            'reference_a': {'kind': 'face', 'index': 0},
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(CadAssemblyConstraint.objects.filter(assembly_project=self.asm_david).count(), 1)

    def test_contrainte_entre_instances_de_son_assemblage_acceptee(self):
        bis = CadAssemblyInstance.objects.create(
            assembly_project=self.asm_david, source_project=self.piece_david,
            source_mesh=self.inst_david.source_mesh)
        resp = self.david.post(f'/api/projects/{self.asm_david.id}/cad-constraints/', {
            'constraint_type': 'COINCIDENT', 'instance_a': self.inst_david.id,
            'reference_a': {'kind': 'face', 'index': 0},
            'instance_b': bis.id, 'reference_b': {'kind': 'face', 'index': 1},
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['assembly_project'], self.asm_david.id)

    def test_instance_la_piece_doit_etre_une_sous_partie_de_cet_assemblage(self):
        piece_alice = self.inst_alice.source_project
        resp = self.david.post(f'/api/projects/{self.asm_david.id}/cad-instances/',
                               {'source_project': piece_alice.id}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_repointage_seulement_vers_un_maillage_de_la_meme_piece(self):
        resp = self.david.patch(f'/api/cad-instances/{self.inst_david.id}/',
                                {'source_mesh': self.mesh_alice.id}, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.david.patch(f'/api/cad-instances/{self.inst_alice.id}/',
                                          {'source_mesh': self.mesh_alice.id}, format='json').status_code, 404)
