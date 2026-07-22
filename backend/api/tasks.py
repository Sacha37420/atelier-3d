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
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import trimesh
from celery import shared_task
from django.conf import settings
from django.core.files import File
from django.db.models import Max
from PIL import Image, ImageOps

from . import facade as facade_module
from . import repair as repair_module
from .models import Job, Mesh, Photo, PhotoLabel, SemanticClass
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


def _parse_cameras_txt(cameras_txt: Path) -> dict:
    """
    cameras.txt COLMAP : une ligne par caméra, 'CAMERA_ID MODEL WIDTH HEIGHT PARAMS[]'.
    Persisté depuis le Lot 4 (module Bâtiments, cf. facade.py) qui reprojette les
    faces du maillage vers chaque photo — a besoin des intrinsèques, pas
    seulement de la pose, pour ça. `feature_extractor` est appelé avec
    `--ImageReader.single_camera 1` (un seul jeu d'intrinsèques partagé par
    toutes les photos du projet) et `--ImageReader.camera_model SIMPLE_RADIAL`
    (params = [f, cx, cy, k]) — cf. run_reconstruction ci-dessous.
    """
    cameras = {}
    for line in cameras_txt.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split()
        camera_id, model, width, height = parts[0], parts[1], parts[2], parts[3]
        cameras[int(camera_id)] = {
            'model': model,
            'width': int(width),
            'height': int(height),
            'params': [float(p) for p in parts[4:]],
        }
    return cameras


def _select_best_sparse_model(sparse_dir: Path) -> Path:
    """
    `colmap mapper` peut produire plusieurs modèles candidats (sparse/0, sparse/1, ...)
    quand les photos ne se connectent pas toutes en une seule reconstruction — rien ne
    garantit que le modèle 0 soit le plus complet. Vérifié sur un jeu de test réel (18
    photos d'un bâtiment, recouvrement irrégulier entre prises consécutives) : sparse/1
    contenait 10 images enregistrées contre 6 pour sparse/0, silencieusement ignoré par
    une version antérieure de ce pipeline qui lisait toujours sparse/0 — la moitié des
    données utilisables (dont toute une autre façade) était perdue avant même OpenMVS.
    On choisit ici le modèle avec le plus d'images enregistrées via `colmap model_analyzer`.
    """
    candidates = sorted(d for d in sparse_dir.iterdir() if d.is_dir() and d.name.isdigit())
    if not candidates:
        raise ReconstructionError(
            "Aucune reconstruction exploitable : trop peu de recouvrement entre les photos, "
            "ou objet/scène insuffisamment texturé pour le SfM. Reprendre des photos avec "
            "davantage de recouvrement entre prises de vue consécutives."
        )
    best_dir, best_count = candidates[0], -1
    for candidate in candidates:
        result = subprocess.run(
            ['colmap', 'model_analyzer', '--path', str(candidate)],
            capture_output=True, text=True,
        )
        match = re.search(r'Registered images:\s*(\d+)', result.stdout + result.stderr)
        count = int(match.group(1)) if match else 0
        if count > best_count:
            best_dir, best_count = candidate, count
    return best_dir


def _record_camera_poses(model_dir: Path, photos: list) -> None:
    txt_out = model_dir.parent / f'{model_dir.name}_txt'
    txt_out.mkdir(parents=True, exist_ok=True)
    _run(['colmap', 'model_converter',
          '--input_path', model_dir, '--output_path', txt_out, '--output_type', 'TXT'])
    registered = _parse_images_txt(txt_out / 'images.txt')
    cameras = _parse_cameras_txt(txt_out / 'cameras.txt')
    for photo in photos:
        pose = registered.get(Path(photo.file.name).name)
        if pose is not None:
            # Intrinsèques dénormalisées directement dans le JSON de la pose —
            # module Bâtiments (Lot 4) : évite de dépendre du répertoire de
            # travail COLMAP brut (jamais nettoyé mais pas modélisé), la
            # reprojection ne lit ensuite plus que Photo.camera_pose.
            pose['intrinsics'] = cameras.get(pose['camera_id'])
        photo.camera_pose = pose
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
        model_dir = _select_best_sparse_model(sparse_dir)
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


@shared_task(bind=True)
def run_repair(self, job_id: int):
    """
    Job REPAIR (Lot 2, module Impression 3D) : réparation watertight + décimation
    du dernier maillage du projet, via `repair.repair_mesh()`. Même verrou global
    que RECONSTRUCTION (posé côté vue, cf. RepairLaunchView).
    """
    job = Job.objects.select_related('project').get(pk=job_id)
    job.celery_task_id = self.request.id
    job.save(update_fields=['celery_task_id'])

    project = job.project
    start = time.monotonic()
    try:
        job.set_state(status=Job.RUNNING, progress=5, message="Chargement du maillage source…")
        source = project.meshes.order_by('-version').first()
        if source is None:
            raise repair_module.RepairError(
                "Aucun maillage à réparer pour ce projet — lancez d'abord une reconstruction."
            )

        workdir = Path(settings.MEDIA_ROOT) / 'projects' / str(project.id) / f'work_{job.id}'
        output_ply = workdir / 'repaired.ply'

        job.set_state(progress=15, message="Réparation watertight (fermeture des trous, non-manifold)…")
        target_faces = job.params.get('target_faces')
        report = repair_module.repair_mesh(Path(source.file.path), output_ply, target_faces)

        job.set_state(progress=80, message="Export du résultat…")
        version = (project.meshes.aggregate(v=Max('version'))['v'] or 0) + 1
        mesh = Mesh(
            project=project, job=job, version=version,
            is_watertight=report['after']['is_watertight'], repair_report=report,
        )
        with open(output_ply, 'rb') as fh:
            mesh.file.save(f'repaired_v{version}.ply', File(fh), save=False)

        # Export glTF pour le viewer three.js — best-effort comme pour la
        # reconstruction (cf. _save_mesh_result) : le PLY réparé reste
        # exploitable même si cet export échoue.
        try:
            import trimesh
            geom = trimesh.load(str(output_ply), process=False)
            gltf_path = workdir / 'repaired.glb'
            geom.export(str(gltf_path))
            with open(gltf_path, 'rb') as fh:
                mesh.gltf_file.save(f'repaired_v{version}.glb', File(fh), save=False)
            mesh.vertex_count = len(geom.vertices)
            mesh.face_count = len(geom.faces)
        except Exception:
            pass
        mesh.save()

        elapsed = time.monotonic() - start
        job.duration_seconds = elapsed
        job.save(update_fields=['duration_seconds'])

        method_label = (
            "reconstruction de Poisson (maillage trop dégradé pour une réparation conventionnelle)"
            if report['method'] == 'poisson' else "fermeture de trous / correction non-manifold"
        )
        job.set_state(
            status=Job.DONE, progress=100,
            message=(
                f"Réparation terminée ({method_label}) : "
                f"{report['before']['number_holes']} trou(s) → {report['after']['number_holes']}, "
                f"{mesh.vertex_count or '?'} sommets / {mesh.face_count or '?'} faces, "
                f"{'watertight' if mesh.is_watertight else 'toujours NON watertight'} — "
                f"calculé en {int(elapsed // 60)}m{int(elapsed % 60):02d}s."
            ),
        )
    except Exception as exc:
        job.set_state(status=Job.ERROR, message=str(exc))


@shared_task(bind=True)
def run_facade_segmentation(self, job_id: int):
    """
    Job SEGMENTATION_FACADE (Lot 4, module Bâtiments) : segmentation sémantique
    murs/fenêtres/portes/toit du dernier maillage du projet par rétro-projection
    multi-vues (cf. facade.py) à partir des `PhotoLabel` déjà posés. Même verrou
    global que RECONSTRUCTION/REPAIR (posé côté vue, cf. FacadeLaunchView).
    """
    job = Job.objects.select_related('project').get(pk=job_id)
    job.celery_task_id = self.request.id
    job.save(update_fields=['celery_task_id'])

    project = job.project
    start = time.monotonic()
    try:
        job.set_state(status=Job.RUNNING, progress=2, message="Préparation…")
        source = project.meshes.order_by('-version').first()
        if source is None:
            raise facade_module.FacadeError(
                "Aucun maillage pour ce projet — lancez d'abord une reconstruction."
            )
        photos = [p for p in project.photos.all() if p.pose_resolved]
        if not photos:
            raise facade_module.FacadeError(
                "Aucune photo avec une pose caméra résolue — ce module réutilise "
                "obligatoirement les poses du Lot 1 (Reconstruction)."
            )
        photo_ids = [p.id for p in photos]
        labels_by_photo = defaultdict(dict)
        for label in PhotoLabel.objects.filter(photo_id__in=photo_ids):
            labels_by_photo[label.photo_id][label.region_index] = label.semantic_class
        if not labels_by_photo:
            raise facade_module.FacadeError(
                "Aucune région labellisée — clique au moins une région par classe "
                "sur une ou deux photos avant de lancer la segmentation."
            )

        job.set_state(progress=4, message="Chargement du maillage source…")
        geom = trimesh.load(str(source.file.path), process=False)

        def progress_cb(i, n, photo):
            job.set_state(
                progress=5 + int(60 * (i + 1) / max(n, 1)),
                message=f"Segmentation 2D + projection ({i + 1}/{n} photos)…",
            )

        class_id, class_names = facade_module.classify_faces(geom, photos, labels_by_photo, progress_cb)

        job.set_state(progress=70, message="Régularisation des murs (RANSAC)…")
        vertices, walls = facade_module.regularize_walls(geom, class_id, class_names)

        job.set_state(progress=80, message="Régularisation des ouvertures (fenêtres/portes)…")
        class_id = facade_module.regularize_openings(vertices, geom.faces, class_id, class_names, walls)

        job.set_state(progress=90, message="Export du résultat…")
        workdir = Path(settings.MEDIA_ROOT) / 'projects' / str(project.id) / f'work_{job.id}'
        workdir.mkdir(parents=True, exist_ok=True)

        version = (project.meshes.aggregate(v=Max('version'))['v'] or 0) + 1
        mesh = Mesh(project=project, job=job, version=version)

        out_ply = workdir / 'facade.ply'
        facade_module.export_ply_with_class_id(vertices, geom.faces, class_id, out_ply)
        with open(out_ply, 'rb') as fh:
            mesh.file.save(f'facade_v{version}.ply', File(fh), save=False)

        try:
            gltf_path = workdir / 'facade.glb'
            facade_module.export_gltf_by_class(vertices, geom.faces, class_id, class_names, gltf_path)
            with open(gltf_path, 'rb') as fh:
                mesh.gltf_file.save(f'facade_v{version}.glb', File(fh), save=False)
        except Exception:
            pass

        mesh.vertex_count = len(vertices)
        mesh.face_count = len(geom.faces)
        mesh.save()

        for i, name in enumerate(class_names):
            face_ids = np.nonzero(class_id == i)[0].tolist()
            if not face_ids:
                continue
            SemanticClass.objects.create(
                mesh=mesh, name=facade_module.CLASS_LABELS.get(name, name),
                color=facade_module.CLASS_COLORS.get(name, '#888888'), face_ids=face_ids,
            )

        elapsed = time.monotonic() - start
        job.duration_seconds = elapsed
        job.save(update_fields=['duration_seconds'])

        n_classified = int((class_id >= 0).sum())
        classes_summary = ', '.join(
            f"{facade_module.CLASS_LABELS.get(n, n)} ({int((class_id == i).sum())})"
            for i, n in enumerate(class_names)
        )
        job.set_state(
            status=Job.DONE, progress=100,
            message=(
                f"Segmentation terminée : {n_classified}/{len(class_id)} faces classées"
                f"{' — ' + classes_summary if classes_summary else ''}, "
                f"calculé en {int(elapsed // 60)}m{int(elapsed % 60):02d}s."
            ),
        )
    except Exception as exc:
        job.set_state(status=Job.ERROR, message=str(exc))
