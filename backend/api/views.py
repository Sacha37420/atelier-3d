import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.views.static import serve as django_static_serve
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics, status
from rest_framework.response import Response

from . import reconstruction, video_import
from .models import Department, UserRecord, Project, Photo, Job
from .serializers import (
    DepartmentSerializer, UserRecordSerializer,
    ProjectSerializer, ProjectDetailSerializer, PhotoSerializer, JobSerializer,
)
from .tasks import run_reconstruction


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
