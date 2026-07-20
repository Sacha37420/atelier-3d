"""
Extraction de frames depuis une vidéo uploadée (cf. to_do_3D.md Lot 1) :
ffmpeg pour le sous-échantillonnage temporel (fps réduit — garantit une baseline
suffisante entre frames retenues), puis filtre de netteté par variance du
Laplacien (élimine les frames floues de mouvement de caméra, qui dégraderaient
le SfM bien plus qu'elles ne l'aideraient).
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from django.core.files import File
from PIL import Image
from scipy import ndimage

from .models import Photo

# Fréquence d'échantillonnage temporel : une frame toutes les 0.5s. Suffisant
# pour garantir une baseline (parallaxe) entre frames retenues sans multiplier
# inutilement le nombre de photos à traiter par le SfM.
DEFAULT_FPS = 2.0

# Pourcentile de netteté sous lequel une frame est jugée trop floue et rejetée.
SHARPNESS_REJECT_PERCENTILE = 25

# Ne pas dépasser ce nombre de frames retenues, même si toutes sont nettes —
# garde-fou contre un job de matching exhaustif O(n²) hors de contrôle sur une
# vidéo longue (l'utilisateur voit le nombre retenu et peut choisir un fps plus
# bas s'il veut plus de frames).
MAX_FRAMES = 120


def _laplacian_sharpness(image_path: Path) -> float:
    """Variance du Laplacien : plus la valeur est haute, plus l'image est nette."""
    img = Image.open(image_path).convert('L')
    img.thumbnail((512, 512))  # la netteté relative suffit, pas besoin du plein format
    arr = np.asarray(img, dtype=np.float64)
    return float(ndimage.laplace(arr).var())


def extract_frames_from_video(video_path: Path, project, fps: float = DEFAULT_FPS) -> list:
    """
    Extrait des frames de `video_path` via ffmpeg, filtre les plus floues, et
    crée un objet `Photo` par frame retenue (dans l'ordre de la vidéo).
    Retourne la liste des `Photo` créées.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix='atelier3d-frames-'))
    try:
        subprocess.run(
            [
                'ffmpeg', '-y', '-i', str(video_path),
                '-vf', f'fps={fps}',
                '-qscale:v', '2',
                str(tmpdir / 'frame_%05d.jpg'),
            ],
            check=True, capture_output=True,
        )

        frame_paths = sorted(tmpdir.glob('frame_*.jpg'))
        if not frame_paths:
            return []

        scored = [(p, _laplacian_sharpness(p)) for p in frame_paths]
        scores_sorted = sorted(s for _, s in scored)
        threshold = (
            scores_sorted[len(scores_sorted) * SHARPNESS_REJECT_PERCENTILE // 100]
            if len(scores_sorted) > 4 else 0.0
        )
        kept = [p for p, s in scored if s >= threshold]

        # Sous-échantillonne uniformément si le filtre de netteté ne suffit pas
        # à repasser sous MAX_FRAMES (garde une couverture régulière de la vidéo).
        if len(kept) > MAX_FRAMES:
            step = len(kept) / MAX_FRAMES
            kept = [kept[int(i * step)] for i in range(MAX_FRAMES)]

        photos = []
        for order, frame_path in enumerate(kept):
            with open(frame_path, 'rb') as fh:
                photo = Photo(project=project, order=order)
                photo.file.save(frame_path.name, File(fh), save=True)
                photos.append(photo)
        return photos
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
