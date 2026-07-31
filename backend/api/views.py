import tempfile
import uuid
from pathlib import Path

from django.db import transaction
from django.db.models import Max
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils.text import get_valid_filename
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import AnonRateThrottle
from rest_framework import generics, status
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from . import facade, permissions, reconstruction, repair, segmentation, storage_backend, storage_client, video_import
from .models import (
    Department, UserRecord, Project, ProjectShare, Photo, Job, Mesh, Part, Joint, PhotoLabel,
    SemanticClass, CadSketch, CadOperation, CadAssemblyInstance, CadAssemblyConstraint,
)
from .serializers import (
    DepartmentSerializer, UserRecordSerializer,
    ProjectSerializer, ProjectDetailSerializer, ProjectShareSerializer, PhotoSerializer, JobSerializer,
    PartSerializer, JointSerializer, PhotoLabelSerializer, SemanticClassSerializer,
    CadSketchSerializer, CadOperationSerializer,
    CadAssemblyInstanceSerializer, CadAssemblyConstraintSerializer, PublicAssemblySerializer,
)
from .tasks import (
    run_reconstruction, run_repair, run_facade_segmentation, run_cad_build, run_cad_assemble,
)


class StorageUnavailable(APIException):
    """Levée quand la synchronisation storage (Share/ShareMember) échoue sur un
    chemin où elle ne peut pas être traitée en best-effort : un partage/retrait
    (ProjectShareListCreateView/ProjectShareDetailView) qui ne se répercuterait
    pas dans storage laisserait l'accès direct frontend → storage désynchronisé
    de ProjectShare — c'est justement ce que cette synchronisation est censée
    empêcher (cf. api/storage_client.py, sync_project_share). Même pattern que
    arbre-genealogique/backend/api/views.py (StorageUnavailable/TreeShareViewSet)."""

    status_code = 502
    default_detail = 'Stockage indisponible.'
    default_code = 'storage_unavailable'


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
    """
    GET /api/projects/ — liste des projets TOP-LEVEL uniquement
    (`parent_project` nul), restreinte à ceux visibles par l'utilisateur
    courant (propriétaire, ou membre d'un ProjectShare — cf. api/permissions.py,
    chantier « accès direct storage » du 2026-07-30, Option A : Project est
    privé par défaut depuis ce chantier, ce n'était PAS le cas avant). Une
    sous-partie CAO (Lot 5.1) rattachée à un Project(ASSEMBLY) n'apparaît
    jamais ici, seulement dans `/api/projects/<id>/sub-parts/` de son parent.
    POST /api/projects/ — crée un projet top-level, provisionne son partage
    storage (best-effort : un projet neuf n'a encore aucun fichier, un échec
    ici n'ouvre ni ne ferme aucun accès, le prochain sync_project_share
    rattrapera l'état).
    """

    serializer_class = ProjectSerializer

    def get_queryset(self):
        email = getattr(self.request.user, 'email', '')
        return permissions.visible_projects(email).filter(parent_project__isnull=True)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), 'role_of': self._role_of}

    def _role_of(self, project):
        return permissions.role_on(project, getattr(self.request.user, 'email', ''))

    def perform_create(self, serializer):
        project = serializer.save(owner_email=getattr(self.request.user, 'email', ''))
        try:
            storage_client.sync_project_share(project)
        except storage_client.StorageClientError:
            pass


class ProjectDetailView(generics.RetrieveUpdateAPIView):
    """
    GET   /api/projects/<id>/ — détail complet (photos, jobs, maillages).
          Visible au propriétaire et à tout membre d'un ProjectShare (VIEWER
          ou EDITOR) — cf. api/permissions.py.
    PATCH /api/projects/<id>/ — met à jour name/description/project_type, et
          surtout `scale_meters_per_unit` (calibration d'échelle, cf. viewer
          three.js frontend). Réservé au propriétaire et aux EDITOR.
    """

    http_method_names = ['get', 'patch', 'head', 'options']

    def get_queryset(self):
        email = getattr(self.request.user, 'email', '')
        if self.request.method in ('GET', 'HEAD'):
            return permissions.visible_projects(email)
        return permissions.editable_projects(email)

    def get_serializer_context(self):
        # Les vues génériques DRF injectent 'request' par défaut, ce qui fait
        # que FileField (photos, maillages) sérialise en URL absolue via
        # request.build_absolute_uri() — construite à partir du chemin déjà
        # amputé du préfixe '/atelier-3d-api' par Caddy (handle_path le retire
        # avant de transmettre à Django). Résultat : une URL absolue mais
        # incomplète, que le frontend prend pour définitive (mediaUrl() ne
        # préfixe/n'ajoute le token que sur un chemin relatif) et qui 404.
        # Sans 'request' ici, FileField.url reste relatif ('/api/files/...',
        # cf. api/storage_backend.LabStorage.url() — accès storage direct
        # depuis le 2026-07-30, plus '/media/...') et c'est mediaUrl() côté
        # frontend qui construit l'URL complète (sur storage, plus sur cette
        # app — cf. ApiService.storageBase).
        context = super().get_serializer_context()
        context.pop('request', None)
        context['role_of'] = self._role_of
        return context

    def _role_of(self, project):
        return permissions.role_on(project, getattr(self.request.user, 'email', ''))

    def get_serializer_class(self):
        return ProjectDetailSerializer if self.request.method == 'GET' else ProjectSerializer


# ──────────────────────────────────────────────────────────────────────────────
# Partages — ProjectShare (email + rôle read/write), réservés aux projets
# TOP-LEVEL. Même pattern que TreeShareViewSet (arbre-genealogique), adapté au
# style de vues d'atelier-3d (APIView explicites imbriquées sous
# /api/projects/<id>/…, comme cad-sketches/cad-operations/sub-parts) plutôt
# qu'un ModelViewSet + DefaultRouter (jamais utilisé ailleurs dans cette app).
# ──────────────────────────────────────────────────────────────────────────────
class ProjectShareListCreateView(APIView):
    """
    GET  /api/projects/<id>/shares/ — partages du projet <id>. Réservé au
         propriétaire (gérer les partages, comme supprimer le projet, est une
         prérogative du propriétaire — cf. api/permissions.py, check_owner).
    POST /api/projects/<id>/shares/ — invite quelqu'un (body : {email, role}),
         ou change son rôle si déjà invité (upsert — un second POST pour la
         même adresse ne doit pas échouer avec un 400 incompréhensible, c'est
         le geste attendu pour « passer quelqu'un de lecture à édition »).
         Synchronise storage (Share/ShareMember) dans la même transaction
         locale : un échec storage annule aussi le ProjectShare, plutôt que de
         laisser un partage « actif » côté app mais invisible pour storage
         (cf. api/storage_client.sync_project_share).
    """

    def get(self, request, pk):
        project = permissions.get_owned_project(request, pk)
        return Response(ProjectShareSerializer(project.shares.all(), many=True).data)

    def post(self, request, pk):
        project = permissions.get_owned_project(request, pk)
        # get_owned_project() ne renvoie que des projets top-level
        # (permissions.owned_projects filtre déjà parent_project__isnull=True) —
        # défensif seulement, ne devrait jamais se produire :
        if project.parent_project_id is not None:
            return Response(
                {'detail': "Un sous-projet ne peut pas avoir son propre partage."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = ProjectShareSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        if email == project.owner_email:
            return Response({'detail': 'Ce projet vous appartient déjà.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                share, _ = ProjectShare.objects.update_or_create(
                    project=project,
                    email=email,
                    defaults={
                        'role': serializer.validated_data.get('role', ProjectShare.Role.VIEWER),
                        'invited_by': getattr(request.user, 'email', ''),
                    },
                )
                storage_client.sync_project_share(project)
        except storage_client.StorageClientError as exc:
            raise StorageUnavailable(f'Stockage indisponible : {exc}')
        return Response(ProjectShareSerializer(share).data, status=status.HTTP_201_CREATED)


class ProjectShareDetailView(APIView):
    """PATCH/DELETE /api/project-shares/<id>/ — changer le rôle ou retirer un
    partage. Réservé au propriétaire du projet concerné."""

    def _get_share(self, request, pk) -> ProjectShare:
        share = get_object_or_404(ProjectShare, pk=pk)
        permissions.check_owner(share.project, request)
        return share

    def patch(self, request, pk):
        share = self._get_share(request, pk)
        role = request.data.get('role')
        if role not in dict(ProjectShare.Role.choices):
            return Response({'detail': "'role' invalide."}, status=status.HTTP_400_BAD_REQUEST)
        share.role = role
        try:
            with transaction.atomic():
                share.save(update_fields=['role'])
                storage_client.sync_project_share(share.project)
        except storage_client.StorageClientError as exc:
            raise StorageUnavailable(f'Stockage indisponible : {exc}')
        return Response(ProjectShareSerializer(share).data)

    def delete(self, request, pk):
        share = self._get_share(request, pk)
        project = share.project
        try:
            with transaction.atomic():
                share.delete()
                storage_client.sync_project_share(project)
        except storage_client.StorageClientError as exc:
            raise StorageUnavailable(f'Stockage indisponible : {exc}')
        return Response(status=status.HTTP_204_NO_CONTENT)


class PhotoUploadView(APIView):
    """POST /api/projects/<id>/photos/ — dépôt d'une ou plusieurs photos (glisser-déposer)."""

    def post(self, request, pk):
        project = permissions.get_editable_project(request, pk)
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
        project = permissions.get_editable_project(request, pk)
        photo = get_object_or_404(Photo, pk=photo_id, project=project)
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
        project = permissions.get_editable_project(request, pk)
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
        project = permissions.get_visible_project(request, pk)
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
        project = permissions.get_editable_project(request, pk)
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
        project = permissions.get_editable_project(request, pk)
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
        permissions.check_read(mesh.project, request)
        try:
            with storage_backend.local_copy(mesh.file) as mesh_path:
                suggestion = repair.suggest_print_orientation(mesh_path)
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
        permissions.check_read(project, request)
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
            with storage_backend.local_copy(mesh.file) as mesh_path:
                data = repair.export_print_file(
                    mesh_path, quaternion, project.scale_meters_per_unit, file_format,
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
        with storage_backend.local_copy(part.mesh.file) as mesh_path:
            fit = segmentation.fit_primitive_to_faces(mesh_path, part.face_ids)
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
        permissions.check_read(mesh.project, request)
        return Response(PartSerializer(mesh.parts.all(), many=True).data)

    def post(self, request, mesh_id):
        mesh = get_object_or_404(Mesh, pk=mesh_id)
        permissions.check_write(mesh.project, request)
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
        permissions.check_write(mesh.project, request)
        try:
            with storage_backend.local_copy(mesh.file) as mesh_path:
                suggestions = segmentation.suggest_parts(mesh_path)
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
        permissions.check_write(part.mesh.project, request)
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
        permissions.check_write(part.mesh.project, request)
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
        permissions.check_read(mesh.project, request)
        joints = Joint.objects.filter(parent_part__mesh=mesh)
        return Response(JointSerializer(joints, many=True).data)

    def post(self, request, mesh_id):
        mesh = get_object_or_404(Mesh, pk=mesh_id)
        permissions.check_write(mesh.project, request)
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
        permissions.check_write(joint.parent_part.mesh.project, request)
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
        permissions.check_write(joint.parent_part.mesh.project, request)
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
        permissions.check_read(part.mesh.project, request)
        other = get_object_or_404(Part, pk=request.query_params.get('other'), mesh=part.mesh)
        try:
            with storage_backend.local_copy(part.mesh.file) as mesh_path:
                suggestion = segmentation.suggest_joint_axis(
                    mesh_path, part.face_ids, other.face_ids,
                )
        except (segmentation.SegmentationError, FileNotFoundError):
            return Response({'detail': "Fichier de maillage introuvable."}, status=status.HTTP_404_NOT_FOUND)
        return Response({'suggestion': suggestion})


class JobListView(generics.ListAPIView):
    """GET /api/jobs/?project=<id> — liste des jobs (récents, tous modules),
    restreinte aux projets visibles par l'utilisateur courant."""

    serializer_class = JobSerializer

    def get_queryset(self):
        email = getattr(self.request.user, 'email', '')
        qs = Job.objects.filter(project__in=permissions.visible_projects(email))
        project_id = self.request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        return qs[:50]


class JobDetailView(APIView):
    """GET /api/jobs/<id>/ — état d'un job (polling frontend)."""

    def get(self, request, pk):
        try:
            job = Job.objects.select_related('project').get(pk=pk)
        except Job.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        permissions.check_read(job.project, request)
        return Response(JobSerializer(job).data)


class MediaView(APIView):
    """
    GET /media/<path> — photos et maillages, derrière la même authentification
    ET le même cloisonnement par projet que le reste de l'API (IsAuthenticated
    + KeycloakJWTAuthentication + api.permissions.check_read, cf.
    storage_backend.project_id_from_path). Proxie l'API storage (cf.
    api/storage_backend.py) : le fichier n'est jamais exposé directement,
    storage n'a aucun chemin de lecture anonyme.

    ⚠ N'est plus le chemin par défaut depuis le chantier « accès direct
    frontend → storage » (2026-07-30) : LabStorage.url() (api/storage_backend.py)
    renvoie désormais un chemin storage direct, plus '/media/<path>' — le
    frontend (ApiService.mediaUrl()) lit photos/maillages/glTF/STEP directement
    sur storage, avec son propre token. Cette vue reste en place (fallback
    authentifié, ex. usage futur hors navigateur), mais aucun serializer ne
    produit plus de chemin qui y mène — le cloisonnement par projet ci-dessous
    est donc défensif : sans lui, un chemin '/media/<path>?token=' devinable
    (id de projet séquentiel) contournerait totalement le passage en privé par
    défaut (Option A) en repassant par le compte de service, qui a accès à
    tout.
    """

    def get(self, request, path):
        project_id = storage_backend.project_id_from_path(path)
        if project_id is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        project = get_object_or_404(Project, pk=project_id)
        permissions.check_read(project, request)
        try:
            upstream = storage_client.download(path, namespace=storage_backend.namespace_for_path(path))
        except FileNotFoundError:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except storage_client.StorageClientError as exc:
            return Response({'detail': f'Stockage indisponible : {exc}'},
                            status=status.HTTP_502_BAD_GATEWAY)
        return StreamingHttpResponse(
            upstream.iter_content(chunk_size=65536),
            content_type=upstream.headers.get('Content-Type', 'application/octet-stream'),
        )


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

    Écrit en base (cache region_map/region_overlay) : traité comme une
    écriture (check_write), pas juste une lecture.
    """

    def get(self, request, pk):
        photo = get_object_or_404(Photo, pk=pk)
        permissions.check_write(photo.project, request)
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
        permissions.check_read(photo.project, request)
        return Response(PhotoLabelSerializer(photo.labels.all(), many=True).data)

    def post(self, request, photo_id):
        photo = get_object_or_404(Photo, pk=photo_id)
        permissions.check_write(photo.project, request)
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
        permissions.check_write(label.photo.project, request)
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
        project = permissions.get_visible_project(request, pk)
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
        project = permissions.get_editable_project(request, pk)
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
        mesh = get_object_or_404(Mesh, pk=self.kwargs['mesh_id'])
        permissions.check_read(mesh.project, self.request)
        return SemanticClass.objects.filter(mesh=mesh)


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 5 : Conception CAO manuelle (modélisation de pièces)
# ──────────────────────────────────────────────────────────────────────────────
class CadSketchListCreateView(APIView):
    """
    GET  /api/projects/<id>/cad-sketches/ — sketches d'un projet CAO.
    POST /api/projects/<id>/cad-sketches/ — crée un sketch (plan de référence
    + entités 2D, cf. cad_build.py pour le format attendu par type d'entité).
    """

    def get(self, request, pk):
        project = permissions.get_visible_project(request, pk)
        return Response(CadSketchSerializer(project.cad_sketches.all(), many=True).data)

    def post(self, request, pk):
        project = permissions.get_editable_project(request, pk)
        serializer = CadSketchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(project=project)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CadSketchDetailView(APIView):
    """PATCH/DELETE /api/cad-sketches/<id>/"""

    def patch(self, request, pk):
        sketch = get_object_or_404(CadSketch, pk=pk)
        permissions.check_write(sketch.project, request)
        serializer = CadSketchSerializer(sketch, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        sketch = get_object_or_404(CadSketch, pk=pk)
        permissions.check_write(sketch.project, request)
        sketch.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CadOperationListCreateView(APIView):
    """
    GET  /api/projects/<id>/cad-operations/ — historique d'opérations CAO,
    dans l'ordre.
    POST /api/projects/<id>/cad-operations/ — ajoute une opération à la fin
    de l'historique (`order` = max existant + 1 si non fourni).
    """

    def get(self, request, pk):
        project = permissions.get_visible_project(request, pk)
        return Response(CadOperationSerializer(project.cad_operations.all(), many=True).data)

    def post(self, request, pk):
        project = permissions.get_editable_project(request, pk)
        data = request.data.copy()
        if not data.get('order'):
            data['order'] = (project.cad_operations.aggregate(m=Max('order'))['m'] or 0) + 1
        serializer = CadOperationSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(project=project)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CadOperationDetailView(APIView):
    """PATCH/DELETE /api/cad-operations/<id>/"""

    def patch(self, request, pk):
        operation = get_object_or_404(CadOperation, pk=pk)
        permissions.check_write(operation.project, request)
        serializer = CadOperationSerializer(operation, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        operation = get_object_or_404(CadOperation, pk=pk)
        permissions.check_write(operation.project, request)
        operation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CadBuildLaunchView(APIView):
    """
    POST /api/projects/<id>/cad-build/ — lance le job CAD_BUILD (évalue
    l'historique CadOperation du projet en un nouveau Mesh, cf. cad_build.py
    et tasks.run_cad_build). Body optionnel : `linear_deflection`/
    `angular_deflection` (tessellation, défauts 0.1/0.3 — cf. to_do_3D.md,
    exposés comme paramètres utilisateur). Même verrou global que les autres
    jobs lourds : un seul actif à la fois, tous modules confondus.
    """

    def post(self, request, pk):
        project = permissions.get_editable_project(request, pk)
        if not project.cad_operations.exists():
            return Response({'detail': "Aucune opération CAO à évaluer pour ce projet."},
                             status=status.HTTP_400_BAD_REQUEST)

        params = {}
        for key in ('linear_deflection', 'angular_deflection'):
            if key in request.data:
                try:
                    params[key] = float(request.data[key])
                except (TypeError, ValueError):
                    return Response({'detail': f"'{key}' invalide."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            if Job.objects.select_for_update().filter(status__in=[Job.PENDING, Job.RUNNING]).exists():
                return Response(
                    {'detail': "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, "
                               "tous modules confondus. Réessayer une fois celui-ci terminé."},
                    status=status.HTTP_409_CONFLICT,
                )
            job = Job.objects.create(
                project=project, kind=Job.CAD_BUILD, status=Job.PENDING, params=params,
                owner_email=getattr(request.user, 'email', ''),
            )
            transaction.on_commit(lambda: run_cad_build.delay(job.id))

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class SubPartListCreateView(APIView):
    """
    GET  /api/projects/<id>/sub-parts/ — sous-parties CAO d'un projet (Lot
    5.1 : chacune garde son propre historique CadSketch/CadOperation et son
    propre Mesh, cf. cad_build.py — simplement rattachée à ce parent au lieu
    d'être un projet top-level, cf. décision du 2026-07-26).
    POST /api/projects/<id>/sub-parts/ — crée une nouvelle sous-partie
    (`project_type=OBJECT` par défaut, `parent_project` forcé au parent).
    Refuse (400) si `<id>` est déjà lui-même un sous-projet : une sous-partie
    ne peut pas avoir de sous-partie (cf. Project.root_project — l'accès et le
    partage storage supposent une profondeur d'un seul niveau, cf. rapport de
    chantier « accès direct storage » du 2026-07-30).
    """

    def get(self, request, pk):
        project = permissions.get_visible_project(request, pk)
        return Response(ProjectSerializer(project.sub_parts.all(), many=True).data)

    def post(self, request, pk):
        project = permissions.get_editable_project(request, pk)
        if project.parent_project_id is not None:
            return Response(
                {'detail': "Un sous-projet ne peut pas avoir lui-même de sous-partie."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = request.data.copy()
        data.pop('parent_project', None)
        serializer = ProjectSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(
            parent_project=project, project_type=data.get('project_type', Project.OBJECT),
            owner_email=getattr(request.user, 'email', ''),
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CadAssemblyInstanceListCreateView(APIView):
    """
    GET  /api/projects/<id>/cad-instances/ — instances placées dans un
    Project(ASSEMBLY).
    POST /api/projects/<id>/cad-instances/ — ajoute une instance (référence
    une sous-partie + son Mesh le plus récent par défaut si non précisé).
    """

    def get(self, request, pk):
        project = permissions.get_visible_project(request, pk)
        return Response(CadAssemblyInstanceSerializer(project.cad_instances.all(), many=True).data)

    def post(self, request, pk):
        project = permissions.get_editable_project(request, pk)
        data = request.data.copy()
        source_project = get_object_or_404(Project, pk=data.get('source_project'), parent_project=project)
        if not data.get('source_mesh'):
            latest = source_project.meshes.order_by('-version').first()
            if latest is None:
                return Response(
                    {'detail': f"« {source_project.name} » n'a encore aucun Mesh (CAD_BUILD requis avant assemblage)."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            data['source_mesh'] = latest.id
        serializer = CadAssemblyInstanceSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(assembly_project=project)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CadAssemblyInstanceDetailView(APIView):
    """
    DELETE /api/cad-instances/<id>/
    PATCH  /api/cad-instances/<id>/ — re-pointage EXPLICITE vers un autre
    `Mesh` du même `source_project` (typiquement sa dernière version, cf.
    to_do_3D.md limite topologique n°2). Body : {'source_mesh': <id>}. C'est
    cette action précise — pas une invalidation spontanée — qui doit avertir :
    les `CadAssemblyConstraint` déjà posées sur cette instance référencent des
    faces/arêtes par index du STEP précédent, qui peuvent ne plus désigner la
    même géométrie une fois la sous-partie régénérée. Réinitialise `placement`
    (une nouvelle résolution est nécessaire) et fait repasser l'assemblage en
    DRAFT.
    """

    def delete(self, request, pk):
        instance = get_object_or_404(CadAssemblyInstance, pk=pk)
        permissions.check_write(instance.assembly_project, request)
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def patch(self, request, pk):
        instance = get_object_or_404(CadAssemblyInstance, pk=pk)
        permissions.check_write(instance.assembly_project, request)
        new_mesh_id = request.data.get('source_mesh')
        if not new_mesh_id:
            return Response({'detail': "'source_mesh' requis."}, status=status.HTTP_400_BAD_REQUEST)
        new_mesh = get_object_or_404(Mesh, pk=new_mesh_id, project=instance.source_project)

        has_constraints = instance.constraints_as_a.exists() or instance.constraints_as_b.exists()
        instance.source_mesh = new_mesh
        instance.placement = None
        instance.save(update_fields=['source_mesh', 'placement'])
        if instance.assembly_project.assembly_status != Project.DRAFT:
            instance.assembly_project.assembly_status = Project.DRAFT
            instance.assembly_project.save(update_fields=['assembly_status'])

        data = CadAssemblyInstanceSerializer(instance).data
        if has_constraints:
            data['warning'] = (
                "Cette instance est référencée par au moins une contrainte — ses références de "
                "face/arête (posées sur l'ancienne version du Mesh) peuvent ne plus désigner la même "
                "géométrie après régénération. Vérifiez-les avant de relancer la résolution."
            )
        return Response(data)


class CadAssemblyConstraintListCreateView(APIView):
    """
    GET  /api/projects/<id>/cad-constraints/ — contraintes posées dans un
    Project(ASSEMBLY).
    POST /api/projects/<id>/cad-constraints/ — pose une contrainte entre deux
    CadAssemblyInstance (une seule si FIXED, cf. modèle).
    """

    def get(self, request, pk):
        project = permissions.get_visible_project(request, pk)
        return Response(CadAssemblyConstraintSerializer(project.cad_constraints.all(), many=True).data)

    def post(self, request, pk):
        project = permissions.get_editable_project(request, pk)
        serializer = CadAssemblyConstraintSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(assembly_project=project)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CadAssemblyConstraintDetailView(APIView):
    """DELETE /api/cad-constraints/<id>/"""

    def delete(self, request, pk):
        constraint = get_object_or_404(CadAssemblyConstraint, pk=pk)
        permissions.check_write(constraint.assembly_project, request)
        constraint.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CadAssembleLaunchView(APIView):
    """
    POST /api/projects/<id>/cad-assemble/ — lance le job CAD_ASSEMBLE (résout
    les CadAssemblyConstraint du Project(ASSEMBLY) via FreeCAD headless, cf.
    cad_assemble.py et tasks.run_cad_assemble). Même verrou global que les
    autres jobs lourds.

    Avant résolution (cf. to_do_3D.md, limite topologique n°2, Lot 5.2) :
    vérifie qu'aucune CadAssemblyInstance ne pointe un Mesh plus ancien que la
    dernière version disponible de son source_project — une sous-partie
    reconstruite après l'ajout de son instance laisserait sinon l'assemblage se
    résoudre silencieusement sur une géométrie périmée. Bloque (409) tant que
    le body ne porte pas `confirm_outdated: true` explicite.
    """

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk, project_type=Project.ASSEMBLY)
        permissions.check_write(project, request)
        if not project.cad_constraints.exists():
            return Response({'detail': "Aucune contrainte posée pour cet assemblage."},
                             status=status.HTTP_400_BAD_REQUEST)

        confirm_outdated = str(request.data.get('confirm_outdated', '')).lower() in ('1', 'true', 'yes')
        if not confirm_outdated:
            outdated = []
            for instance in project.cad_instances.select_related('source_project', 'source_mesh'):
                latest = instance.source_project.meshes.order_by('-version').first()
                if latest is not None and latest.version > instance.source_mesh.version:
                    outdated.append({
                        'instance_id': instance.id,
                        'label': instance.label or instance.source_project.name,
                        'source_project': instance.source_project_id,
                        'pinned_mesh_version': instance.source_mesh.version,
                        'latest_mesh_version': latest.version,
                    })
            if outdated:
                return Response(
                    {
                        'detail': (
                            f"{len(outdated)} instance(s) pointent vers une version de Mesh plus "
                            "ancienne que la dernière disponible pour leur sous-partie — les "
                            "contraintes posées dessus peuvent ne plus désigner les mêmes faces/arêtes "
                            "après régénération. Re-pointez chaque instance (PATCH /api/cad-instances/"
                            "<id>/) ou renvoyez confirm_outdated=true pour résoudre quand même."
                        ),
                        'outdated_instances': outdated,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        with transaction.atomic():
            if Job.objects.select_for_update().filter(status__in=[Job.PENDING, Job.RUNNING]).exists():
                return Response(
                    {'detail': "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, "
                               "tous modules confondus. Réessayer une fois celui-ci terminé."},
                    status=status.HTTP_409_CONFLICT,
                )
            job = Job.objects.create(
                project=project, kind=Job.CAD_ASSEMBLE, status=Job.PENDING,
                owner_email=getattr(request.user, 'email', ''),
            )
            transaction.on_commit(lambda: run_cad_assemble.delay(job.id))

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 5.3 : API publique (lecture seule, sans authentification)
# ──────────────────────────────────────────────────────────────────────────────
# Exception actée dans to_do_3D.md (section « Sécurité / cloisonnement ») : ces
# routes précises contournent DÉLIBÉRÉMENT les deux verrous du lab (pas de flow
# Keycloak à poser côté client, pas de contrôle azp/groups ici) — même pattern
# que public_plat_photo (restauration/backend/api/views_public.py) et
# l'endpoint image AllowAny de google-agenda. Strictement limitées aux
# `Project(project_type=ASSEMBLY, assembly_status=SOLVED)` : un Project CAO
# mono-pièce ou un assemblage encore DRAFT/ERROR n'y est jamais exposé.
# Aucune autre route de cette app n'est concernée.
class PublicReadThrottle(AnonRateThrottle):
    scope = 'public'


def _public_assembly_queryset():
    return Project.objects.filter(
        project_type=Project.ASSEMBLY, assembly_status=Project.SOLVED, parent_project__isnull=True,
    )


class PublicAssemblyListView(generics.ListAPIView):
    """GET /api/public/assemblies/ — liste des assemblages résolus, publique."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicReadThrottle]
    serializer_class = PublicAssemblySerializer
    queryset = _public_assembly_queryset()


class PublicAssemblyDetailView(generics.RetrieveAPIView):
    """GET /api/public/assemblies/<id>/ — détail d'un assemblage résolu, public."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicReadThrottle]
    serializer_class = PublicAssemblySerializer
    queryset = _public_assembly_queryset()


class PublicAssemblyMeshFileView(APIView):
    """
    GET /api/public/assemblies/<id>/gltf/ — glTF du Mesh le plus récent (visualisation).
    GET /api/public/assemblies/<id>/step/ — STEP du Mesh le plus récent (réutilisation exacte).

    Proxie l'API storage via le compte de service de cette app (même transport
    que MediaView, cf. plus haut) : storage n'a lui-même aucun chemin de
    lecture anonyme, c'est cette vue qui décide délibérément de republier ces
    octets à tout visiteur, une fois le filtre ASSEMBLY/SOLVED déjà passé par
    _public_assembly_queryset().
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicReadThrottle]

    def get(self, request, pk, fmt):
        if fmt not in ('gltf', 'step'):
            return Response(status=status.HTTP_404_NOT_FOUND)
        project = get_object_or_404(_public_assembly_queryset(), pk=pk)
        mesh = project.meshes.order_by('-version').first()
        if mesh is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        field = mesh.gltf_file if fmt == 'gltf' else mesh.step_file
        if not field:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            upstream = storage_client.download(field.name, namespace=storage_backend.namespace_for_path(field.name))
        except FileNotFoundError:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except storage_client.StorageClientError as exc:
            return Response({'detail': f'Stockage indisponible : {exc}'}, status=status.HTTP_502_BAD_GATEWAY)
        return StreamingHttpResponse(
            upstream.iter_content(chunk_size=65536),
            content_type=upstream.headers.get('Content-Type', 'application/octet-stream'),
        )
