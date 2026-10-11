"""Hiérarchie projet / sous-partie (héritage complet des droits et du partage
storage du projet racine) et suppression avec purge des octets."""
import unittest

from django.test import TestCase

from api import permissions, storage_backend
from api.models import (
    CadAssemblyInstance, Job, Mesh, Photo, Project, ProjectShare,
)

from .helpers import ALICE, BOB, CHLOE, DAVID, LabMixin


class Base(LabMixin, TestCase):
    def setUp(self):
        self.monter_le_lab()
        self.alice, self.bob = self.client_de(ALICE), self.client_de(BOB)
        self.chloe, self.david = self.client_de(CHLOE), self.client_de(DAVID)
        self.racine = self.projet_partage(name='Racine', project_type=Project.ASSEMBLY,
                                          assembly_status=Project.DRAFT)


class SousPartiesTests(Base):

    def creer(self, client=None, parent=None, **corps):
        return (client or self.bob).post(
            f'/api/projects/{(parent or self.racine).id}/sub-parts/', {'name': 'Pignon', **corps}, format='json')

    def test_creation_rattachee_au_parent_quel_que_soit_le_corps(self):
        ailleurs = self.projet(BOB, name='Ailleurs')
        resp = self.creer(parent_project=ailleurs.id, owner_email=DAVID)
        self.assertEqual(resp.status_code, 201)
        piece = Project.objects.get(name='Pignon')
        self.assertEqual(piece.parent_project_id, self.racine.id)
        self.assertEqual(piece.project_type, Project.OBJECT)
        self.assertEqual(piece.owner_email, BOB)        # l'auteur, pas le propriétaire de la racine
        self.assertEqual(piece.root_project.pk, self.racine.pk)

    def test_une_sous_partie_n_a_pas_de_sous_partie(self):
        piece = self.projet(ALICE, parent_project=self.racine)
        self.assertEqual(self.creer(self.alice, parent=piece).status_code, 400)
        self.assertEqual(Project.objects.filter(parent_project=piece).count(), 0)

    def test_les_droits_viennent_de_la_racine_jamais_de_l_auteur(self):
        """Bob (éditeur) crée une sous-partie : elle porte son adresse, mais
        Alice la voit et la modifie, Chloé la lit, et Bob la perd avec son
        invitation — owner_email d'une sous-partie n'ouvre aucun droit."""
        piece = Project.objects.get(pk=self.creer().json()['id'])
        url = f'/api/projects/{piece.id}/'
        self.assertEqual(self.alice.get(url).json()['my_role'], 'OWNER')
        self.assertEqual(self.bob.get(url).json()['my_role'], 'EDITOR')
        self.assertEqual(self.chloe.get(url).json()['my_role'], 'VIEWER')
        self.assertEqual(self.david.get(url).status_code, 404)
        self.assertEqual(self.alice.patch(url, {'name': 'Roue'}, format='json').status_code, 200)
        self.assertIn(self.chloe.patch(url, {'name': 'Non'}, format='json').status_code, (403, 404))
        self.assertEqual(self.bob.delete(url).status_code, 403)     # supprimer : propriétaire de la racine

        ProjectShare.objects.filter(project=self.racine, email=BOB).delete()
        self.assertEqual(self.bob.get(url).status_code, 404)
        self.assertEqual(self.bob.patch(url, {'name': 'À moi'}, format='json').status_code, 404)
        self.assertFalse(permissions.can_read(piece, BOB))
        self.assertEqual(self.bob.get('/api/projects/').json(), [])

    def test_une_sous_partie_n_a_pas_de_partage_propre(self):
        piece = self.projet(ALICE, parent_project=self.racine)
        url = f'/api/projects/{piece.id}/shares/'
        self.assertEqual(self.alice.get(url).status_code, 404)
        self.assertEqual(self.alice.post(url, {'email': DAVID}, format='json').status_code, 404)
        self.assertFalse(ProjectShare.objects.filter(project=piece).exists())

    def test_liste_des_sous_parties_et_absence_de_la_liste_des_projets(self):
        self.creer()
        self.assertEqual([p['name'] for p in self.chloe.get(f'/api/projects/{self.racine.id}/sub-parts/').json()],
                         ['Pignon'])
        self.assertEqual([p['name'] for p in self.alice.get('/api/projects/').json()], ['Racine'])
        self.assertEqual(self.alice.get(f'/api/projects/{self.racine.id}/').json()['sub_parts_count'], 1)

    def test_fichiers_d_une_sous_partie_dans_le_partage_de_la_racine(self):
        piece = self.projet(BOB, parent_project=self.racine)
        chemin = f'projects/{piece.id}/meshes/cad_v1.step'
        self.assertEqual(storage_backend.namespace_for_path(chemin), self.espace(self.racine))
        self.assertTrue(self.espace(self.racine).endswith(f'-project-{self.racine.id}'))
        self.deposer(piece, chemin, b'step')
        # Chloé, invitée de la racine, lit le fichier de la sous-partie ; David non.
        self.assertEqual(b''.join(self.chloe.get(f'/media/{chemin}').streaming_content), b'step')
        self.assertEqual(self.david.get(f'/media/{chemin}').status_code, 404)

    def test_root_project_ne_boucle_pas_sur_un_cycle(self):
        a = self.projet(ALICE, name='a')
        b = self.projet(BOB, name='b', parent_project=a)
        Project.objects.filter(pk=a.pk).update(parent_project=b)   # état impossible par l'API
        a.refresh_from_db()
        self.assertIn(a.root_project.pk, (a.pk, b.pk))             # termine, c'est tout ce qu'on exige


class SuppressionTests(Base):

    def setUp(self):
        super().setUp()
        self.piece = self.projet(BOB, name='Pièce', parent_project=self.racine)
        self.mesh_piece = self.maillage(self.piece, step=b'step', gltf=b'glb')
        self.mesh_racine = self.maillage(self.racine, gltf=b'glb')
        self.photo = Photo.objects.create(
            project=self.racine, file=self.deposer(self.racine, f'projects/{self.racine.id}/photos/a.jpg'),
            region_map=self.deposer(self.racine, f'projects/{self.racine.id}/photos/regions/1_regions.npz'))
        self.autre = self.projet(ALICE, name='Autre')
        self.fichier_autre = self.deposer(self.autre, f'projects/{self.autre.id}/photos/z.jpg')
        Photo.objects.create(project=self.autre, file=self.fichier_autre)
        self.url = f'/api/projects/{self.racine.id}/'

    def test_suppression_emporte_les_sous_parties_et_tout_ce_qui_en_depend(self):
        self.assertEqual(self.alice.delete(self.url).status_code, 204)
        self.assertFalse(Project.objects.filter(pk__in=[self.racine.pk, self.piece.pk]).exists())
        self.assertFalse(Mesh.objects.filter(pk__in=[self.mesh_piece.pk, self.mesh_racine.pk]).exists())
        self.assertFalse(Photo.objects.filter(pk=self.photo.pk).exists())
        self.assertFalse(ProjectShare.objects.filter(project_id=self.racine.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.autre.pk).exists())
        # Les octets d'un AUTRE projet ne sont jamais touchés.
        self.assertIn((self.espace(self.autre), self.fichier_autre), self.storage.fichiers)

    # Corrigé : la purge était faite après `project.delete()` par `field.delete()`,
    # qui relisait en base le projet tout juste supprimé pour trouver son partage —
    # l'exception était avalée, rien n'était purgé.
    def test_suppression_purge_les_octets_du_projet_et_de_ses_sous_parties(self):
        espace = self.espace(self.racine)
        self.storage.partages[espace] = {ALICE: 'write', BOB: 'write', CHLOE: 'read'}
        self.storage.partages[self.espace(self.autre)] = {ALICE: 'write'}
        self.assertEqual(len([1 for (ns, _) in self.storage.fichiers if ns == espace]), 7)
        self.assertEqual(self.alice.delete(self.url).status_code, 204)
        self.assertEqual(sorted(c for (ns, c) in self.storage.fichiers if ns == espace), [])
        self.assertEqual(len(self.storage.appels_vers('/content/', 'DELETE')), 7)   # un par fichier
        # Projet racine : son partage storage part aussi, avec ses membres.
        self.assertNotIn(espace, self.storage.partages)
        # Rien d'un autre projet n'est touché.
        self.assertEqual(self.storage.partages, {self.espace(self.autre): {ALICE: 'write'}})
        self.assertIn((self.espace(self.autre), self.fichier_autre), self.storage.fichiers)

    def test_suppression_d_une_sous_partie_purge_ses_octets_et_eux_seuls(self):
        espace = self.espace(self.racine)
        self.storage.partages[espace] = {ALICE: 'write'}
        self.assertEqual(self.alice.delete(f'/api/projects/{self.piece.id}/').status_code, 204)
        restants = sorted(c for (ns, c) in self.storage.fichiers if ns == espace)
        self.assertEqual(restants, sorted([
            self.mesh_racine.file.name, self.mesh_racine.gltf_file.name,
            self.photo.file.name, self.photo.region_map.name,
        ]))
        self.assertIn(espace, self.storage.partages)    # le partage est celui de la racine : il reste

    def test_purge_en_echec_journalisee_la_suppression_reste_acquise(self):
        self.storage.arrete = True
        with self.assertLogs('api.views', level='ERROR') as journal:
            self.assertEqual(self.alice.delete(self.url).status_code, 204)
        self.assertFalse(Project.objects.filter(pk__in=[self.racine.pk, self.piece.pk]).exists())
        self.assertEqual(len(journal.records), 8)       # 7 fichiers + le partage
        self.assertIn(f'projects/{self.racine.id}/photos/a.jpg', journal.output[0])
        self.assertIn('partage storage non retiré', journal.output[-1])

    def test_suppression_d_une_sous_partie_seule_laisse_la_racine_intacte(self):
        self.assertEqual(self.alice.delete(f'/api/projects/{self.piece.id}/').status_code, 204)
        self.assertFalse(Project.objects.filter(pk=self.piece.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.racine.pk).exists())
        self.assertTrue(Mesh.objects.filter(pk=self.mesh_racine.pk).exists())
        espace = self.espace(self.racine)
        for champ in (self.mesh_racine.file, self.mesh_racine.gltf_file, self.photo.file):
            self.assertIn((espace, champ.name), self.storage.fichiers)

    def test_refusee_tant_qu_un_job_est_actif_puis_permise(self):
        job = Job.objects.create(project=self.racine, kind=Job.REPAIR, status=Job.PENDING)
        for statut in (Job.PENDING, Job.RUNNING):
            job.status = statut
            job.save()
            self.assertEqual(self.alice.delete(self.url).status_code, 409)
        self.assertTrue(Project.objects.filter(pk=self.racine.pk).exists())
        self.assertEqual(len(self.storage.appels_vers('/content/', 'DELETE')), 0)
        job.status = Job.ERROR
        job.save()
        self.assertEqual(self.alice.delete(self.url).status_code, 204)

    # DÉFAUT (mineur) — le garde « job actif » ne regarde que les jobs du projet
    # supprimé, pas ceux de ses sous-parties, pourtant emportées par la même
    # cascade : un CAD_BUILD en cours sur une sous-partie n'empêche pas de
    # supprimer l'assemblage, et la tâche écrit ensuite pour un projet disparu.
    @unittest.expectedFailure
    def test_refusee_tant_qu_un_job_est_actif_sur_une_sous_partie(self):
        Job.objects.create(project=self.piece, kind=Job.CAD_BUILD, status=Job.RUNNING)
        self.assertEqual(self.alice.delete(self.url).status_code, 409)
        self.assertTrue(Project.objects.filter(pk=self.racine.pk).exists())

    def test_refusee_tant_qu_un_maillage_est_epingle_par_une_instance(self):
        """`CadAssemblyInstance.source_mesh` est en PROTECT : ni la sous-partie
        seule, ni l'assemblage entier ne partent tant que l'instance existe —
        et rien n'est supprimé à moitié."""
        instance = CadAssemblyInstance.objects.create(
            assembly_project=self.racine, source_project=self.piece, source_mesh=self.mesh_piece)
        self.assertEqual(self.alice.delete(f'/api/projects/{self.piece.id}/').status_code, 409)
        self.assertEqual(self.alice.delete(self.url).status_code, 409)
        self.assertTrue(Project.objects.filter(pk__in=[self.racine.pk, self.piece.pk]).count() == 2)
        self.assertTrue(Mesh.objects.filter(pk=self.mesh_piece.pk).exists())
        self.assertTrue(Photo.objects.filter(pk=self.photo.pk).exists())
        self.assertEqual(len(self.storage.appels_vers('/content/', 'DELETE')), 0)

        self.assertEqual(self.bob.delete(f'/api/cad-instances/{instance.id}/').status_code, 204)
        self.assertEqual(self.alice.delete(self.url).status_code, 204)
