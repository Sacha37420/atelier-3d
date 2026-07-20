from django.db import models


class Department(models.Model):
    """Département ou équipe de l'organisation."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        db_table = 'departments'
        ordering = ['name']

    def __str__(self) -> str:
        return self.name


class UserRecord(models.Model):
    """Enregistrement d'un utilisateur Keycloak, créé automatiquement à la première connexion."""

    email = models.EmailField(primary_key=True, max_length=255)
    display_name = models.CharField(max_length=200, blank=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='members',
    )
    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_records'
        ordering = ['email']

    def __str__(self) -> str:
        return self.display_name or self.email


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 1 : Reconstruction (photos/vidéo → objet 3D)
# ──────────────────────────────────────────────────────────────────────────────
class Project(models.Model):
    """
    Un projet de reconstruction 3D : un ensemble de photos et les maillages
    successifs produits à partir d'elles (reconstruction, réparation, segmentation).
    """

    OBJECT = 'objet'
    BUILDING = 'batiment'
    TYPE_CHOICES = [(OBJECT, 'Objet'), (BUILDING, 'Bâtiment')]

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    project_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=OBJECT)
    # Mètres par unité du maillage. Null tant qu'aucune calibration n'a été
    # renseignée (cf. Photo.calibration_points / calibration_ref_size ci-dessous) :
    # le maillage sort alors à une échelle arbitraire (point bloquant pour le
    # futur module Impression, cf. to_do_3D.md Lot 2).
    scale_meters_per_unit = models.FloatField(null=True, blank=True)
    owner_email = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'projects'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return self.name

    @property
    def has_scale(self) -> bool:
        return self.scale_meters_per_unit is not None

    @property
    def has_active_job(self) -> bool:
        return self.jobs.filter(status__in=[Job.PENDING, Job.RUNNING]).exists()


def photo_upload_path(instance, filename):
    return f'projects/{instance.project_id}/photos/{filename}'


class Photo(models.Model):
    """Une photo source d'un projet (déposée directement ou extraite d'une vidéo)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='photos')
    file = models.ImageField(upload_to=photo_upload_path, max_length=500)
    order = models.PositiveIntegerField(default=0)
    # Pose caméra résolue par le SfM (position/orientation/intrinsèques COLMAP) —
    # nulle tant que la reconstruction n'a pas tourné, ou si cette image précise
    # n'a pas pu être enregistrée par le SfM (poses "échouées", cf. page résultat).
    camera_pose = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'photos'
        ordering = ['order', 'created_at']

    @property
    def pose_resolved(self) -> bool:
        return self.camera_pose is not None


class Job(models.Model):
    """
    Suivi d'une tâche asynchrone lourde (reconstruction, réparation, segmentation),
    exposé au frontend pour affichage du statut/progress (polling).
    """

    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    DONE = 'DONE'
    ERROR = 'ERROR'
    STATUS_CHOICES = [(PENDING, 'En attente'), (RUNNING, 'En cours'),
                       (DONE, 'Terminé'), (ERROR, 'Erreur')]

    # Un kind par étape lourde du cahier des charges (to_do_3D.md) — seul
    # RECONSTRUCTION est implémenté au Lot 1, les autres sont réservés aux
    # lots suivants (Impression, Mouvements, Bâtiments).
    RECONSTRUCTION = 'RECONSTRUCTION'
    REPAIR = 'REPAIR'
    SEGMENTATION_PARTS = 'SEGMENTATION_PARTS'
    SEGMENTATION_FACADE = 'SEGMENTATION_FACADE'
    KIND_CHOICES = [
        (RECONSTRUCTION, 'Reconstruction'),
        (REPAIR, 'Réparation impression'),
        (SEGMENTATION_PARTS, 'Segmentation parties/jointures'),
        (SEGMENTATION_FACADE, 'Segmentation bâtiment'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='jobs')
    kind = models.CharField(max_length=40, choices=KIND_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    progress = models.IntegerField(default=0)  # 0..100
    message = models.TextField(blank=True)
    params = models.JSONField(default=dict, blank=True)
    celery_task_id = models.CharField(max_length=64, blank=True)
    owner_email = models.CharField(max_length=255, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'jobs'
        ordering = ['-created_at']

    def set_state(self, status=None, progress=None, message=None):
        """Met à jour l'état et persiste (appelé par la tâche Celery)."""
        if status is not None:
            self.status = status
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
        self.save(update_fields=['status', 'progress', 'message', 'updated_at'])


def mesh_upload_path(instance, filename):
    return f'projects/{instance.project_id}/meshes/{filename}'


class Mesh(models.Model):
    """
    Un maillage produit par un job (reconstruction / réparation / segmentation).
    Chaque étape crée une nouvelle version plutôt que d'écraser la précédente
    (cf. to_do_3D.md — modèle de données).
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='meshes')
    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name='meshes')
    file = models.FileField(upload_to=mesh_upload_path, max_length=500)
    # glTF généré à partir du PLY pivot pour le viewer three.js (cf. to_do_3D.md —
    # « Format pivot interne »). Null si l'export a échoué mais que le PLY est bon.
    gltf_file = models.FileField(upload_to=mesh_upload_path, max_length=500, null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    vertex_count = models.PositiveIntegerField(null=True, blank=True)
    face_count = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'meshes'
        ordering = ['project', '-version']
