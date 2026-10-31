# Note — Impression / Mouvements / Bâtiments face aux 3 modes d'initialisation

Suite à l'ajout du mode « Photos client » (import d'un maillage reconstruit par le
client avec son propre logiciel de photogrammétrie), analyse de la compatibilité
des 3 modules avals (Impression, Mouvements, Bâtiments) avec les **3 modes
d'initialisation** désormais disponibles : Photo (reconstruction serveur COLMAP+
OpenMVS), Photos client (import), Conception CAO.

**Pure analyse — aucun code modifié.** Vérifié en lisant le code réel (pas par
supposition), références précises ci-dessous.

---

## Verdict par module

| Module | Photo | Photos client | Conception CAO |
|---|---|---|---|
| **Impression** (Lot 2) | ✅ | ✅ | ✅ |
| **Mouvements** (Lot 3) | ✅ | ✅ | ✅ |
| **Bâtiments** (Lot 4) | ✅ | ⛔ **gap réel** | ⛔ exclusion normale |

Impression et Mouvements sont déjà pleinement adaptés aux 3 modes — rien à
corriger. Bâtiments a un vrai point de défaillance pour le nouveau mode Photos
client ; son indisponibilité en Conception CAO, elle, est correcte et attendue
(voir plus bas pourquoi ce n'est pas la même situation).

---

## Impression (Lot 2) — ✅ pleinement mode-agnostique

Les trois briques du module n'ont **aucune dépendance** à la façon dont le
maillage a été obtenu :

- **Calibration d'échelle** (`Project.scale_meters_per_unit`) : mesure manuelle
  par 2 clics directement sur le maillage chargé dans le viewer three.js
  (`mesh-viewer.component.ts:54-57,354,370`, `impression.component.ts:166-182`,
  `PATCH /api/projects/<id>/`). Ne lit ni photo, ni pose caméra, ni origine du
  maillage — un simple "je clique ici, je clique là, je dis que ça fait 30 cm"
  sur n'importe quel maillage affiché.
- **Réparation** (`RepairLaunchView`, `views.py:389-440`) : seule condition
  `project.meshes.exists()` — vrai dès qu'un `Mesh` existe, quel qu'en soit le
  `job.kind` d'origine.
- **Orientation auto / Export STL-3MF** (`MeshAutoOrientView`/`MeshExportView`,
  `views.py:443-516`) : opèrent sur `mesh.file` (le PLY pivot) via
  `storage_backend.local_copy()` — pure géométrie (`repair.py`), aucune lecture
  de `Photo`/`camera_pose`.

`tasks.run_mesh_import` (le nouveau job Photos client) écrit `mesh.file`
exactement comme les 4 autres pipelines — le module Impression ne voit aucune
différence.

⚠ **Détail mineur sans rapport avec la question posée, à noter en passant** :
le commentaire de `Project.scale_meters_per_unit` (`models.py:81-82`) renvoie
vers *« cf. `Photo.calibration_points` / `Photo.calibration_ref_size`
ci-dessous »* — ces deux champs **n'existent pas** sur le modèle `Photo` actuel
(vérifié : seuls `file`/`order`/`camera_pose`/`region_map`/`region_overlay`/
`region_count`/`created_at`). Un commentaire resté d'une conception antérieure,
remplacée depuis par la calibration 2-clics dans le viewer — inoffensif (aucun
code ne s'appuie dessus) mais trompeur à la lecture.

---

## Mouvements (Lot 3) — ✅ pleinement mode-agnostique

`Part`/`Joint` s'appuient uniquement sur `face_ids` (indices dans le `Trimesh`
unique du maillage — cf. commentaire `models.py` sur `Part.face_ids`) :

- `PartListCreateView`/`PartSuggestView`/`SuggestJointAxisView`
  (`views.py:536-738`) chargent `mesh.file` via `storage_backend.local_copy()`
  et appellent `segmentation.py` — aucune lecture de `Photo`/`camera_pose`.
- `mouvements.component.html:33` affiche `photo_count` en badge, mais c'est un
  simple affichage cosmétique, pas une condition d'accès — vérifié par grep,
  aucune autre référence à `photo_count`/`has_resolved_poses` dans ce module
  frontend.

`run_mesh_import` fusionne systématiquement en un `Trimesh` unique
(`trimesh.util.concatenate(geom.dump())` si l'import est une `Scene`, cf.
`tasks.py`, commentaire de `run_mesh_import`) — exactement l'invariant que Part/
Joint attendent. Le module Mouvements fonctionne donc aussi bien sur un
maillage CAO, importé, ou reconstruit sur ce serveur.

---

## Bâtiments (Lot 4) — ⛔ dépendance dure aux poses caméra COLMAP

### Le mécanisme

`FacadeLaunchView` (`views.py:924-964`) refuse (400) tant que
`project.has_resolved_poses` est faux :

```python
@property
def has_resolved_poses(self) -> bool:
    return self.photos.filter(camera_pose__isnull=False).exists()
```
(`models.py:109-114`)

Or **`Photo.camera_pose` n'est renseigné qu'à un seul endroit dans tout le
code** : `_record_camera_poses()`, appelée uniquement par `run_reconstruction`
(`tasks.py:140-156,234`) — c'est-à-dire uniquement quand COLMAP tourne
réellement sur ce serveur et résout les poses caméra par SfM (`colmap mapper`).
Rien d'autre ne peuple ce champ.

Le module Bâtiments (labellisation 2D par photo, reprojection multi-vues,
régularisation des murs/ouvertures — `facade.py`) a un besoin **structurel** de
cette pose : chaque `PhotoLabel` posé sur une photo doit être reprojeté sur le
maillage 3D via la pose+intrinsèques de la caméra qui a pris cette photo
précise (`facade.classify_faces`). Sans pose, il n'y a tout simplement rien à
reprojeter.

### Par mode

- **Photo** : ✅ — `run_reconstruction` peuple toujours `camera_pose` pour les
  photos effectivement enregistrées par le SfM.
- **Photos client** : ⛔ **gap réel**. Le nouveau mode dépose bien des `Photo`
  (même `PhotoUploadView` que le mode Photo — cf. brief d'origine, réutilisé
  tel quel), mais `run_mesh_import` ne lance aucun calcul de pose : il ne fait
  que convertir le fichier déjà fourni par le client. `has_resolved_poses`
  reste donc faux pour tout projet Photos client, quel que soit le nombre de
  photos déposées.
- **Conception CAO** : ⛔ mais **exclusion normale, pas un bug** — un projet CAO
  n'a généralement aucune photo réelle d'un bâtiment physique ; le module n'a
  conceptuellement rien à segmenter. Cette exclusion existait déjà avant
  l'ajout de Photos client et n'a pas besoin d'être « corrigée ».

### Où ça se voit côté UI aujourd'hui

`batiments.component.ts:57` : `selectableProjects = computed(() =>
this.projects().filter(p => p.has_resolved_poses))` — un projet sans pose
résolue est **silencieusement absent** de la liste de sélection du module
Bâtiments (`batiments.component.html:5-16,22-38`). Un message explicatif
n'apparaît que si **aucun** projet du compte entier n'a de pose résolue
(`batiments.component.html:23-26`, *« Aucun projet n'a de photo à pose caméra
résolue… »*) — dès qu'au moins un projet Photo classique existe, un projet
Photos client se contente de ne pas apparaître dans la liste, sans qu'aucun
message n'explique pourquoi à l'endroit où on s'attendrait à le voir (la page
projet elle-même, `project-detail.component.html`, ne fait aucun lien vers
Bâtiments — seul un lien vers Impression y existe, `:958`). Un propriétaire de
projet Photos client n'a donc aucun moyen de comprendre, depuis son projet, que
Bâtiments lui est fermé et pourquoi.

---

## Préconisations

### Correctif immédiat, faible effort — visibilité de l'exclusion

Avant même d'envisager de lever la limitation technique, remplacer le silence
actuel par une explication : soit un badge/texte sur la page projet
(`project-detail.component.html`) quand `!project.has_resolved_poses &&
project.photo_count > 0` (« Bâtiments indisponible pour ce projet — nécessite
des photos reconstruites en mode Photo (poses caméra), pas Photos client »),
soit un message par entrée manquante côté `batiments.component.html` plutôt
qu'une liste simplement plus courte. Coût : quelques lignes, aucun changement
côté backend, aucun risque de régression.

### Correctif structurel — permettre Bâtiments en mode Photos client

Deux pistes, pas équivalentes en robustesse :

**A. Importer les poses déjà calculées par le logiciel du client (recommandé)**
— les 3 logiciels du guide Photos client exportent tous, en plus du maillage,
un fichier de poses caméra dans leur solveur d'origine :
- Meshroom/AliceVision : `cameras.sfm` (JSON du nœud `StructureFromMotion`,
  pose + intrinsèques par vue).
- RealityScan : export « Registration » (XMP par image ou CSV des paramètres
  caméra internes/externes).
- 3DF Zephyr : export caméras (XML propriétaire ou format Bundler).

  Avantage décisif : ces poses sont **déjà exprimées dans le même repère que le
  maillage exporté** (même session de calcul) — pas de recalage à faire.
  Inconvénient : un analyseur différent par logiciel (3 formats, 3 conventions
  de rotation/axes potentiellement différentes), travail réel non négligeable,
  et rien ne garantit qu'un 4ᵉ logiciel ajouté plus tard au guide n'ajoute pas
  un 4ᵉ format à supporter.

**B. Recalculer les poses côté serveur (déconseillé)** — relancer
`feature_extractor`/`exhaustive_matcher`/`mapper` (sans la partie dense/mesh/
texture, déjà fournie par le client) sur les photos déposées en Photos client,
pour obtenir un nuage de points épars + poses. Problème non trivial : ce nuage
épars recalculé vit dans un repère **différent** de celui du maillage importé
(origine, échelle, orientation arbitraires côté COLMAP vs. arbitraires côté
logiciel client) — il faudrait un recalage géométrique (type ICP) entre les
deux nuages pour aligner les poses recalculées sur le maillage importé, avec
le risque d'échec/imprécision inhérent à tout recalage automatique. Piste plus
fragile que A pour un gain équivalent.

**Recommandation** : traiter A comme un lot à part entière si le besoin se
confirme (au minimum Meshroom d'abord, c'est le format le mieux documenté et
open-source des trois) — pas une correction ponctuelle. En attendant, le
correctif immédiat ci-dessus suffit à éviter la confusion.
