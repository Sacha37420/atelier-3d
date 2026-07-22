"""
Job SEGMENTATION_FACADE (Lot 4, module Bâtiments) — segmentation sémantique
murs/fenêtres/portes/toit d'un maillage de bâtiment par rétro-projection
multi-vues, à partir d'une labellisation assistée sur une ou deux photos.

Réutilise obligatoirement les poses caméra + intrinsèques du Lot 1
(Photo.camera_pose, cf. tasks._record_camera_poses) — aucune reconstruction ni
correspondance 3D n'est recalculée ici. « Aucun modèle pré-entraîné spécifique
façades » (cf. to_do_3D.md) : la segmentation 2D est du découpage en régions
zero-shot générique (FastSAM), pas une classification — le nom de la classe
vient uniquement des clics de l'utilisateur.

**Segmentation 2D — FastSAM (ultralytics), pas SAM classique.** Validé sur le
CPU cible (Pentium G3250, 2 vCPU, PAS d'AVX — vérifié via /proc/cpuinfo) avant
d'écrire ce module : contrairement à open3d (cf. repair.py, SIGILL au simple
import), torch 2.13 + torchvision + ultralytics tournent sans crash sur ce
CPU. Mesuré sur une photo réelle (3072×2304, dataset south-building du Lot 0) :
~14s/photo (4s inférence + 9s post-traitement des masques, imgsz=1024) — accepté
explicitement par to_do_3D.md ("accepter une latence par image, traiter en
batch"). Le temps par photo est indépendant de sa résolution d'origine (FastSAM
downscale à `imgsz` en interne) — l'estimation de durée ci-dessous est donc une
simple fonction linéaire du nombre de photos.

**Visibilité multi-vues — PAS de raycast d'occlusion complet.** Mesuré avant
d'écrire ce module : `trimesh.ray.ray_triangle.RayMeshIntersector` (backend
pur Python/rtree, sans pyembree) n'a pas terminé 50 000 rayons contre un
maillage synthétique de 328k faces en moins de 7 minutes (tué manuellement) —
totalement impraticable à l'échelle mesh × photos de ce module (jusqu'à
plusieurs centaines de milliers de faces × plusieurs centaines de photos en
scénario drone, cf. to_do_3D.md). Repli délibéré, même logique que le
sous-échantillonnage RANSAC de segmentation.py : un test de visibilité
approximatif (face orientée vers la caméra + dans le cadre image), SANS
occlusion par d'autres parties du bâtiment. Un vote erroné issu d'une face
occluse dans UNE photo est en pratique noyé par le vote majoritaire des autres
vues qui la voient correctement — installer pyembree (dépendance C++ lourde)
pour un raycast exact est hors de proportion avec ce que ça achète ici.
"""
import colorsys
from pathlib import Path

import numpy as np
import pyransac3d as pyrsc
import trimesh
from PIL import Image

CLASS_MUR = 'mur'
CLASS_FENETRE = 'fenetre'
CLASS_PORTE = 'porte'
CLASS_TOIT = 'toit'
CLASS_NAMES = [CLASS_MUR, CLASS_FENETRE, CLASS_PORTE, CLASS_TOIT]
CLASS_LABELS = {CLASS_MUR: 'Mur', CLASS_FENETRE: 'Fenêtre', CLASS_PORTE: 'Porte', CLASS_TOIT: 'Toit'}
CLASS_COLORS = {CLASS_MUR: '#c9a876', CLASS_FENETRE: '#5fb3d9', CLASS_PORTE: '#8a5a3c', CLASS_TOIT: '#b5493f'}
UNCLASSIFIED_LABEL = 'Non classé'
UNCLASSIFIED_COLOR = '#5a5f6b'

# ── Segmentation 2D zero-shot (FastSAM) ──────────────────────────────────────
# Poids baké dans l'image au build (cf. Dockerfile) — pas de téléchargement au
# runtime d'un job (fiabilité : un job de plusieurs heures ne doit pas pouvoir
# échouer à cause d'un flake réseau sur une ressource déjà disponible ailleurs
# dans l'image, même logique que COLMAP/OpenMVS compilés au build, Lot 0).
FASTSAM_WEIGHTS = '/opt/fastsam/FastSAM-s.pt'
FASTSAM_IMG_SIZE = 1024
FASTSAM_CONF = 0.4
FASTSAM_IOU = 0.9
_fastsam_model = None  # chargé une fois par process worker (import + poids ~1s)

# ── Estimation de durée (cf. to_do_3D.md — avertir avant un job drone-scale) ──
# Mesuré sur le CPU cible : ~14s/photo, indépendant de la résolution d'origine
# (imgsz fixe la taille réellement traitée). Valeur pessimiste (15s) par
# cohérence avec reconstruction.py ("majore plutôt que minore").
FASTSAM_SECONDS_PER_PHOTO = 15.0
FIXED_OVERHEAD_S = 30.0  # chargement maillage, régularisation, export

# ── Vote multi-vues ───────────────────────────────────────────────────────────
# Une région n'est propagée (passe 2) que si elle est dominée par une classe
# parmi les faces déjà classées (passe 1) qui y projettent — évite de propager
# une classe sur la base d'un vote marginal/ambigu.
REGION_PROPAGATION_MIN_RATIO = 0.5
# ET seulement si une part minimale de la région est effectivement classée
# (pas juste un ratio parmi une poignée de faces classées par hasard) — cf.
# facade.classify_faces, garde-fou contre l'inondation d'une grande région.
REGION_PROPAGATION_MIN_COVERAGE = 0.2

# ── Régularisation RANSAC des murs ────────────────────────────────────────────
# Mêmes seuils relatifs à la diagonale de la bounding box que segmentation.py
# (Lot 3) — un maillage "bâtiment" n'a pas une échelle d'unités fixe tant que
# non calibré (cf. Project.scale_meters_per_unit).
WALL_PLANE_THRESH_RATIO = 0.0015
MIN_WALL_COMPONENT_FACES = 20
FIT_SUBSAMPLE_SIZE = 3000

# ── Régularisation rectangulaire des ouvertures ───────────────────────────────
MIN_OPENING_COMPONENT_FACES = 5
OPENING_WALL_DIST_RATIO = 0.01  # distance max (× diagonale) d'une ouverture à "son" mur
OPENING_RECT_PADDING_STD = 2.2  # demi-étendue du rectangle = ce facteur × écart-type (PCA)


class FacadeError(Exception):
    """Erreur métier (maillage/photos manquants, aucun label posé…) — message affichable tel quel."""


def estimate_duration_seconds(n_photos: int) -> float:
    return FASTSAM_SECONDS_PER_PHOTO * n_photos + FIXED_OVERHEAD_S


def _bbox_diagonal(points: np.ndarray) -> float:
    return float(np.linalg.norm(points.max(axis=0) - points.min(axis=0))) or 1.0


def _subsample(pts: np.ndarray, size: int) -> np.ndarray:
    if len(pts) <= size:
        return pts
    idx = np.random.choice(len(pts), size, replace=False)
    return pts[idx]


# ──────────────────────────────────────────────────────────────────────────────
# Segmentation 2D zero-shot par région (FastSAM), avec cache sur Photo
# ──────────────────────────────────────────────────────────────────────────────
def _get_fastsam_model():
    global _fastsam_model
    if _fastsam_model is None:
        from ultralytics import FastSAM
        _fastsam_model = FastSAM(FASTSAM_WEIGHTS)
    return _fastsam_model


def _region_color(index: int) -> tuple:
    # Espacement par nombre d'or : couleurs bien réparties sur la roue teinte,
    # stables (même indice -> même couleur d'un appel à l'autre).
    hue = (index * 0.61803398875) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.95)
    return int(r * 255), int(g * 255), int(b * 255)


def _compute_regions(image_path: Path) -> np.ndarray:
    """
    Segmentation zero-shot par région d'une image (FastSAM, mode "segment
    everything"). Les masques proposés se chevauchent souvent — chaque pixel
    est attribué au plus PETIT masque qui le couvre (les masques sont assignés
    du plus petit au plus grand, sans écraser un pixel déjà pris), pour un
    découpage cohérent en régions disjointes plutôt qu'un empilement arbitraire.
    Retourne un tableau (H, W) int32, -1 = aucune région (fond/arrière-plan).

    Image chargée via PIL + `ImageOps.exif_transpose` (pas le chemin brut passé
    tel quel au modèle) pour matcher exactement `tasks._copy_resized()`, qui
    applique le même correctif avant de fournir les photos à COLMAP — sans ça,
    une photo dont l'orientation EXIF diffère de l'orientation "pixels bruts"
    (cas courant d'un smartphone tenu à la verticale) désalignerait la carte de
    régions par rapport aux poses caméra, avec le même symptôme que le bug de
    résolution corrigé ci-dessous (classify_faces).
    """
    from PIL import ImageOps

    img = ImageOps.exif_transpose(Image.open(image_path)).convert('RGB')
    model = _get_fastsam_model()
    results = model(
        img, device='cpu', retina_masks=True,
        imgsz=FASTSAM_IMG_SIZE, conf=FASTSAM_CONF, iou=FASTSAM_IOU, verbose=False,
    )
    h, w = results[0].orig_shape
    region_ids = np.full((h, w), -1, dtype=np.int32)
    masks = results[0].masks
    if masks is None:
        return region_ids

    data = masks.data.cpu().numpy() > 0.5  # (n, h, w) bool, déjà à la résolution d'origine (retina_masks=True)
    areas = data.sum(axis=(1, 2))
    order = np.argsort(areas)  # plus petit d'abord
    for new_idx, orig_idx in enumerate(order):
        unassigned = region_ids == -1
        region_ids[data[orig_idx] & unassigned] = new_idx
    return region_ids


def ensure_photo_regions(photo) -> np.ndarray:
    """
    Retourne la carte de régions (H, W) de `photo`, calculée et mise en cache
    (Photo.region_map/.region_overlay/.region_count) au premier appel — jamais
    au moment de l'upload (cf. to_do_3D.md : aucun job auto au dépôt), appelé
    à la demande par la labellisation assistée ou par le job SEGMENTATION_FACADE.
    """
    if photo.region_map and Path(photo.region_map.path).exists():
        return np.load(photo.region_map.path)['region_ids']

    region_ids = _compute_regions(Path(photo.file.path))
    n_regions = int(region_ids.max()) + 1 if region_ids.size and region_ids.max() >= 0 else 0

    from django.core.files.base import ContentFile
    import io

    npz_buf = io.BytesIO()
    np.savez_compressed(npz_buf, region_ids=region_ids)
    photo.region_map.save(f'{photo.id}_regions.npz', ContentFile(npz_buf.getvalue()), save=False)

    overlay = np.zeros((*region_ids.shape, 4), dtype=np.uint8)
    for idx in range(n_regions):
        r, g, b = _region_color(idx)
        mask = region_ids == idx
        overlay[mask] = (r, g, b, 140)
    png_buf = io.BytesIO()
    Image.fromarray(overlay, mode='RGBA').save(png_buf, format='PNG')
    photo.region_overlay.save(f'{photo.id}_overlay.png', ContentFile(png_buf.getvalue()), save=False)

    photo.region_count = n_regions
    photo.save(update_fields=['region_map', 'region_overlay', 'region_count'])
    return region_ids


def region_at(region_ids: np.ndarray, x_norm: float, y_norm: float) -> int:
    """Résout un clic (coordonnées normalisées 0..1, indépendantes de la
    résolution d'affichage côté client) en indice de région."""
    h, w = region_ids.shape
    x = min(w - 1, max(0, int(x_norm * w)))
    y = min(h - 1, max(0, int(y_norm * h)))
    return int(region_ids[y, x])


# ──────────────────────────────────────────────────────────────────────────────
# Projection caméra (convention COLMAP images.txt : X_cam = R(q)·X_world + t)
# ──────────────────────────────────────────────────────────────────────────────
def _quat_to_matrix(qw: float, qx: float, qy: float, qz: float) -> np.ndarray:
    n = qw * qw + qx * qx + qy * qy + qz * qz
    if n < 1e-12:
        return np.eye(3)
    s = 2.0 / n
    wx, wy, wz = s * qw * qx, s * qw * qy, s * qw * qz
    xx, xy, xz = s * qx * qx, s * qx * qy, s * qx * qz
    yy, yz, zz = s * qy * qy, s * qy * qz, s * qz * qz
    return np.array([
        [1 - (yy + zz), xy - wz, xz + wy],
        [xy + wz, 1 - (xx + zz), yz - wx],
        [xz - wy, yz + wx, 1 - (xx + yy)],
    ])


def _pose_matrix(camera_pose: dict) -> tuple:
    R = _quat_to_matrix(*camera_pose['quaternion_wxyz'])
    t = np.asarray(camera_pose['translation'], dtype=np.float64)
    return R, t


def _project(cam_pts: np.ndarray, depths: np.ndarray, intrinsics: dict) -> np.ndarray:
    """Projette des points déjà en repère caméra (Nx3) vers des pixels (Nx2),
    modèle SIMPLE_RADIAL de COLMAP (params = f, cx, cy, k) — seul modèle utilisé
    par ce pipeline (cf. tasks.run_reconstruction, ImageReader.camera_model)."""
    if intrinsics.get('model') != 'SIMPLE_RADIAL':
        raise FacadeError(f"Modèle de caméra non supporté pour la reprojection : {intrinsics.get('model')}")
    f, cx, cy, k = intrinsics['params']
    x = cam_pts[:, 0] / depths
    y = cam_pts[:, 1] / depths
    r2 = x * x + y * y
    radial = 1 + k * r2
    u = f * x * radial + cx
    v = f * y * radial + cy
    return np.stack([u, v], axis=1)


# ──────────────────────────────────────────────────────────────────────────────
# Vote multi-vues : rétro-projection -> class_id par face
# ──────────────────────────────────────────────────────────────────────────────
def classify_faces(geom: 'trimesh.Trimesh', photos: list, labels_by_photo: dict, progress_cb=None) -> tuple:
    """
    `labels_by_photo` : {photo.id: {region_index: semantic_class}} (labels posés
    par l'utilisateur, cf. PhotoLabel). Retourne (class_id: np.ndarray[int32] de
    taille n_faces, -1 = non classé ; class_names: liste ordonnée des noms de
    classe utilisés, class_id sert d'indice dans cette liste).

    Passe 1 — vote direct : pour chaque photo à pose résolue, projette les
    centroïdes de faces orientées vers la caméra ; si le pixel tombe dans une
    région labellisée de CETTE photo, vote pour sa classe. Majorité des votes
    par face -> class_id.

    Passe 2 — propagation par cohérence régionale ("correspondances multi-vues"
    du cahier des charges) : pour chaque photo (labellisée ou non), regroupe les
    faces visibles par région FastSAM ; si une région est dominée (>50%) par une
    classe parmi les faces déjà classées en passe 1 qui y projettent, les faces
    NON classées de la même région héritent de cette classe — comble les trous
    de la passe 1 dans les vues jamais cliquées par l'utilisateur, en s'appuyant
    sur les frontières de régions plutôt que sur un vote par-triangle isolé.
    """
    centroids = geom.triangles_center
    normals = geom.face_normals
    n_faces = len(centroids)
    votes = [None] * n_faces  # face_idx -> dict(class_name -> count), créé à la demande
    per_photo_visible = {}  # photo.id -> (face_idx array, region_id array)

    n_photos = len(photos)
    for i, photo in enumerate(photos):
        pose = photo.camera_pose
        if progress_cb:
            progress_cb(i, n_photos, photo)
        if not pose or not pose.get('intrinsics'):
            continue

        region_ids = ensure_photo_regions(photo)
        R, t = _pose_matrix(pose)
        cam_pts = centroids @ R.T + t
        depths = cam_pts[:, 2]
        cam_center = -R.T @ t
        view_dir = centroids - cam_center
        frontfacing = np.einsum('ij,ij->i', normals, view_dir) < 0
        valid = frontfacing & (depths > 1e-6)
        candidates = np.nonzero(valid)[0]
        if len(candidates) == 0:
            continue

        pix = _project(cam_pts[candidates], depths[candidates], pose['intrinsics'])
        h, w = region_ids.shape
        # `pose['intrinsics']` calibre l'image REDIMENSIONNÉE que le SfM a réellement
        # utilisée (cf. tasks._copy_resized, preset qualité/résolution — souvent PAS
        # la pleine résolution de la photo source) alors que `region_ids` est calculé
        # sur la photo ORIGINALE (facade._compute_regions charge Photo.file tel quel).
        # Sans cette mise à l'échelle, les pixels projetés (dans le repère de l'image
        # redimensionnée) n'indexent qu'un coin de `region_ids` (repère de l'image
        # d'origine, généralement plus grande) — bug réel constaté à la vérification :
        # 3 des 4 régions labellisées sur un jeu de test réel n'obtenaient JAMAIS un
        # seul vote, tous les votes s'écrasant sur la région couvrant ce coin.
        pix[:, 0] *= w / pose['intrinsics']['width']
        pix[:, 1] *= h / pose['intrinsics']['height']
        inb = (pix[:, 0] >= 0) & (pix[:, 0] < w) & (pix[:, 1] >= 0) & (pix[:, 1] < h)
        face_idx = candidates[inb]
        if len(face_idx) == 0:
            continue
        regs = region_ids[pix[inb, 1].astype(int), pix[inb, 0].astype(int)]
        per_photo_visible[photo.id] = (face_idx, regs)

        labels = labels_by_photo.get(photo.id)
        if labels:
            for f, r in zip(face_idx.tolist(), regs.tolist()):
                cls = labels.get(r)
                if not cls:
                    continue
                if votes[f] is None:
                    votes[f] = {}
                votes[f][cls] = votes[f].get(cls, 0) + 1

    class_id = np.full(n_faces, -1, dtype=np.int32)
    class_names: list = []
    name_to_id: dict = {}

    def _cid(name: str) -> int:
        if name not in name_to_id:
            name_to_id[name] = len(class_names)
            class_names.append(name)
        return name_to_id[name]

    for f, counts in enumerate(votes):
        if counts:
            best = max(counts.items(), key=lambda kv: kv[1])[0]
            class_id[f] = _cid(best)

    # Passe 2 : propagation par cohérence régionale. Calculée à partir d'un
    # instantané figé de la passe 1 (class_id_seed), PAS de `class_id` mis à
    # jour au fil des photos — sans ce gel, un remplissage décidé pour la
    # photo N devient à son tour une "preuve" pour la photo N+1 (dont les
    # régions se recoupent forcément en partie avec celles de N sur le même
    # maillage), ce qui amorce une réaction en chaîne : un bug réel constaté à
    # la vérification a fait dégénérer 3 clics sur 3 classes différentes en un
    # maillage classé à 99 % dans UNE seule classe après quelques photos.
    # Garde-fou supplémentaire : exige un minimum de faces déjà classées dans
    # la région (pas seulement un ratio parmi elles) — sans ça, 2 faces
    # classées par hasard dans une région de 50 000 faces suffiraient à
    # inonder les 49 998 autres.
    class_id_seed = class_id.copy()
    fills = np.full(n_faces, -1, dtype=np.int32)
    for face_idx, regs in per_photo_visible.values():
        classified = class_id_seed[face_idx] >= 0
        if not classified.any():
            continue
        for reg in np.unique(regs):
            in_region = regs == reg
            in_region_classified = in_region & classified
            n_in_region = int(in_region.sum())
            n_in_region_classified = int(in_region_classified.sum())
            if n_in_region_classified < max(3, REGION_PROPAGATION_MIN_COVERAGE * n_in_region):
                continue
            reg_class_ids = class_id_seed[face_idx[in_region_classified]]
            vals, counts = np.unique(reg_class_ids, return_counts=True)
            top_count = int(counts.max())
            if top_count / n_in_region_classified < REGION_PROPAGATION_MIN_RATIO:
                continue
            top_class = int(vals[np.argmax(counts)])
            to_fill = in_region & (class_id_seed[face_idx] == -1)
            fills[face_idx[to_fill]] = top_class

    class_id = np.where(fills >= 0, fills, class_id_seed)
    return class_id, class_names


# ──────────────────────────────────────────────────────────────────────────────
# Régularisation géométrique
# ──────────────────────────────────────────────────────────────────────────────
def _connected_face_groups(geom: 'trimesh.Trimesh', face_mask: np.ndarray) -> list:
    face_idx = np.nonzero(face_mask)[0]
    if len(face_idx) == 0:
        return []
    adjacency = geom.face_adjacency
    both_in_mask = face_mask[adjacency[:, 0]] & face_mask[adjacency[:, 1]]
    edges = adjacency[both_in_mask]
    return trimesh.graph.connected_components(edges, nodes=face_idx, min_len=1)


def _fit_plane(points: np.ndarray, diag: float):
    """RANSAC plan sur un sous-échantillon (cf. segmentation.py — même piège de
    perf), recalcule les inliers réels sur l'intégralité de `points`."""
    sample = _subsample(points, FIT_SUBSAMPLE_SIZE)
    thresh = WALL_PLANE_THRESH_RATIO * diag
    try:
        equation, _ = pyrsc.Plane().fit(sample, thresh=thresh, minPoints=min(100, len(sample)))
    except Exception:
        return None
    normal = np.asarray(equation[:3])
    normal = normal / (np.linalg.norm(normal) or 1.0)
    d = float(equation[3])
    return {'normal': normal, 'd': d}


def regularize_walls(geom: 'trimesh.Trimesh', class_id: np.ndarray, class_names: list) -> tuple:
    """
    Ajuste un plan RANSAC par COMPOSANTE CONNEXE de faces "mur" (pas un plan
    global — un bâtiment a plusieurs murs non coplanaires) et projette
    orthogonalement les sommets de chaque composante sur son plan (aplanit le
    bruit de reconstruction, cf. to_do_3D.md). Retourne (vertices régularisés,
    liste de {face_ids, plane} par mur — réutilisée par regularize_openings).
    """
    vertices = geom.vertices.copy()
    if CLASS_MUR not in class_names:
        return vertices, []
    mur_id = class_names.index(CLASS_MUR)
    mask = class_id == mur_id
    diag = _bbox_diagonal(geom.vertices)
    groups = _connected_face_groups(geom, mask)

    walls = []
    for face_ids in groups:
        if len(face_ids) < MIN_WALL_COMPONENT_FACES:
            continue
        vert_ids = np.unique(geom.faces[face_ids].ravel())
        plane = _fit_plane(geom.vertices[vert_ids], diag)
        if plane is None:
            continue
        normal, d = plane['normal'], plane['d']
        pos = vertices[vert_ids]
        dist = pos @ normal + d
        vertices[vert_ids] = pos - np.outer(dist, normal)
        walls.append({'face_ids': np.asarray(face_ids), 'normal': normal, 'd': d})

    return vertices, walls


def regularize_openings(
    geom_vertices: np.ndarray, geom_faces: np.ndarray, class_id: np.ndarray, class_names: list, walls: list,
) -> np.ndarray:
    """
    Pour chaque composante connexe de faces "fenêtre"/"porte" : trouve le mur
    le plus proche (distance moyenne au plan), projette les sommets du
    bâtiment proches de ce mur dans la base 2D du plan, ajuste un rectangle
    orienté par PCA sur la composante, puis réassigne à cette classe toute face
    du même mur dont le centroïde projeté tombe dans le rectangle ET reste
    proche du plan — comble les bords irréguliers de la détection 2D zero-shot.
    Retourne un nouveau tableau class_id (copie).
    """
    class_id = class_id.copy()
    if not walls:
        return class_id
    diag = _bbox_diagonal(geom_vertices)
    dist_thresh = OPENING_WALL_DIST_RATIO * diag
    face_centroids = geom_vertices[geom_faces].mean(axis=1)

    for opening_name in (CLASS_FENETRE, CLASS_PORTE):
        if opening_name not in class_names:
            continue
        opening_id = class_names.index(opening_name)
        mask = class_id == opening_id
        groups = _connected_face_groups(
            trimesh.Trimesh(vertices=geom_vertices, faces=geom_faces, process=False), mask,
        )

        for face_ids in groups:
            if len(face_ids) < MIN_OPENING_COMPONENT_FACES:
                continue
            comp_centroids = face_centroids[face_ids]

            best_wall, best_mean_dist = None, None
            for wall in walls:
                dist = np.abs(comp_centroids @ wall['normal'] + wall['d'])
                mean_dist = float(dist.mean())
                if mean_dist < dist_thresh and (best_mean_dist is None or mean_dist < best_mean_dist):
                    best_wall, best_mean_dist = wall, mean_dist
            if best_wall is None:
                continue  # ouverture pas nettement associée à un mur régularisé : laissée telle quelle

            normal = best_wall['normal']
            u_axis = np.cross(normal, [0.0, 0.0, 1.0])
            if np.linalg.norm(u_axis) < 1e-6:
                u_axis = np.cross(normal, [0.0, 1.0, 0.0])
            u_axis = u_axis / np.linalg.norm(u_axis)
            v_axis = np.cross(normal, u_axis)

            comp_u = comp_centroids @ u_axis
            comp_v = comp_centroids @ v_axis
            center_u, center_v = comp_u.mean(), comp_v.mean()
            half_u = max(comp_u.std(), 1e-9) * OPENING_RECT_PADDING_STD
            half_v = max(comp_v.std(), 1e-9) * OPENING_RECT_PADDING_STD

            wall_face_ids = best_wall['face_ids']
            candidate_ids = np.union1d(wall_face_ids, face_ids)
            cand_centroids = face_centroids[candidate_ids]
            cand_dist = np.abs(cand_centroids @ normal + best_wall['d'])
            cand_u = cand_centroids @ u_axis
            cand_v = cand_centroids @ v_axis
            in_rect = (
                (np.abs(cand_u - center_u) <= half_u)
                & (np.abs(cand_v - center_v) <= half_v)
                & (cand_dist < dist_thresh)
            )
            class_id[candidate_ids[in_rect]] = opening_id

    return class_id


# ──────────────────────────────────────────────────────────────────────────────
# Export
# ──────────────────────────────────────────────────────────────────────────────
def export_ply_with_class_id(vertices: np.ndarray, faces: np.ndarray, class_id: np.ndarray, out_path: Path) -> None:
    """
    PLY géométrie seule (pas de texture/couleur — même choix que repair.py) AVEC
    une propriété par face `class_id` (format pivot interne, cf. to_do_3D.md).
    Écrit à la main en ASCII plutôt que via trimesh.export() : trimesh ne
    permet pas d'ajouter une propriété de face arbitraire dans son exporteur
    PLY standard ; un lecteur PLY générique (dont trimesh lui-même) ignore
    sans erreur une propriété de face qu'il ne connaît pas, donc ce fichier
    reste chargeable normalement par le reste du pipeline (vertices/faces).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as fh:
        fh.write('ply\nformat ascii 1.0\ncomment atelier-3d Lot 4 (segmentation bâtiment)\n')
        fh.write(f'element vertex {len(vertices)}\n')
        fh.write('property float x\nproperty float y\nproperty float z\n')
        fh.write(f'element face {len(faces)}\n')
        fh.write('property list uchar int vertex_indices\n')
        fh.write('property int class_id\n')
        fh.write('end_header\n')
        for v in vertices:
            fh.write(f'{v[0]} {v[1]} {v[2]}\n')
        for face, cid in zip(faces, class_id):
            fh.write(f'3 {face[0]} {face[1]} {face[2]} {int(cid)}\n')


def _hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def export_gltf_by_class(vertices: np.ndarray, faces: np.ndarray, class_id: np.ndarray, class_names: list, out_path: Path) -> None:
    """glTF en sous-maillages nommés par classe (+ 'Non classé'), cf. to_do_3D.md
    — un noeud par classe, coloré, pour distinguer visuellement les classes
    dans le viewer three.js sans dépendre d'un shader dédié."""
    scene = trimesh.Scene()
    for cid in np.unique(class_id):
        mask = class_id == cid
        if not mask.any():
            continue
        if cid >= 0:
            name = class_names[cid]
            label = CLASS_LABELS.get(name, name)
            color = CLASS_COLORS.get(name, '#999999')
        else:
            label = UNCLASSIFIED_LABEL
            color = UNCLASSIFIED_COLOR

        sub = trimesh.Trimesh(vertices=vertices, faces=faces[mask], process=False)
        sub.remove_unreferenced_vertices()
        rgb = _hex_to_rgb(color)
        sub.visual = trimesh.visual.ColorVisuals(
            sub, face_colors=np.tile([*rgb, 255], (len(sub.faces), 1)),
        )
        scene.add_geometry(sub, node_name=label, geom_name=label)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(out_path))
