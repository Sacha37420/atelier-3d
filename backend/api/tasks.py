"""
Job RECONSTRUCTION : COLMAP (SfM, poses caméra) -> OpenMVS (densify, mesh, texture).

Pipeline et correctifs repris tels quels du spike technique Lot 0
(dev/atelier-3d-spike/, voir aussi la mémoire atelier-3d-lot0-spike-findings) :
  - `--SiftExtraction.use_gpu 0` / `--SiftMatching.use_gpu 0` sont obligatoires
    même en build CUDA_ENABLED=OFF : sans ça COLMAP tente un contexte OpenGL et
    abort() immédiatement en environnement headless.
  - `TextureMesh` doit recevoir `-m <mesh>.ply` explicitement : son nom de
    fichier mesh déduit par défaut ne correspond pas à ce que produit
    `ReconstructMesh` (suffixe `_mesh` absent de la déduction), sinon échec
    silencieux (exit 1, zéro log).
  - Le bug `_FORTIFY_SOURCE` d'OpenMVS (`Util::formatTime`) est corrigé à la
    compilation (backend/Dockerfile), rien à faire ici.
"""
import subprocess
import time
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.files import File
from django.db.models import Max
from PIL import Image, ImageOps

from .models import Job, Mesh, Photo
from .reconstruction import PRESETS, DEFAULT_PRESET


class ReconstructionError(Exception):
    """Erreur métier (photos insuffisantes, SfM en échec…) — message affichable tel quel."""


def _run(cmd: list) -> None:
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        raise ReconstructionError(
            f"{cmd[0]} a échoué : {result.stderr.strip()[-1500:] or result.stdout.strip()[-1500:]}"
        )


def _copy_resized(src: Path, dst: Path, max_size: int) -> None:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    if max(img.size) > max_size:
        img.thumbnail((max_size, max_size), Image.LANCZOS)
    img.convert('RGB').save(dst, quality=95)


def _parse_images_txt(images_txt: Path) -> dict:
    """cameras/images.txt COLMAP alterne 1 ligne de pose / 1 ligne de points2D."""
    lines = [l for l in images_txt.read_text().splitlines() if l.strip() and not l.startswith('#')]
    registered = {}
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        _, qw, qx, qy, qz, tx, ty, tz, camera_id, name = parts[:10]
        registered[name] = {
            'quaternion_wxyz': [float(qw), float(qx), float(qy), float(qz)],
            'translation': [float(tx), float(ty), float(tz)],
            'camera_id': int(camera_id),
        }
        i += 2
    return registered


def _record_camera_poses(model_dir: Path, photos: list) -> None:
    txt_out = model_dir.parent / f'{model_dir.name}_txt'
    txt_out.mkdir(parents=True, exist_ok=True)
    _run(['colmap', 'model_converter',
          '--input_path', model_dir, '--output_path', txt_out, '--output_type', 'TXT'])
    registered = _parse_images_txt(txt_out / 'images.txt')
    for photo in photos:
        photo.camera_pose = registered.get(Path(photo.file.name).name)
    Photo.objects.bulk_update(photos, ['camera_pose'])


def _save_mesh_result(project, job, textured_ply: Path, dense_dir: Path) -> Mesh:
    import trimesh

    geom = trimesh.load(str(textured_ply), process=False)
    version = (project.meshes.aggregate(v=Max('version'))['v'] or 0) + 1
    mesh = Mesh(project=project, job=job, version=version)

    with open(textured_ply, 'rb') as fh:
        mesh.file.save(f'reconstruction_v{version}.ply', File(fh), save=False)

    # Export glTF pour le viewer three.js (format pivot interne = PLY, cf. to_do_3D.md).
    # Best-effort : le PLY reste exploitable même si cet export échoue.
    try:
        gltf_path = dense_dir / 'scene_dense_texture.glb'
        geom.export(str(gltf_path))
        with open(gltf_path, 'rb') as fh:
            mesh.gltf_file.save(f'reconstruction_v{version}.glb', File(fh), save=False)
    except Exception:
        pass

    mesh.vertex_count = len(geom.vertices) if hasattr(geom, 'vertices') else None
    mesh.face_count = len(geom.faces) if hasattr(geom, 'faces') else None
    mesh.save()
    return mesh


@shared_task(bind=True)
def run_reconstruction(self, job_id: int):
    job = Job.objects.select_related('project').get(pk=job_id)
    job.celery_task_id = self.request.id
    job.save(update_fields=['celery_task_id'])

    project = job.project
    preset = PRESETS.get(job.params.get('preset'), PRESETS[DEFAULT_PRESET])
    workdir = Path(settings.MEDIA_ROOT) / 'projects' / str(project.id) / f'work_{job.id}'
    images_dir = workdir / 'images'
    dense_dir = workdir / 'dense'

    start = time.monotonic()
    try:
        job.set_state(status=Job.RUNNING, progress=2, message="Préparation des photos…")
        images_dir.mkdir(parents=True, exist_ok=True)
        photos = list(project.photos.order_by('order'))
        if len(photos) < 3:
            raise ReconstructionError(
                "Au moins 3 photos sont nécessaires pour lancer une reconstruction."
            )
        for photo in photos:
            _copy_resized(Path(photo.file.path), images_dir / Path(photo.file.name).name,
                          preset['max_image_size'])

        db_path = workdir / 'db.db'
        job.set_state(progress=5, message="Extraction des points d'intérêt (SIFT)…")
        _run([
            'colmap', 'feature_extractor',
            '--database_path', db_path,
            '--image_path', images_dir,
            '--ImageReader.camera_model', 'SIMPLE_RADIAL',
            '--ImageReader.single_camera', '1',
            '--SiftExtraction.use_gpu', '0',
            '--SiftExtraction.max_num_features', preset['max_num_features'],
        ])

        job.set_state(progress=20, message="Mise en correspondance des photos…")
        _run(['colmap', 'exhaustive_matcher',
              '--database_path', db_path, '--SiftMatching.use_gpu', '0'])

        job.set_state(progress=40, message="Reconstruction des poses caméra (SfM)…")
        sparse_dir = workdir / 'sparse'
        sparse_dir.mkdir(parents=True, exist_ok=True)
        _run(['colmap', 'mapper',
              '--database_path', db_path, '--image_path', images_dir,
              '--output_path', sparse_dir])
        model_dir = sparse_dir / '0'
        if not model_dir.exists():
            raise ReconstructionError(
                "Aucune reconstruction exploitable : trop peu de recouvrement entre les photos, "
                "ou objet/scène insuffisamment texturé pour le SfM. Reprendre des photos avec "
                "davantage de recouvrement entre prises de vue consécutives."
            )
        _record_camera_poses(model_dir, photos)
        n_resolved = sum(1 for p in photos if p.pose_resolved)

        job.set_state(progress=50, message="Undistortion des images…")
        dense_dir.mkdir(parents=True, exist_ok=True)
        _run(['colmap', 'image_undistorter',
              '--image_path', images_dir, '--input_path', model_dir,
              '--output_path', dense_dir, '--output_type', 'COLMAP'])

        job.set_state(progress=55, message="Conversion vers OpenMVS…")
        scene_mvs = dense_dir / 'scene.mvs'
        _run(['InterfaceCOLMAP', '-w', dense_dir, '-i', dense_dir, '-o', scene_mvs])

        job.set_state(progress=60, message="Densification du nuage de points…")
        _run(['DensifyPointCloud', scene_mvs, '-w', dense_dir])

        job.set_state(progress=85, message="Reconstruction du maillage…")
        scene_dense = dense_dir / 'scene_dense.mvs'
        _run(['ReconstructMesh', scene_dense, '-w', dense_dir])

        job.set_state(progress=92, message="Texturage du maillage…")
        mesh_ply = dense_dir / 'scene_dense_mesh.ply'
        _run(['TextureMesh', scene_dense, '-m', mesh_ply, '-w', dense_dir])

        job.set_state(progress=97, message="Export du résultat…")
        textured_ply = dense_dir / 'scene_dense_texture.ply'
        mesh = _save_mesh_result(project, job, textured_ply, dense_dir)

        elapsed = time.monotonic() - start
        job.duration_seconds = elapsed
        job.save(update_fields=['duration_seconds'])
        scale_warning = (
            "" if project.has_scale else
            " Échelle non calibrée : le maillage est à une échelle arbitraire "
            "(bloquant pour l'impression 3D — Lot 2)."
        )
        job.set_state(
            status=Job.DONE, progress=100,
            message=(
                f"Reconstruction terminée : {n_resolved}/{len(photos)} photos utilisées, "
                f"maillage {mesh.vertex_count or '?'} sommets / {mesh.face_count or '?'} faces, "
                f"calculé en {int(elapsed // 60)}m{int(elapsed % 60):02d}s.{scale_warning}"
            ),
        )
    except Exception as exc:
        job.set_state(status=Job.ERROR, message=str(exc))
