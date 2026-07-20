"""
Presets qualité/résolution et estimation de durée pour le job RECONSTRUCTION.

Les temps de référence sont ceux mesurés au Lot 0 (spike technique, avant tout
code UI/API) : 18 photos réelles et texturées, ~2816px sur le plus grand côté
(6.84 MPixels/image), pipeline COLMAP 3.11.1 + OpenMVS v2.3.0 CPU-only, sur le
matériel cible (2 vCPU, 16 Go, sans AVX). Détail dans dev/atelier-3d-spike/timings.log :
  feature_extractor 266s + exhaustive_matcher 129s + mapper 5s + image_undistorter 8s
  + InterfaceCOLMAP 1s + DensifyPointCloud 488s + ReconstructMesh 38s + TextureMesh 136s
  = 1071s au total.

L'estimation ci-dessous est volontairement pessimiste (majore plutôt que minore) :
mieux vaut surprendre l'utilisateur par une reconstruction plus rapide que prévu
que l'inverse.
"""

PRESETS = {
    'rapide': {
        'label': 'Rapide',
        'max_image_size': 1200,
        'max_num_features': 4096,
    },
    'equilibre': {
        'label': 'Équilibré',
        'max_image_size': 2000,
        'max_num_features': 8192,
    },
    'precis': {
        'label': 'Précis',
        'max_image_size': 3200,
        'max_num_features': 8192,
    },
}

DEFAULT_PRESET = 'equilibre'

# ── Calibration (cf. dev/atelier-3d-spike/timings.log, Lot 0) ─────────────────
_CAL_N_PHOTOS = 18
_CAL_LONG_SIDE = 2816
_CAL_FEATURE_MATCH_S = 266 + 129        # feature_extractor + exhaustive_matcher
_CAL_DENSE_S = 488 + 38 + 136           # DensifyPointCloud + ReconstructMesh + TextureMesh
_CAL_FIXED_S = 5 + 8 + 1                # mapper + image_undistorter + InterfaceCOLMAP

# Avertir avant de lancer un job dont l'estimation dépasse ce seuil (cf. Lot 4 —
# scénario drone, centaines de photos — mais utile dès le Lot 1 pour un gros lot
# de photos d'objet).
DURATION_WARNING_THRESHOLD_S = 2 * 3600


def estimate_duration_seconds(n_photos: int, preset: str) -> float:
    """
    Estimation grossière du temps total de reconstruction pour `n_photos` avec
    le preset donné. Le matching exhaustif domine en O(n²) ; la densification et
    le texturage en O(n × pixels).
    """
    cfg = PRESETS[preset]
    pixel_ratio = (cfg['max_image_size'] / _CAL_LONG_SIDE) ** 2
    n = max(n_photos, 1)
    feature_match = _CAL_FEATURE_MATCH_S * (n / _CAL_N_PHOTOS) ** 2 * pixel_ratio
    dense = _CAL_DENSE_S * (n / _CAL_N_PHOTOS) * pixel_ratio
    return feature_match + dense + _CAL_FIXED_S
