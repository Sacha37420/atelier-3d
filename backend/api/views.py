import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.text import get_valid_filename
from django.views.static import serve as django_static_serve
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics, status
from rest_framework.response import Response

from . import facade, reconstruction, repair, segmentation, video_import
from .models import Department, UserRecord, Project, Photo, Job, Mesh, Part, Joint, PhotoLabel, SemanticClass
from .serializers import (
    DepartmentSerializer, UserRecordSerializer,
    ProjectSerializer, ProjectDetailSerializer, PhotoSerializer, JobSerializer,
    PartSerializer, JointSerializer, PhotoLabelSerializer, SemanticClassSerializer,
)
from .tasks import run_reconstruction, run_repair, run_facade_segmentation


class MeView(APIView):
    """
    permission_classes = [IsAuthenticated]
    GET /api/me/
    Retourne l'identité de l'utilisateur authentifié (depuis le JWT + DB).
    Crée un UserRecord à la première visite.
    """

    def get(self, request):
        email    = request.user.email
        username = request.user.username
        groups   = request.user.claims.get('groups', [])

        record, created = UserRecord.objects.get_or_create(
            email=email,
            defaults={'display_name': username},
        )

        return Response({
            'email':        email,
            'username':     username,
            'groups':       groups,
            'display_name': record.display_name,
            'department':   DepartmentSerializer(record.department).data
                            if record.department else None,
            'registered_at': record.registered_at,
            'is_new':        created,
        })


class DepartmentListView(generics.ListAPIView):
    """GET /api/departments/ — liste tous les départements."""

    queryset         = Department.objects.all()
    serializer_class = DepartmentSerializer


class UserListView(generics.ListAPIView):
    """GET /api/users/ — liste tous les utilisateurs enregistrés."""

    queryset         = UserRecord.objects.select_related('department')
    serializer_class = UserRecordSerializer


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 1 : Reconstruction
# ──────────────────────────────────────────────────────────────────────────────
class ProjectListCreateView(generics.ListCreateAPIView):
    """GET /api/projects/ — liste ; POST /api/projects/ — crée un projet."""

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer

    def perform_create(self, serializer):
        serializer.save(owner_email=getattr(self.request.user, 'email', ''))


class ProjectDetailView(generics.RetrieveUpdateAPIView):
    """
    GET   /api/projects/<id>/ — détail complet (photos, jobs, maillages).
    PATCH /api/projects/<id>/ — met à jour name/description/project_type, et
                                 surtout `scale_meters_per_unit` (calibration
                                 d'échelle, cf. viewer three.js frontend).
    """

    queryset = Project.objects.all()
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_serializer_context(self):
        # Les vues génériques DRF injectent 'request' par défaut, ce qui fait
        # que FileField (photos, maillages) sérialise en URL absolue via
        # request.build_absolute_uri() — construite à partir du chemin déjà
        # amputé du préfixe '/atelier-3d-api' par Caddy (handle_path le retire
        # avant de transmettre à Django). Résultat : une URL absolue mais
        # incomplète, que le frontend prend pour définitive (mediaUrl() ne
        # préfixe/n'ajoute le token que sur un chemin relatif) et qui 404.
        # Sans 'request' ici, FileField.url reste relatif ('/media/...') et
        # c'est mediaUrl() côté frontend qui construit l'URL complète.
        context = super().get_serializer_context()
        context.pop('request', None)
        return context

    def get_serializer_class(self):
        return ProjectDetailSerializer if self.request.method == 'GET' else ProjectSerializer


class PhotoUploadView(APIView):
    """POST /api/projects/<id>/photos/ — dépôt d'une ou plusieurs photos (glisser-déposer)."""

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        files = request.FILES.getlist('files')
        if not files:
            return Response({'detail': "Aucun fichier reçu (champ 'files')."},
                             status=status.HTTP_400_BAD_REQUEST)
        start_order = project.photos.count()
        created = [
            Photo.objects.create(project=project, file=f, order=start_order + i)
            for i, f in enumerate(files)
        ]
        return Response(PhotoSerializer(created, many=True).data, status=status.HTTP_201_CREATED)

    def delete(self, request, pk, photo_id=None):
        photo = get_object_or_404(Photo, pk=photo_id, project_id=pk)
        photo.file.delete(save=False)
        photo.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VideoUploadView(APIView):
    """
    POST /api/projects/<id>/video/ — dépôt d'une vidéo : extraction automatique de
    frames (ffmpeg, sous-échantillonnage temporel + filtre de netteté par variance
    du Laplacien). Exécuté de façon synchrone : ce n'est PAS l'un des 4 jobs lourds
    du verrou global (cf. to_do_3D.md — seuls RECONSTRUCTION/REPAIR/SEGMENTATION_*
    sont concernés), l'extraction reste rapide devant les étapes SfM/MVS.
    """

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        video_file = request.FILES.get('file')
        if not video_file:
            return Response({'detail': "Aucun fichier reçu (champ 'file')."},
                             status=status.HTTP_400_BAD_REQUEST)
        try:
            fps = float(request.data.get('fps', video_import.DEFAULT_FPS))
        except (TypeError, ValueError):
            return Response({'detail': "Paramètre 'fps' invalide."}, status=status.HTTP_400_BAD_REQUEST)

        tmp_path = Path(tempfile.gettempdir()) / f'atelier3d-upload-{uuid.uuid4().hex}-{video_file.name}'
        with open(tmp_path, 'wb') as fh:
            for chunk in video_file.chunks():
                fh.write(chunk)
        try:
            photos = video_import.extract_frames_from_video(tmp_path, project, fps=fps)
        finally:
            tmp_path.unlink(missing_ok=True)

        if not photos:
            return Response({'detail': "Aucune frame exploitable extraite de la vidéo."},
                             status=status.HTTP_400_BAD_REQUEST)
        start_order = project.photos.count() - len(photos)
        for i, photo in enumerate(photos):
            photo.order = start_order + i
        Photo.objects.bulk_update(photos, ['order'])
        return Response(PhotoSerializer(photos, many=True).data, status=status.HTTP_201_CREATED)


class ReconstructionEstimateView(APIView):
    """
    GET /api/projects/<id>/reconstruct/estimate/?preset=equilibre — estimation de
    durée avant lancement (calibrée sur la mesure réelle du Lot 0), affichée par le
    frontend avant confirmation (aucun job ne se déclenche automatiquement).
    """

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        preset = request.query_params.get('preset', reconstruction.DEFAULT_PRESET)
        if preset not in reconstruction.PRESETS:
            return Response({'detail': 'Preset inconnu.'}, status=status.HTTP_400_BAD_REQUEST)
        n_photos = project.photos.count()
        seconds = reconstruction.estimate_duration_seconds(n_photos, preset)
        return Response({
            'preset': preset,
            'n_photos': n_photos,
            'estimated_seconds': round(seconds),
            'warning_threshold_exceeded': seconds > reconstruction.DURATION_WARNING_THRESHOLD_S,
        })


class ReconstructionLaunchView(APIView):
    """
    POST /api/projects/<id>/reconstruct/ — lance le job RECONSTRUCTION.
    Refuse (409) si un job lourd est déjà PENDING/RUNNING, tous modules et projets
    confondus — verrou applicatif global exigé par to_do_3D.md (CPU partagé avec
    le reste du lab, un seul job lourd actif à la fois). `CELERY_WORKER_CONCURRENCY`
    à 1 (docker-compose) est le filet de sécurité si ce verrou était contourné.
    """

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        preset = request.data.get('preset', reconstruction.DEFAULT_PRESET)
        if preset not in reconstruction.PRESETS:
            return Response({'detail': 'Preset inconnu.'}, status=status.HTTP_400_BAD_REQUEST)
        if project.photos.count() < 3:
            return Response({'detail': "Au moins 3 photos sont nécessaires pour lancer une reconstruction."},
                             status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            if Job.objects.select_for_update().filter(status__in=[Job.PENDING, Job.RUNNING]).exists():
                return Response(
                    {'detail': "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, "
                               "tous modules confondus. Réessayer une fois celui-ci terminé."},
                    status=status.HTTP_409_CONFLICT,
                )
            job = Job.objects.create(
                project=project, kind=Job.RECONSTRUCTION, status=Job.PENDING,
                params={'preset': preset},
                owner_email=getattr(request.user, 'email', ''),
            )
            transaction.on_commit(lambda: run_reconstruction.delay(job.id))

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 2 : Impression 3D
# ──────────────────────────────────────────────────────────────────────────────
class RepairLaunchView(APIView):
    """
    POST /api/projects/<id>/repair/ — lance le job REPAIR (réparation watertight
    + décimation optionnelle) sur le dernier maillage du projet. Body optionnel :
    `target_triangles` (int) OU `target_size_mb` (float) — cible de décimation
    (cf. to_do_3D.md : « nombre de triangles ou poids de fichier »). Même verrou
    global qu'à la reconstruction : un seul job lourd à la fois, tous modules
    confondus.
    """

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not project.meshes.exists():
            return Response(
                {'detail': "Aucun maillage à réparer — lancez d'abord une reconstruction."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_triangles = request.data.get('target_triangles')
        target_size_mb = request.data.get('target_size_mb')
        target_faces = None
        if target_triangles is not None:
            try:
                target_faces = int(target_triangles)
                if target_faces <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return Response({'detail': "'target_triangles' invalide."}, status=status.HTTP_400_BAD_REQUEST)
        elif target_size_mb is not None:
            try:
                size_mb = float(target_size_mb)
                if size_mb <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return Response({'detail': "'target_size_mb' invalide."}, status=status.HTTP_400_BAD_REQUEST)
            target_faces = repair.estimate_target_faces_for_size_mb(size_mb)

        with transaction.atomic():
            if Job.objects.select_for_update().filter(status__in=[Job.PENDING, Job.RUNNING]).exists():
                return Response(
                    {'detail': "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, "
                               "tous modules confondus. Réessayer une fois celui-ci terminé."},
                    status=status.HTTP_409_CONFLICT,
                )
            job = Job.objects.create(
                project=project, kind=Job.REPAIR, status=Job.PENDING,
                params={'target_faces': target_faces},
                owner_email=getattr(request.user, 'email', ''),
            )
            transaction.on_commit(lambda: run_repair.delay(job.id))

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class MeshAutoOrientView(APIView):
    """
    GET /api/meshes/<id>/auto-orient/ — suggestion d'orientation d'impression
    (heuristique : surplomb minimisé par échantillonnage d'orientations, avec
    bonus pour une face posée à plat, cf. to_do_3D.md Lot 2). Calcul synchrone :
    pure géométrie, quelques centaines de ms — ni job Celery, ni verrou global.
    """

    def get(self, request, pk):
        mesh = get_object_or_404(Mesh, pk=pk)
        try:
            suggestion = repair.suggest_print_orientation(Path(mesh.file.path))
        except (repair.RepairError, FileNotFoundError):
            return Response({'detail': "Fichier de maillage introuvable."}, status=status.HTTP_404_NOT_FOUND)
        return Response(suggestion)


class MeshExportView(APIView):
    """
    GET /api/meshes/<id>/export/?file_format=stl|3mf&qx=&qy=&qz=&qw= — export du
    maillage pour impression 3D, orienté selon le quaternion fourni (identité
    par défaut) et mis à l'échelle réelle (1 unité de fichier = 1 mm, convention
    slicer). Bloqué (409) tant que le projet n'a pas d'échelle métrique connue
    (cf. to_do_3D.md — point bloquant explicite). Pas de slicing/g-code (hors
    périmètre).

    Le paramètre s'appelle `file_format`, PAS `format` : DRF réserve `?format=`
    pour sa négociation de contenu (sélection du renderer, cf. `URL_FORMAT_OVERRIDE`)
    — une valeur non reconnue ('stl') y échoue silencieusement en 404 avant même
    d'atteindre `get()` (vérifié : reproductible, aucune trace dans le code de la vue).
    """

    def get(self, request, pk):
        mesh = get_object_or_404(Mesh, pk=pk)
        project = mesh.project
        if not project.has_scale:
            return Response(
                {'detail': "Échelle non calibrée pour ce projet — impossible d'exporter un fichier "
                           "d'impression tant que le maillage n'a pas d'échelle métrique connue."},
                status=status.HTTP_409_CONFLICT,
            )

        file_format = request.query_params.get('file_format', 'stl')
        if file_format not in repair.EXPORT_FORMATS:
            return Response({'detail': "Format inconnu (attendu : stl ou 3mf)."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            quaternion = [
                float(request.query_params.get('qx', 0.0)),
                float(request.query_params.get('qy', 0.0)),
                float(request.query_params.get('qz', 0.0)),
                float(request.query_params.get('qw', 1.0)),
            ]
        except (TypeError, ValueError):
            return Response({'detail': "Quaternion d'orientation invalide."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            data = repair.export_print_file(
                Path(mesh.file.path), quaternion, project.scale_meters_per_unit, file_format,
            )
        except (repair.RepairError, FileNotFoundError):
            return Response({'detail': "Fichier de maillage introuvable."}, status=status.HTTP_404_NOT_FOUND)

        # project.name est un texte libre utilisateur : passé par get_valid_filename()
        # avant d'atterrir dans l'en-tête Content-Disposition (sinon injection d'en-tête
        # possible via des guillemets/retours à la ligne dans le nom du projet).
        safe_name = get_valid_filename(project.name) or 'export'
        response = HttpResponse(data, content_type='application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{safe_name}_v{mesh.version}.{file_format}"'
        return response


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 3 : Mouvements (parties + jointures)
# ──────────────────────────────────────────────────────────────────────────────
def _fit_and_set_primitive(part: Part) -> None:
    try:
        fit = segmentation.fit_primitive_to_faces(Path(part.mesh.file.path), part.face_ids)
    except segmentation.SegmentationError:
        fit = None
    if fit:
        part.primitive_type = fit['primitive_type']
        part.primitive_params = fit['primitive_params']
    else:
        part.primitive_type = ''
        part.primitive_params = None


class PartListCreateView(APIView):
    """
    GET  /api/meshes/<mesh_id>/parts/ — liste des parties d'un maillage.
    POST /api/meshes/<mesh_id>/parts/ — crée une partie à partir d'une sélection
    de faces peintes dans le viewer (body : {name, face_ids}). Ajuste
    automatiquement la meilleure primitive (plan/cylindre/sphère, cf.
    segmentation.py) sur ces faces — réutilisée plus tard pour suggérer un axe
    de jointure.
    """

    def get(self, request, mesh_id):
        mesh = get_object_or_404(Mesh, pk=mesh_id)
        return Response(PartSerializer(mesh.parts.all(), many=True).data)

    def post(self, request, mesh_id):
        mesh = get_object_or_404(Mesh, pk=mesh_id)
        name = (request.data.get('name') or '').strip()
        face_ids = request.data.get('face_ids')
        if not name:
            return Response({'detail': "Le champ 'name' est requis."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(face_ids, list) or not face_ids:
            return Response({'detail': "'face_ids' doit être une liste non vide."}, status=status.HTTP_400_BAD_REQUEST)

        part = Part(mesh=mesh, name=name, face_ids=face_ids)
        _fit_and_set_primitive(part)
        part.save()
        return Response(PartSerializer(part).data, status=status.HTTP_201_CREATED)


class PartSuggestView(APIView):
    """
    POST /api/meshes/<mesh_id>/parts/suggest/ — segmentation RANSAC globale du
    maillage (cf. to_do_3D.md : suggestion en fond que l'utilisateur garde,
    ajuste ou ignore) ; crée directement les `Part` suggérées (suggested=True).
    Synchrone (quelques secondes, cf. segmentation.py) : pas de job Celery, pas
    concerné par le verrou global des jobs lourds.
    """

    def post(self, request, mesh_id):
        mesh = get_object_or_404(Mesh, pk=mesh_id)
        try:
            suggestions = segmentation.suggest_parts(Path(mesh.file.path))
        except (segmentation.SegmentationError, FileNotFoundError):
            return Response({'detail': "Fichier de maillage introuvable."}, status=status.HTTP_404_NOT_FOUND)

        existing = mesh.parts.count()
        created = [
            Part.objects.create(
                mesh=mesh, name=f"Suggestion {existing + i + 1}", face_ids=s['face_ids'],
                suggested=True, primitive_type=s['primitive_type'], primitive_params=s['primitive_params'],
            )
            for i, s in enumerate(suggestions)
        ]
        return Response(PartSerializer(created, many=True).data, status=status.HTTP_201_CREATED)


class PartDetailView(APIView):
    """PATCH/DELETE /api/parts/<id>/ — renommer/repeindre ou supprimer une partie."""

    def patch(self, request, pk):
        part = get_object_or_404(Part, pk=pk)
        name = request.data.get('name')
        face_ids = request.data.get('face_ids')
        if name is not None:
            part.name = name.strip() or part.name
        if face_ids is not None:
            if not isinstance(face_ids, list) or not face_ids:
                return Response({'detail': "'face_ids' doit être une liste non vide."}, status=status.HTTP_400_BAD_REQUEST)
            part.face_ids = face_ids
            # Repeinte à la main : ce n'est plus une suggestion brute non retouchée.
            part.suggested = False
            _fit_and_set_primitive(part)
        part.save()
        return Response(PartSerializer(part).data)

    def delete(self, request, pk):
        part = get_object_or_404(Part, pk=pk)
        part.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _creates_cycle(parent: Part, child: Part) -> bool:
    """Remonte l'arbre depuis `parent` par les jointures existantes : si `child`
    y apparaît, relier parent→child fermerait un cycle."""
    current = parent
    seen = set()
    while True:
        if current.pk == child.pk:
            return True
        if current.pk in seen:
            return False  # garde-fou : l'arbre existant est censé déjà être valide
        seen.add(current.pk)
        joint = Joint.objects.filter(child_part=current).first()
        if joint is None:
            return False
        current = joint.parent_part


class JointListCreateView(APIView):
    """
    GET  /api/meshes/<mesh_id>/joints/ — liste des jointures du maillage.
    POST /api/meshes/<mesh_id>/joints/ — crée une jointure entre deux `Part` du
    même maillage. L'arbre cinématique exige qu'une partie n'ait qu'un seul
    parent (pas de nœud à deux parents) et qu'aucun cycle ne se forme.
    """

    def get(self, request, mesh_id):
        mesh = get_object_or_404(Mesh, pk=mesh_id)
        joints = Joint.objects.filter(parent_part__mesh=mesh)
        return Response(JointSerializer(joints, many=True).data)

    def post(self, request, mesh_id):
        mesh = get_object_or_404(Mesh, pk=mesh_id)
        try:
            parent = Part.objects.get(pk=request.data.get('parent_part'), mesh=mesh)
            child = Part.objects.get(pk=request.data.get('child_part'), mesh=mesh)
        except (Part.DoesNotExist, ValueError, TypeError):
            return Response(
                {'detail': "'parent_part'/'child_part' doivent désigner des parties existantes de ce maillage."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if parent.pk == child.pk:
            return Response({'detail': "Une jointure ne peut pas relier une partie à elle-même."},
                             status=status.HTTP_400_BAD_REQUEST)
        if Joint.objects.filter(child_part=child).exists():
            return Response(
                {'detail': "Cette partie a déjà une jointure parente — une partie ne peut avoir qu'un "
                           "seul parent dans l'arbre cinématique."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if _creates_cycle(parent, child):
            return Response({'detail': "Cette jointure créerait un cycle dans l'arbre cinématique."},
                             status=status.HTTP_400_BAD_REQUEST)

        joint_type = request.data.get('joint_type')
        if joint_type not in dict(Joint.TYPE_CHOICES):
            return Response({'detail': "'joint_type' invalide."}, status=status.HTTP_400_BAD_REQUEST)

        joint = Joint.objects.create(
            parent_part=parent, child_part=child, joint_type=joint_type,
            axis_origin=request.data.get('axis_origin') or [0, 0, 0],
            axis_direction=request.data.get('axis_direction') or [0, 0, 1],
            limit_min=request.data.get('limit_min'),
            limit_max=request.data.get('limit_max'),
        )
        return Response(JointSerializer(joint).data, status=status.HTTP_201_CREATED)


class JointDetailView(APIView):
    """PATCH/DELETE /api/joints/<id>/ — ajuster ou supprimer une jointure."""

    def patch(self, request, pk):
        joint = get_object_or_404(Joint, pk=pk)
        if 'joint_type' in request.data:
            if request.data['joint_type'] not in dict(Joint.TYPE_CHOICES):
                return Response({'detail': "'joint_type' invalide."}, status=status.HTTP_400_BAD_REQUEST)
            joint.joint_type = request.data['joint_type']
        for field in ('axis_origin', 'axis_direction', 'limit_min', 'limit_max'):
            if field in request.data:
                setattr(joint, field, request.data[field])
        joint.save()
        return Response(JointSerializer(joint).data)

    def delete(self, request, pk):
        joint = get_object_or_404(Joint, pk=pk)
        joint.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SuggestJointAxisView(APIView):
    """
    GET /api/parts/<pk>/suggest-axis/?other=<id> — suggestion d'axe de jointure
    à partir de la zone de contact entre les deux parties (cf. to_do_3D.md :
    suggestion automatique si la zone est cylindrique/planaire, sinon manuel).
    Retourne {'suggestion': {...} | null} — l'absence de suggestion n'est pas
    une erreur, c'est le signal explicite de repli sur le placement manuel
    (manipulateur 3D dans le viewer, cf. ImpressionComponent pour le pattern
    équivalent de calibration par 2 clics — même mécanisme réutilisé côté
    frontend pour poser l'axe à la main).
    """

    def get(self, request, pk):
        part = get_object_or_404(Part, pk=pk)
        other = get_object_or_404(Part, pk=request.query_params.get('other'), mesh=part.mesh)
        try:
            suggestion = segmentation.suggest_joint_axis(
                Path(part.mesh.file.path), part.face_ids, other.face_ids,
            )
        except (segmentation.SegmentationError, FileNotFoundError):
            return Response({'detail': "Fichier de maillage introuvable."}, status=status.HTTP_404_NOT_FOUND)
        return Response({'suggestion': suggestion})


class JobListView(generics.ListAPIView):
    """GET /api/jobs/?project=<id> — liste des jobs (récents, tous modules)."""

    serializer_class = JobSerializer

    def get_queryset(self):
        qs = Job.objects.all()
        project_id = self.request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        return qs[:50]


class JobDetailView(APIView):
    """GET /api/jobs/<id>/ — état d'un job (polling frontend)."""

    def get(self, request, pk):
        try:
            return Response(JobSerializer(Job.objects.get(pk=pk)).data)
        except Job.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)


class MediaView(APIView):
    """
    GET /media/<path> — photos et maillages, derrière la même authentification
    que le reste de l'API (IsAuthenticated + KeycloakJWTAuthentication, y compris
    le contrôle de groupe). Sans cette vue, ces fichiers étaient servis en clair
    par django.views.static.serve, accessibles sans connexion à quiconque devine
    l'URL — contraire au principe du lab (cf. CLAUDE.md, cloisonnement).
    """

    def get(self, request, path):
        return django_static_serve(request._request, path, document_root=settings.MEDIA_ROOT)


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 4 : Bâtiments (segmentation sémantique)
# ──────────────────────────────────────────────────────────────────────────────
class PhotoRegionsView(APIView):
    """
    GET /api/photos/<id>/regions/ — calcule (ou réutilise le cache, cf.
    Photo.region_map) la segmentation 2D zero-shot de cette photo (FastSAM) et
    retourne la photo à jour (overlay coloré + nombre de régions). Synchrone :
    jusqu'à ~15-20s pour une photo au premier appel (mesuré sur le CPU cible) —
    acceptable pour une action ponctuelle sur les 1-2 photos choisies pour la
    labellisation assistée (PAS pour toutes les photos d'un coup, réservé au
    job SEGMENTATION_FACADE). Réponse construite sans passer par un
    get_serializer_context() de vue générique : PhotoSerializer expose deux
    FileField (file, region_overlay), cf. le piège Caddy documenté sur
    ProjectDetailView plus haut dans ce fichier.
    """

    def get(self, request, pk):
        photo = get_object_or_404(Photo, pk=pk)
        if not photo.pose_resolved:
            return Response(
                {'detail': "Cette photo n'a pas de pose caméra résolue — inutilisable pour la "
                           "labellisation (la reprojection multi-vues a besoin de la pose)."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            facade.ensure_photo_regions(photo)
        except Exception as exc:
            return Response({'detail': f"Échec de la segmentation 2D : {exc}"},
                             status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(PhotoSerializer(photo).data)


class PhotoLabelListCreateView(APIView):
    """
    GET  /api/photos/<id>/labels/ — labels déjà posés sur cette photo.
    POST /api/photos/<id>/labels/ — pose (ou met à jour) le label d'une région,
    désignée par un clic dans le viewer 2D frontend : body {x, y (coordonnées
    normalisées 0..1 dans l'image), semantic_class}. Nécessite d'avoir appelé
    GET .../regions/ au moins une fois avant (régions mises en cache).
    """

    def get(self, request, photo_id):
        photo = get_object_or_404(Photo, pk=photo_id)
        return Response(PhotoLabelSerializer(photo.labels.all(), many=True).data)

    def post(self, request, photo_id):
        photo = get_object_or_404(Photo, pk=photo_id)
        semantic_class = request.data.get('semantic_class')
        if semantic_class not in dict(PhotoLabel.CLASS_CHOICES):
            return Response({'detail': "'semantic_class' invalide (attendu : mur, fenetre, porte, toit)."},
                             status=status.HTTP_400_BAD_REQUEST)
        try:
            x = float(request.data.get('x'))
            y = float(request.data.get('y'))
        except (TypeError, ValueError):
            return Response({'detail': "'x'/'y' (coordonnées normalisées 0..1) requis."},
                             status=status.HTTP_400_BAD_REQUEST)
        if not photo.region_map:
            return Response(
                {'detail': "Régions pas encore calculées pour cette photo — "
                           "appeler GET .../regions/ avant de labelliser."},
                status=status.HTTP_409_CONFLICT,
            )

        region_ids = facade.ensure_photo_regions(photo)  # déjà en cache, résout immédiatement
        region_index = facade.region_at(region_ids, x, y)
        if region_index < 0:
            return Response(
                {'detail': "Aucune région détectée à cet endroit — clique sur une zone reconnue "
                           "(pas l'arrière-plan)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        label, _ = PhotoLabel.objects.update_or_create(
            photo=photo, region_index=region_index, defaults={'semantic_class': semantic_class},
        )
        return Response(PhotoLabelSerializer(label).data, status=status.HTTP_201_CREATED)


class PhotoLabelDetailView(APIView):
    """DELETE /api/photo-labels/<id>/ — retire un label posé par erreur."""

    def delete(self, request, pk):
        label = get_object_or_404(PhotoLabel, pk=pk)
        label.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FacadeEstimateView(APIView):
    """
    GET /api/projects/<id>/facade/estimate/ — estimation de durée du job
    SEGMENTATION_FACADE avant lancement (cf. to_do_3D.md : avertir explicitement
    si l'estimation dépasse plusieurs heures, scénario drone). Compte les photos
    à pose résolue (seules concernées par la segmentation 2D) — même seuil
    d'avertissement que la reconstruction (2h, cf. reconstruction.py).
    """

    def get(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        n_photos = project.photos.filter(camera_pose__isnull=False).count()
        seconds = facade.estimate_duration_seconds(n_photos)
        return Response({
            'n_photos': n_photos,
            'estimated_seconds': round(seconds),
            'warning_threshold_exceeded': seconds > reconstruction.DURATION_WARNING_THRESHOLD_S,
        })


class FacadeLaunchView(APIView):
    """
    POST /api/projects/<id>/facade/ — lance le job SEGMENTATION_FACADE sur le
    dernier maillage du projet. Refuse (400) sans maillage, sans photo à pose
    résolue (réutilise obligatoirement les poses du Lot 1), ou sans aucune
    région labellisée (rien à propager). Même verrou global que les autres
    jobs lourds — un seul actif à la fois, tous modules confondus.
    """

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        if not project.has_mesh:
            return Response({'detail': "Aucun maillage — lancez d'abord une reconstruction."},
                             status=status.HTTP_400_BAD_REQUEST)
        if not project.has_resolved_poses:
            return Response(
                {'detail': "Aucune photo avec une pose caméra résolue — ce module réutilise "
                           "obligatoirement les poses du Lot 1 (Reconstruction)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not PhotoLabel.objects.filter(photo__project=project).exists():
            return Response(
                {'detail': "Aucune région labellisée — clique au moins une région par classe "
                           "sur une ou deux photos avant de lancer la segmentation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if Job.objects.select_for_update().filter(status__in=[Job.PENDING, Job.RUNNING]).exists():
                return Response(
                    {'detail': "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, "
                               "tous modules confondus. Réessayer une fois celui-ci terminé."},
                    status=status.HTTP_409_CONFLICT,
                )
            job = Job.objects.create(
                project=project, kind=Job.SEGMENTATION_FACADE, status=Job.PENDING,
                owner_email=getattr(request.user, 'email', ''),
            )
            transaction.on_commit(lambda: run_facade_segmentation.delay(job.id))

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class SemanticClassListView(generics.ListAPIView):
    """GET /api/meshes/<mesh_id>/semantic-classes/ — classes du résultat de segmentation d'un maillage."""

    serializer_class = SemanticClassSerializer

    def get_queryset(self):
        return SemanticClass.objects.filter(mesh_id=self.kwargs['mesh_id'])
