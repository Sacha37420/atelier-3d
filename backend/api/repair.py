"""
Job REPAIR : réparation watertight + décimation (module Impression 3D, Lot 2)
et fonctions synchrones d'orientation d'impression / export STL-3MF.

open3d n'est PAS utilisé (cf. requirements.txt) : ses wheels pip exigent AVX2 et
crashent (SIGILL) au simple `import` sur le CPU cible (2 vCPU, sans AVX). Le
repli « reconstruction de Poisson » prévu par to_do_3D.md pour un maillage trop
dégradé utilise le filtre pymeshlab `generate_surface_reconstruction_screened_poisson`
à la place — même besoin couvert, déjà validé fonctionnel sur ce matériel.
"""
import io
from pathlib import Path

import numpy as np
import pymeshlab
import trimesh

# ── Réparation topologique ─────────────────────────────────────────────────────
# Nombre de passes de fermeture de trous avant de considérer le maillage trop
# dégradé pour une réparation conventionnelle. Vérifié sur un maillage réel issu
# du Lot 1 (18 photos, 341k faces, 3 trous) : une 1re passe avec
# `selfintersection=True` (défaut, refuse de fermer un trou si le patch obtenu
# s'auto-intersecte) laisse des bords résiduels ; il faut une 2e passe avec
# `selfintersection=False` pour les fermer réellement. 3 tentatives couvrent
# largement ce cas avec de la marge.
MAX_REPAIR_ATTEMPTS = 3
# Volontairement très généreux (bien plus grand que n'importe quel trou réel à
# l'échelle d'un objet) : le but ici est de tout fermer pour l'impression, pas
# de préserver une ouverture volontaire.
CLOSE_HOLES_MAX_HOLE_SIZE = 100_000
POISSON_DEPTH = 8
# Seuil (fraction de %, cf. PercentageValue) de fusion des sommets proches avant
# export. Indispensable : l'export PLY par pymeshlab duplique un sommet par coin
# de face texturé (coordonnées de texture par sommet) — sans cette fusion,
# pymeshlab rapporte un maillage watertight en interne mais le fichier exporté ne
# l'est plus du tout au sens où trimesh/un slicer le comprennent (vérifié : des
# milliers d'arêtes de bord apparaissent après rechargement, alors que
# get_topological_measures() disait 0 avant l'export).
MERGE_VERTICES_THRESHOLD_PERCENT = 0.01

# ── Décimation ──────────────────────────────────────────────────────────────────
# Taille exacte d'un STL binaire : en-tête 80 octets + 4 octets de compte de
# faces, puis 50 octets par facette (normale + 3 sommets en float32 + 2 octets
# d'attribut). Formule déterministe (pas une estimation) : le format n'a pas de
# texture à faire varier, contrairement au PLY/glTF de la reconstruction.
STL_HEADER_BYTES = 84
STL_BYTES_PER_FACE = 50
MIN_TARGET_FACES = 100

# ── Orientation d'impression ────────────────────────────────────────────────────
# Heuristique (cf. to_do_3D.md Lot 2) : échantillonne des directions "haut"
# candidates, choisit celle qui minimise la surface en surplomb (faces dont la
# normale pointe vers le bas de plus de OVERHANG_ANGLE_DEG), avec un bonus pour
# une grande face posée bien à plat (FLAT_ANGLE_DEG, proche de la verticale
# descendante) — les deux critères cités par le cahier des charges.
N_ORIENTATION_CANDIDATES = 162
OVERHANG_ANGLE_DEG = 45.0
FLAT_ANGLE_DEG = 5.0
FLAT_BONUS_WEIGHT = 0.3

# STL/3MF n'ont pas de notion d'unité — convention universelle des slicers
# (PrusaSlicer, Cura...) : 1 unité de fichier = 1 mm.
MM_PER_METER = 1000.0

EXPORT_FORMATS = ('stl', '3mf')


class RepairError(Exception):
    """Erreur métier (maillage source manquant/invalide) — message affichable tel quel."""


def estimate_target_faces_for_size_mb(size_mb: float) -> int:
    """Nombre de faces cible pour obtenir un STL binaire d'environ `size_mb` Mo."""
    n = (size_mb * 1024 * 1024 - STL_HEADER_BYTES) / STL_BYTES_PER_FACE
    return max(MIN_TARGET_FACES, round(n))


def _topology(ms: 'pymeshlab.MeshSet') -> dict:
    tm = ms.get_topological_measures()
    tm['is_watertight'] = (
        tm['boundary_edges'] == 0
        and tm['non_two_manifold_edges'] == 0
        and tm['non_two_manifold_vertices'] == 0
    )
    return tm


def repair_mesh(input_path: Path, output_path: Path, target_faces: int | None) -> dict:
    """
    Répare `input_path` (watertight) et décime éventuellement vers `target_faces`,
    écrit le résultat (géométrie seule, sans texture) dans `output_path`.
    Retourne un rapport avant/après exploitable tel quel par le frontend.
    """
    if not input_path.exists():
        raise RepairError(f"Maillage source introuvable : {input_path}")

    ms = pymeshlab.MeshSet()
    try:
        ms.load_new_mesh(str(input_path))
    except Exception as exc:
        raise RepairError(f"Impossible de charger le maillage source : {exc}") from exc

    before = _topology(ms)

    ms.meshing_remove_duplicate_vertices()
    ms.meshing_remove_duplicate_faces()

    method = 'repair'
    tm = before
    for attempt in range(MAX_REPAIR_ATTEMPTS):
        ms.meshing_repair_non_manifold_edges(method='Remove Faces')
        ms.meshing_repair_non_manifold_vertices()
        ms.meshing_close_holes(
            maxholesize=CLOSE_HOLES_MAX_HOLE_SIZE,
            newfaceselected=False,
            selfintersection=(attempt == 0),
        )
        tm = _topology(ms)
        if tm['is_watertight']:
            break

    if not tm['is_watertight']:
        # Maillage trop dégradé pour une fermeture conventionnelle des trous —
        # repli reconstruction de Poisson (cf. to_do_3D.md ; remplace open3d,
        # voir docstring du module).
        method = 'poisson'
        ms.compute_normal_for_point_clouds()
        ms.generate_surface_reconstruction_screened_poisson(depth=POISSON_DEPTH)
        ms.meshing_close_holes(maxholesize=CLOSE_HOLES_MAX_HOLE_SIZE, newfaceselected=False)
        ms.meshing_close_holes(
            maxholesize=CLOSE_HOLES_MAX_HOLE_SIZE, newfaceselected=False, selfintersection=False,
        )
        tm = _topology(ms)

    if target_faces and tm['faces_number'] > target_faces:
        ms.meshing_decimation_quadric_edge_collapse(
            targetfacenum=int(target_faces), preservetopology=True, preserveboundary=True,
        )

    ms.meshing_merge_close_vertices(
        threshold=pymeshlab.PercentageValue(MERGE_VERTICES_THRESHOLD_PERCENT),
    )
    after = _topology(ms)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ms.save_current_mesh(
        str(output_path),
        save_wedge_texcoord=False, save_vertex_color=False, save_vertex_normal=False,
    )

    return {'method': method, 'before': before, 'after': after, 'target_faces': target_faces}


# ── Orientation + export ────────────────────────────────────────────────────────
def _fibonacci_sphere(n: int) -> np.ndarray:
    i = np.arange(n)
    phi = np.arccos(1 - 2 * (i + 0.5) / n)
    theta = np.pi * (1 + 5 ** 0.5) * i
    return np.stack([np.sin(phi) * np.cos(theta), np.sin(phi) * np.sin(theta), np.cos(phi)], axis=1)


def suggest_print_orientation(mesh_path: Path) -> dict:
    """
    Suggestion d'orientation (quaternion [x, y, z, w] amenant la direction choisie
    sur +Z) minimisant la surface en surplomb, avec bonus pour une large face
    posée à plat — calcul synchrone (pure géométrie, quelques centaines de ms
    même sur un maillage dense), pas un job Celery.
    """
    if not mesh_path.exists():
        raise RepairError(f"Maillage introuvable : {mesh_path}")
    geom = trimesh.load(str(mesh_path), process=False)
    normals = geom.face_normals
    areas = geom.area_faces
    total_area = float(areas.sum()) or 1.0

    candidates = _fibonacci_sphere(N_ORIENTATION_CANDIDATES)
    dots = candidates @ normals.T
    overhang_area = ((dots < -np.cos(np.radians(OVERHANG_ANGLE_DEG))) * areas).sum(axis=1)
    flat_area = ((dots < -np.cos(np.radians(FLAT_ANGLE_DEG))) * areas).sum(axis=1)
    cost = overhang_area - FLAT_BONUS_WEIGHT * flat_area

    best_idx = int(np.argmin(cost))
    best_dir = candidates[best_idx]
    transform = trimesh.geometry.align_vectors(best_dir, [0, 0, 1])
    qw, qx, qy, qz = trimesh.transformations.quaternion_from_matrix(transform)

    return {
        'quaternion': [float(qx), float(qy), float(qz), float(qw)],
        'overhang_ratio': float(overhang_area[best_idx] / total_area),
    }


def export_print_file(
    mesh_path: Path, quaternion: list[float], scale_meters_per_unit: float, file_format: str,
) -> bytes:
    """Exporte `mesh_path` orienté (quaternion [x,y,z,w]) et mis à l'échelle réelle
    (1 unité de fichier = 1 mm, convention slicer) en STL ou 3MF, en mémoire."""
    if file_format not in EXPORT_FORMATS:
        raise RepairError(f"Format d'export inconnu : {file_format}")
    if not mesh_path.exists():
        raise RepairError(f"Maillage introuvable : {mesh_path}")

    geom = trimesh.load(str(mesh_path), process=False)
    qx, qy, qz, qw = quaternion
    transform = trimesh.transformations.quaternion_matrix([qw, qx, qy, qz])
    geom.apply_transform(transform)
    geom.apply_scale(scale_meters_per_unit * MM_PER_METER)

    import io
    buf = io.BytesIO()
    geom.export(buf, file_type=file_format)
    return buf.getvalue()
