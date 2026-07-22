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

    @property
    def has_mesh(self) -> bool:
        return self.meshes.exists()

    @property
    def has_resolved_poses(self) -> bool:
        # Module Bâtiments (Lot 4) : réutilise obligatoirement les poses caméra
        # du Lot 1 (Photo.camera_pose) — un projet sans reconstruction préalable
        # (ou dont le SfM n'a enregistré aucune image) ne peut pas le lancer.
        return self.photos.filter(camera_pose__isnull=False).exists()


def photo_upload_path(instance, filename):
    return f'projects/{instance.project_id}/photos/{filename}'


def region_map_upload_path(instance, filename):
    return f'projects/{instance.project_id}/photos/regions/{filename}'


class Photo(models.Model):
    """Une photo source d'un projet (déposée directement ou extraite d'une vidéo)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='photos')
    file = models.ImageField(upload_to=photo_upload_path, max_length=500)
    order = models.PositiveIntegerField(default=0)
    # Pose caméra résolue par le SfM (position/orientation, PLUS les intrinsèques
    # de la caméra COLMAP — modèle, dimensions, paramètres — depuis Lot 4 : cf.
    # tasks._record_camera_poses) — nulle tant que la reconstruction n'a pas
    # tourné, ou si cette image précise n'a pas pu être enregistrée par le SfM
    # (poses "échouées", cf. page résultat).
    camera_pose = models.JSONField(null=True, blank=True)
    # ── Lot 4 (Bâtiments) : cache de la segmentation 2D zero-shot (FastSAM) ──
    # Calculée à la demande (labellisation assistée ou job SEGMENTATION_FACADE),
    # jamais au moment de l'upload (cf. to_do_3D.md : aucun job auto au dépôt).
    # region_map : tableau numpy (.npz, un indice de région par pixel, -1 = fond)
    # utilisé côté serveur pour résoudre un clic en région. region_overlay :
    # PNG coloré par région, pour l'affichage frontend uniquement.
    region_map = models.FileField(upload_to=region_map_upload_path, max_length=500, null=True, blank=True)
    region_overlay = models.FileField(upload_to=region_map_upload_path, max_length=500, null=True, blank=True)
    region_count = models.PositiveIntegerField(null=True, blank=True)
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
    # Renseignés par le job REPAIR (Lot 2, module Impression) — restent
    # False/null pour un maillage issu directement de la reconstruction (Lot 1),
    # jamais réparé.
    is_watertight = models.BooleanField(default=False)
    repair_report = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'meshes'
        ordering = ['project', '-version']


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 3 : Mouvements (parties + jointures)
# ──────────────────────────────────────────────────────────────────────────────
class Part(models.Model):
    """
    Une partie découpée dans un `Mesh` — un sous-ensemble de faces, désigné
    manuellement (peinture au pinceau 3D, l'outil principal, cf. to_do_3D.md)
    ou proposé par la segmentation RANSAC globale (`suggested=True`), que
    l'utilisateur garde telle quelle, ajuste (repeint par-dessus) ou supprime.
    """

    PLANE = 'plane'
    CYLINDER = 'cylinder'
    SPHERE = 'sphere'
    PRIMITIVE_CHOICES = [(PLANE, 'Plan'), (CYLINDER, 'Cylindre'), (SPHERE, 'Sphère')]

    mesh = models.ForeignKey(Mesh, on_delete=models.CASCADE, related_name='parts')
    name = models.CharField(max_length=100)
    # Indices de faces dans le maillage (mêmes indices que le tableau de faces
    # du PLY/glTF — vérifié : trimesh préserve l'ordre des faces à l'export GLB,
    # donc `faceIndex` d'un raycast three.js correspond exactement à ces indices).
    face_ids = models.JSONField(default=list)
    color = models.CharField(max_length=7, default='#4a90d9')
    # True si créée par la segmentation RANSAC globale plutôt que peinte à la
    # main — purement informatif (badge dans l'UI), n'affecte aucun comportement.
    suggested = models.BooleanField(default=False)
    primitive_type = models.CharField(max_length=10, choices=PRIMITIVE_CHOICES, blank=True)
    primitive_params = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'parts'
        ordering = ['mesh', 'name']

    def __str__(self) -> str:
        return self.name

    @property
    def face_count(self) -> int:
        return len(self.face_ids)


class Joint(models.Model):
    """
    Une jointure reliant deux `Part` du même maillage — `parent_part` est le
    référentiel fixe, `child_part` la partie mobile (arbre cinématique : un
    slider par jointure fait tourner/glisser `child_part` — et tout ce qui en
    dépend plus bas dans l'arbre — autour de l'axe, cf. to_do_3D.md).
    """

    REVOLUTE = 'revolute'
    PRISMATIC = 'prismatic'
    FIXED = 'fixed'
    TYPE_CHOICES = [(REVOLUTE, 'Pivot'), (PRISMATIC, 'Glissière'), (FIXED, 'Fixe')]

    parent_part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name='joints_as_parent')
    child_part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name='joints_as_child')
    joint_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    axis_origin = models.JSONField(default=list)
    axis_direction = models.JSONField(default=list)
    # Angle en degrés (pivot) ou distance en unités de maillage (glissière) —
    # nuls pour une jointure fixe, ou pour une jointure sans limite définie.
    limit_min = models.FloatField(null=True, blank=True)
    limit_max = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'joints'
        ordering = ['id']


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 4 : Bâtiments (segmentation sémantique murs/fenêtres/portes/toit)
# ──────────────────────────────────────────────────────────────────────────────
# Vocabulaire fixe (cf. to_do_3D.md — pas de classes personnalisées par
# l'utilisateur) : clé ASCII interne / libellé accentué affiché / couleur par
# défaut. Vit dans facade.py (logique métier) ; répété ici en commentaire pour
# lisibilité du modèle. Voir facade.CLASS_NAMES/CLASS_LABELS/CLASS_COLORS.
class PhotoLabel(models.Model):
    """
    Une région 2D (segmentation zero-shot FastSAM, cf. Photo.region_map)
    labellisée manuellement par l'utilisateur — l'INPUT du job
    SEGMENTATION_FACADE, pas son résultat (qui vit dans SemanticClass sur le
    nouveau `Mesh` produit). Posée avant le lancement du job, donc pas de FK
    vers SemanticClass (qui n'existe pas encore) — juste le nom de classe.
    """

    MUR = 'mur'
    FENETRE = 'fenetre'
    PORTE = 'porte'
    TOIT = 'toit'
    CLASS_CHOICES = [(MUR, 'Mur'), (FENETRE, 'Fenêtre'), (PORTE, 'Porte'), (TOIT, 'Toit')]

    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name='labels')
    semantic_class = models.CharField(max_length=10, choices=CLASS_CHOICES)
    # Indice de région dans Photo.region_map (pas un identifiant stable hors de
    # ce cache — recalculé si la photo est re-segmentée).
    region_index = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'photo_labels'
        unique_together = [('photo', 'region_index')]
        ordering = ['photo', 'region_index']


class SemanticClass(models.Model):
    """
    Une classe sémantique du résultat de segmentation d'un `Mesh` (module
    Bâtiments) — regroupe les faces qui lui appartiennent, même pattern que
    `Part` (Lot 3) : `face_ids` plutôt qu'un attribut par face du maillage
    (évite de réécrire le PLY/glTF à chaque édition manuelle future).
    """

    mesh = models.ForeignKey(Mesh, on_delete=models.CASCADE, related_name='semantic_classes')
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=7, default='#888888')
    face_ids = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'semantic_classes'
        ordering = ['mesh', 'name']

    def __str__(self) -> str:
        return self.name

    @property
    def face_count(self) -> int:
        return len(self.face_ids)
