"""Backend de fichiers `LabStorage` et client `storage_client`, contre un
storage simulé au niveau HTTP : chemins logiques, partage par projet racine,
copies locales nettoyées, compte de service, réplication des partages."""
import io
from pathlib import Path

import numpy as np
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import TestCase, override_settings

from api import storage_backend, storage_client
from api.models import Mesh, Photo, ProjectShare
from api.storage_backend import LabStorage

from .helpers import ALICE, BOB, CHLOE, JETON_DE_SERVICE, LabMixin, boite_ply, temporaires


class Base(LabMixin, TestCase):
    def setUp(self):
        self.monter_le_lab()
        self.p = self.projet(ALICE)
        self.ns = self.espace(self.p)
        self.depot = storages['default']


class LabStorageTests(Base):

    def test_c_est_bien_le_backend_de_fichiers_par_defaut(self):
        self.assertIsInstance(self.depot, LabStorage)
        self.assertIsInstance(Photo._meta.get_field('file').storage, LabStorage)
        self.assertIsInstance(Mesh._meta.get_field('step_file').storage, LabStorage)

    def test_un_champ_fichier_ecrit_a_son_chemin_logique_dans_le_partage_du_projet(self):
        photo = Photo(project=self.p)
        photo.file.save('vue.jpg', ContentFile(b'jpeg'), save=True)
        mesh = Mesh(project=self.p, version=1)
        mesh.file.save('reconstruction_v1.ply', ContentFile(b'ply'), save=False)
        mesh.step_file.save('cad_v1.step', ContentFile(b'step'), save=False)
        mesh.save()
        self.assertEqual(photo.file.name, f'projects/{self.p.id}/photos/vue.jpg')
        self.assertEqual(self.storage.fichiers, {
            (self.ns, f'projects/{self.p.id}/photos/vue.jpg'): b'jpeg',
            (self.ns, f'projects/{self.p.id}/meshes/reconstruction_v1.ply'): b'ply',
            (self.ns, f'projects/{self.p.id}/meshes/cad_v1.step'): b'step',
        })
        # Relu depuis la base, le champ rend les mêmes octets.
        self.assertEqual(Photo.objects.get(pk=photo.pk).file.read(), b'jpeg')

    def test_path_n_existe_pas_pour_un_stockage_distant(self):
        photo = Photo.objects.create(project=self.p, file=self.deposer(self.p, f'projects/{self.p.id}/photos/a.jpg'))
        with self.assertRaises(NotImplementedError):
            photo.file.path
        with self.assertRaises(NotImplementedError):
            self.depot.path(photo.file.name)

    def test_url_designe_storage_dans_le_partage_du_projet_sans_jeton(self):
        nom = f'projects/{self.p.id}/meshes/m.glb'
        self.assertEqual(self.depot.url(nom), f'/api/files/{self.ns}/content/{nom}')
        self.assertNotIn('token', self.depot.url(nom))

    def test_meme_nom_ecrase_sans_suffixe_aleatoire(self):
        nom = f'projects/{self.p.id}/meshes/reconstruction_v1.ply'
        self.assertEqual(self.depot.save(nom, ContentFile(b'un')), nom)
        self.assertEqual(self.depot.save(nom, ContentFile(b'deux')), nom)
        self.assertEqual(self.storage.fichiers, {(self.ns, nom): b'deux'})

    def test_un_fichier_deja_lu_est_envoye_en_entier(self):
        contenu = io.BytesIO(b'0123456789')
        contenu.read(6)                                 # Django lit l'en-tête d'une image avant de l'enregistrer
        nom = self.depot.save(f'projects/{self.p.id}/photos/a.jpg', ContentFile(b''))
        self.depot._save(nom, contenu)
        self.assertEqual(self.storage.fichiers[(self.ns, nom)], b'0123456789')

    def test_lecture_ecriture_refusee_existence_taille_date_suppression(self):
        nom = self.deposer(self.p, f'projects/{self.p.id}/meshes/m.ply', b'abcdef')
        absent = f'projects/{self.p.id}/meshes/absent.ply'
        self.assertEqual(self.depot.open(nom).read(), b'abcdef')
        with self.assertRaises(ValueError):
            self.depot.open(nom, 'wb')
        with self.assertRaises(FileNotFoundError):
            self.depot.open(absent)
        self.assertTrue(self.depot.exists(nom))
        self.assertFalse(self.depot.exists(absent))
        self.assertFalse(self.depot.exists(nom[:-1]))   # un préfixe n'est pas le fichier
        self.assertEqual(self.depot.size(nom), 6)
        with self.assertRaises(FileNotFoundError):
            self.depot.size(absent)
        self.assertEqual(self.depot.get_modified_time(nom).year, 2026)
        self.depot.delete(nom)
        self.depot.delete(nom)                          # déjà parti : pas une erreur
        self.depot.delete('')
        self.assertEqual(self.storage.fichiers, {})

    def test_listdir(self):
        for nom in ('photos/a.jpg', 'photos/b.jpg', 'photos/regions/1.npz', 'meshes/m.ply'):
            self.deposer(self.p, f'projects/{self.p.id}/{nom}')
        self.assertEqual(self.depot.listdir(f'projects/{self.p.id}/photos'), (['regions'], ['a.jpg', 'b.jpg']))
        self.assertEqual(self.depot.listdir(f'projects/{self.p.id}'), (['meshes', 'photos'], []))

    def test_un_tableau_numpy_se_lit_sans_fichier_temporaire(self):
        tampon = io.BytesIO()
        np.savez_compressed(tampon, region_ids=np.arange(6).reshape(2, 3))
        nom = self.deposer(self.p, f'projects/{self.p.id}/photos/regions/1_regions.npz', tampon.getvalue())
        avant = temporaires()
        with self.depot.open(nom) as fh:
            self.assertEqual(np.load(fh)['region_ids'].tolist(), [[0, 1, 2], [3, 4, 5]])
        self.assertEqual(temporaires(), avant)

    def test_chemin_hors_convention_ou_projet_inconnu(self):
        self.assertEqual(storage_backend.project_id_from_path('projects/42/photos/a.jpg'), 42)
        for nom in ('photos/a.jpg', '/projects/1/a', 'projects//a', 'projects/1', 'projets/1/a', '../projects/1/a'):
            with self.subTest(nom=nom):
                self.assertIsNone(storage_backend.project_id_from_path(nom))
        with self.assertRaises(ValueError):
            storage_backend.namespace_for_path('photos/a.jpg')
        with self.assertRaises(ValueError):
            self.depot.save('ailleurs/a.jpg', ContentFile(b'x'))
        self.assertEqual(self.storage.fichiers, {})

    def test_les_appels_portent_le_jeton_du_compte_de_service(self):
        # FauxStorage répond 401 à tout appel sans ce jeton : un dépôt réussi le prouve.
        self.depot.save(f'projects/{self.p.id}/photos/a.jpg', ContentFile(b'x'))
        self.assertEqual(self.storage.demandes_de_jeton, 1)
        self.assertEqual(storage_client._headers(), {'Authorization': f'Bearer {JETON_DE_SERVICE}'})


class CopieLocaleTests(Base):
    """`local_copy` : un vrai fichier le temps d'un bloc, pour un outil natif."""

    def setUp(self):
        super().setUp()
        self.mesh = self.maillage(self.p)

    def test_copie_fidele_extension_gardee_fichier_supprime_en_sortie(self):
        avant = temporaires()
        with storage_backend.local_copy(self.mesh.file) as chemin:
            self.assertIsInstance(chemin, Path)
            self.assertEqual(chemin.read_bytes(), boite_ply())
            self.assertEqual(chemin.suffix, '.ply')     # trimesh choisit son lecteur d'après l'extension
            self.assertEqual(temporaires() - avant, {chemin})
        self.assertFalse(chemin.exists())
        self.assertEqual(temporaires(), avant)

    def test_suffixe_impose(self):
        with storage_backend.local_copy(self.mesh.file, suffix='.stl') as chemin:
            self.assertEqual(chemin.suffix, '.stl')

    def test_nettoyee_meme_si_le_bloc_echoue(self):
        avant = temporaires()
        with self.assertRaises(RuntimeError):
            with storage_backend.local_copy(self.mesh.file) as chemin:
                raise RuntimeError('COLMAP a planté')
        self.assertFalse(chemin.exists())
        self.assertEqual(temporaires(), avant)

    def test_nettoyee_meme_si_le_telechargement_echoue(self):
        avant = temporaires()
        self.storage.fichiers.clear()
        with self.assertRaises(FileNotFoundError):
            with storage_backend.local_copy(self.mesh.file):
                self.fail('le bloc ne doit pas s’exécuter')
        self.storage.arrete = True
        with self.assertRaises(storage_client.StorageClientError):
            with storage_backend.local_copy(self.mesh.file):
                self.fail('le bloc ne doit pas s’exécuter')
        self.assertEqual(temporaires(), avant)

    def test_champ_vide_refuse(self):
        with self.assertRaises(ValueError):
            with storage_backend.local_copy(self.mesh.step_file):
                pass

    def test_download_to_pose_le_fichier_la_ou_on_le_demande(self):
        destination = Path(self.scratch) / 'input_1.step'
        self.assertEqual(storage_backend.download_to(self.mesh.file, destination), destination)
        self.assertEqual(destination.read_bytes(), boite_ply())

    def test_repertoire_de_travail_sous_scratch_root_puis_purge(self):
        dossier = storage_backend.scratch_dir('projects', '7', 'work_3')
        self.assertEqual(dossier, Path(self.scratch) / 'projects' / '7' / 'work_3')
        (dossier / 'db.db').write_bytes(b'x')
        storage_backend.purge_scratch('projects', '7', 'work_3')
        self.assertFalse(dossier.exists())
        storage_backend.purge_scratch('projects', '7', 'jamais-cree')    # sans erreur


class CompteDeServiceTests(Base):

    def test_jeton_demande_une_fois_puis_redemande_a_l_expiration(self):
        self.assertEqual(storage_client._token(), JETON_DE_SERVICE)
        storage_client.listing('', namespace=self.ns)
        storage_client.listing('', namespace=self.ns)
        self.assertEqual(self.storage.demandes_de_jeton, 1)
        self.assertEqual(self.storage.dernier_secret, 'secret-de-test')
        storage_client._token_cache['exp'] = 0.0
        storage_client.listing('', namespace=self.ns)
        self.assertEqual(self.storage.demandes_de_jeton, 2)

    def test_configuration_incomplete_ou_keycloak_en_panne(self):
        with override_settings(KEYCLOAK_ADMIN_CLIENT_SECRET=''):
            with self.assertRaisesMessage(storage_client.StorageClientError, 'manquants'):
                storage_client._token()
        with override_settings(KEYCLOAK_ISSUER_URI='http://keycloak.test/auth'):
            with self.assertRaisesMessage(storage_client.StorageClientError, 'KEYCLOAK_ISSUER_URI invalide'):
                storage_client._token()
        self.assertEqual(self.storage.appels, [])
        with override_settings(KEYCLOAK_ADMIN_CLIENT_SECRET='mauvais'):
            with self.assertRaisesMessage(storage_client.StorageClientError, 'HTTP 401'):
                storage_client._token()
        self.storage.keycloak_arrete = True
        with self.assertRaisesMessage(storage_client.StorageClientError, 'Keycloak injoignable'):
            storage_client._token()

    def test_erreurs_de_storage_traduites(self):
        with self.assertRaises(FileNotFoundError):
            storage_client.download('projects/1/absent', namespace=self.ns)
        storage_client.delete('projects/1/absent', namespace=self.ns)          # 404 toléré
        self.storage.arrete = True
        for appel in (lambda: storage_client.download('a', namespace=self.ns),
                      lambda: storage_client.delete('a', namespace=self.ns),
                      lambda: storage_client.listing('a', namespace=self.ns),
                      lambda: storage_client.upload('a', io.BytesIO(b'x'), 'a', namespace=self.ns),
                      lambda: storage_client.list_members(self.ns),
                      lambda: storage_client.set_member(self.ns, BOB, 'read'),
                      lambda: storage_client.remove_member(self.ns, BOB)):
            with self.assertRaisesMessage(storage_client.StorageClientError, 'storage injoignable'):
                appel()


class ReplicationDesPartagesTests(Base):
    """`sync_project_share` : storage est le rempart de l'accès direct aux
    fichiers, ses membres doivent être exactement ceux du projet."""

    def test_cree_le_partage_et_y_met_le_proprietaire_en_ecriture(self):
        storage_client.sync_project_share(self.p)
        self.assertEqual(self.storage.partages, {self.ns: {ALICE: 'write'}})
        storage_client.sync_project_share(self.p)       # rejouable
        self.assertEqual(len(self.storage.appels_vers('/api/shares/', 'POST')), 2)   # création + 1 membre

    def test_aligne_ajouts_roles_et_retraits(self):
        self.storage.partages[self.ns] = {ALICE: 'read', BOB: 'read', 'ancien@ssolab.local': 'write'}
        ProjectShare.objects.create(project=self.p, email=BOB, role=ProjectShare.Role.EDITOR)
        ProjectShare.objects.create(project=self.p, email=CHLOE, role=ProjectShare.Role.VIEWER)
        storage_client.sync_project_share(self.p)
        self.assertEqual(self.storage.partages[self.ns], {ALICE: 'write', BOB: 'write', CHLOE: 'read'})

    def test_un_partage_par_projet_racine(self):
        autre = self.projet(BOB)
        storage_client.sync_project_share(self.p)
        storage_client.sync_project_share(autre)
        self.assertNotEqual(self.espace(autre), self.ns)
        self.assertEqual(self.storage.partages[self.espace(autre)], {BOB: 'write'})
        self.assertEqual(self.storage.partages[self.ns], {ALICE: 'write'})

    def test_refuse_une_sous_partie(self):
        piece = self.projet(BOB, parent_project=self.p)
        with self.assertRaises(AssertionError):
            storage_client.sync_project_share(piece)
        self.assertEqual(self.storage.partages, {})

    def test_storage_arrete_l_erreur_remonte(self):
        self.storage.arrete = True
        with self.assertRaises(storage_client.StorageClientError):
            storage_client.sync_project_share(self.p)
