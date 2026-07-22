"""
Module Mouvements (Lot 3) — suggestion automatique de découpage en parties
(RANSAC plans/cylindres/sphères, pyransac3d) et suggestion d'axe de jointure à
partir de la zone de contact entre deux parties.

Décision actée (cf. to_do_3D.md, confirmée avec l'utilisateur) : la peinture
manuelle au pinceau 3D reste l'outil principal — cette segmentation globale
n'est qu'un point de départ optionnel que l'utilisateur garde, ajuste (repeint
par-dessus) ou ignore (supprime) entièrement.

Tout ici est synchrone (pas de job Celery) : avec le sous-échantillonnage et la
réduction d'itérations ci-dessous, un ajustement RANSAC complet (jusqu'à
MAX_PRIMITIVES tentatives) prend quelques secondes — bien en-deçà du seuil qui
justifierait le verrou global des jobs lourds (cf. to_do_3D.md —
RECONSTRUCTION/REPAIR/SEGMENTATION_FACADE).
"""
from pathlib import Path

import numpy as np
import pyransac3d as pyrsc
import trimesh
from scipy.spatial import cKDTree

# Seuils de distance RANSAC exprimés en fraction de la diagonale de la bounding
# box du maillage plutôt qu'en valeur absolue : un maillage "bâtiment" et un
# maillage "objet" n'ont pas du tout la même échelle d'unités brutes (souvent
# arbitraire tant que le projet n'est pas calibré, cf. Project.scale_meters_per_unit) —
# un seuil fixe en unités de maillage serait beaucoup trop lâche pour l'un ou
# beaucoup trop strict pour l'autre.
PLANE_THRESH_RATIO = 0.0015
CYLINDER_THRESH_RATIO = 0.0015
SPHERE_THRESH_RATIO = 0.0015
CONTACT_DISTANCE_RATIO = 0.004

# En dessous de ce ratio d'inliers, l'ajustement n'est pas jugé assez net pour
# être proposé comme suggestion — mieux vaut ne rien suggérer qu'une primitive
# fantaisiste (cf. to_do_3D.md : "sinon entièrement manuel"). Volontairement bas
# (un maillage issu de photogrammétrie est bruité, rarement composé à 50 %+
# d'une seule primitive géométrique parfaite) : le but est de repérer un patch
# cohérent (mur, tuyau...), pas d'exiger qu'il domine tout le nuage de points.
MIN_INLIER_RATIO = 0.15
MIN_PART_FACES = 30
MAX_PRIMITIVES = 12

# RANSAC tourne sur un sous-échantillon (le nombre de points n'améliore pas la
# qualité de l'ajustement au-delà d'un certain seuil, seulement son coût) — les
# inliers réels sont recalculés ensuite sur l'intégralité du nuage à partir des
# paramètres ajustés. Vérifié : sans ça, un ajustement de cylindre sur ~50 000
# points avec les itérations par défaut de pyransac3d (10 000) prend plusieurs
# minutes — inutilisable en requête synchrone.
FIT_SUBSAMPLE_SIZE = 3000
CYLINDER_MAX_ITERATION = 300


class SegmentationError(Exception):
    """Erreur métier (maillage introuvable/invalide) — message affichable tel quel."""


def _face_centroids(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    return vertices[faces].mean(axis=1)


def _bbox_diagonal(points: np.ndarray) -> float:
    return float(np.linalg.norm(points.max(axis=0) - points.min(axis=0))) or 1.0


def _subsample(pts: np.ndarray, size: int) -> np.ndarray:
    if len(pts) <= size:
        return pts
    idx = np.random.choice(len(pts), size, replace=False)
    return pts[idx]


def _plane_inliers(pts: np.ndarray, equation, thresh: float) -> np.ndarray:
    normal = np.asarray(equation[:3])
    d = equation[3]
    dist = np.abs(pts @ normal + d) / (np.linalg.norm(normal) or 1.0)
    return np.nonzero(dist < thresh)[0]


def _sphere_inliers(pts: np.ndarray, center, radius: float, thresh: float) -> np.ndarray:
    dist = np.abs(np.linalg.norm(pts - np.asarray(center), axis=1) - radius)
    return np.nonzero(dist < thresh)[0]


def _cylinder_inliers(pts: np.ndarray, center, axis, radius: float, thresh: float) -> np.ndarray:
    axis = np.asarray(axis)
    axis = axis / (np.linalg.norm(axis) or 1.0)
    rel = pts - np.asarray(center)
    proj = np.outer(rel @ axis, axis)
    radial = rel - proj
    dist = np.abs(np.linalg.norm(radial, axis=1) - radius)
    return np.nonzero(dist < thresh)[0]


def _best_fit(pts: np.ndarray):
    """
    Ajuste plan/cylindre/sphère sur un sous-échantillon de `pts` (RANSAC),
    recalcule les inliers réels sur l'intégralité de `pts`, garde le meilleur.
    Retourne (type, params, inlier_indices dans `pts`) ou None si aucun
    ajustement n'atteint MIN_INLIER_RATIO.
    """
    if len(pts) < 6:
        return None
    diag = _bbox_diagonal(pts)
    sample = _subsample(pts, FIT_SUBSAMPLE_SIZE)
    candidates = []

    try:
        equation, _ = pyrsc.Plane().fit(sample, thresh=PLANE_THRESH_RATIO * diag, minPoints=min(100, len(sample)))
        inliers = _plane_inliers(pts, equation, PLANE_THRESH_RATIO * diag)
        # np.ndarray.tolist() convertit en float Python natif (contrairement à
        # list(array), qui ne fait qu'itérer en gardant des np.float64 —
        # non sérialisables tels quels en JSON par Django/DRF).
        candidates.append(('plane', {'normal': np.asarray(equation[:3]).tolist(), 'd': float(equation[3])}, inliers))
    except Exception:
        pass
    try:
        center, axis, radius, _ = pyrsc.Cylinder().fit(
            sample, thresh=CYLINDER_THRESH_RATIO * diag, maxIteration=CYLINDER_MAX_ITERATION,
        )
        inliers = _cylinder_inliers(pts, center, axis, radius, CYLINDER_THRESH_RATIO * diag)
        candidates.append((
            'cylinder',
            {'center': np.asarray(center).tolist(), 'axis': np.asarray(axis).tolist(), 'radius': float(radius)},
            inliers,
        ))
    except Exception:
        pass
    try:
        center, radius, _ = pyrsc.Sphere().fit(sample, thresh=SPHERE_THRESH_RATIO * diag)
        # Un rayon supérieur à la diagonale du nuage de points lui-même trahit un
        # ajustement dégénéré (une surface quasi plane peut toujours être
        # "expliquée" par une sphère de rayon arbitrairement grand) plutôt qu'une
        # vraie sphère locale (ex. rotule) — on l'écarte, le plan la représentera
        # mieux de toute façon.
        if radius <= diag:
            inliers = _sphere_inliers(pts, center, radius, SPHERE_THRESH_RATIO * diag)
            candidates.append(('sphere', {'center': np.asarray(center).tolist(), 'radius': float(radius)}, inliers))
    except Exception:
        pass

    if not candidates:
        return None
    prim_type, params, inliers = max(candidates, key=lambda c: len(c[2]))
    if len(inliers) / len(pts) < MIN_INLIER_RATIO:
        return None
    return prim_type, params, inliers


def suggest_parts(mesh_path: Path) -> list:
    """
    Segmentation globale du maillage en parties candidates : ajustement RANSAC
    itératif sur les centroïdes de faces non encore assignés, la meilleure
    primitive retirée à chaque tour jusqu'à épuisement ou MAX_PRIMITIVES.
    Retourne une liste de {face_ids, primitive_type, primitive_params}.
    """
    if not mesh_path.exists():
        raise SegmentationError(f"Maillage introuvable : {mesh_path}")
    geom = trimesh.load(str(mesh_path), process=False)
    centroids = _face_centroids(geom.vertices, geom.faces)
    remaining = np.arange(len(geom.faces))
    suggestions = []

    for _ in range(MAX_PRIMITIVES):
        if len(remaining) < MIN_PART_FACES:
            break
        best = _best_fit(centroids[remaining])
        if best is None:
            break
        prim_type, params, inlier_local = best
        if len(inlier_local) < MIN_PART_FACES:
            break
        face_ids = remaining[inlier_local]
        suggestions.append({
            'face_ids': face_ids.tolist(),
            'primitive_type': prim_type,
            'primitive_params': params,
        })
        remaining = np.delete(remaining, inlier_local)

    return suggestions


def fit_primitive_to_faces(mesh_path: Path, face_ids: list) -> dict | None:
    """Ajuste la meilleure primitive (plan/cylindre/sphère) aux faces données —
    utilisé pour caractériser une `Part` créée manuellement (peinture)."""
    if not mesh_path.exists():
        raise SegmentationError(f"Maillage introuvable : {mesh_path}")
    geom = trimesh.load(str(mesh_path), process=False)
    pts = _face_centroids(geom.vertices, geom.faces[face_ids])
    best = _best_fit(pts)
    if best is None:
        return None
    prim_type, params, _inliers = best
    return {'primitive_type': prim_type, 'primitive_params': params}


def suggest_joint_axis(mesh_path: Path, face_ids_a: list, face_ids_b: list) -> dict | None:
    """
    Suggestion d'axe de jointure à partir de la zone de contact entre deux
    parties (sommets de A proches d'un sommet de B et vice-versa). Retourne
    {'type': 'revolute'|'prismatic', 'origin': [...], 'direction': [...]} si la
    zone de contact est nettement cylindrique ou planaire, sinon None — repli
    manuel via le viewer (cf. to_do_3D.md).
    """
    if not mesh_path.exists():
        raise SegmentationError(f"Maillage introuvable : {mesh_path}")
    geom = trimesh.load(str(mesh_path), process=False)
    verts_a = np.unique(geom.faces[face_ids_a].ravel())
    verts_b = np.unique(geom.faces[face_ids_b].ravel())
    pts_a = geom.vertices[verts_a]
    pts_b = geom.vertices[verts_b]
    if len(pts_a) == 0 or len(pts_b) == 0:
        return None

    diag = _bbox_diagonal(geom.vertices)
    contact_dist = CONTACT_DISTANCE_RATIO * diag
    dist_a, _ = cKDTree(pts_b).query(pts_a)
    dist_b, _ = cKDTree(pts_a).query(pts_b)
    contact = np.concatenate([pts_a[dist_a < contact_dist], pts_b[dist_b < contact_dist]], axis=0)
    if len(contact) < MIN_PART_FACES:
        return None

    best = _best_fit(contact)
    if best is None:
        return None
    prim_type, params, _inliers = best

    if prim_type == 'cylinder':
        return {'type': 'revolute', 'origin': params['center'], 'direction': params['axis']}
    if prim_type == 'plane':
        return {'type': 'prismatic', 'origin': contact.mean(axis=0).tolist(), 'direction': params['normal']}
    return None
