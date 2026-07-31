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
    ASSEMBLY = 'assembly'
    TYPE_CHOICES = [(OBJECT, 'Objet'), (BUILDING, 'Bâtiment'), (ASSEMBLY, 'Assemblage')]

    # ── Lot 5 (Conception CAO) : état de résolution d'un Project(ASSEMBLY) ──
    # Distinct du status d'un Job individuel (même logique que Mesh.is_watertight) :
    # persiste l'état courant de l'assemblage entre deux résolutions.
    DRAFT = 'DRAFT'
    SOLVED = 'SOLVED'
    ASSEMBLY_ERROR = 'ERROR'
    ASSEMBLY_STATUS_CHOICES = [
        (DRAFT, 'Brouillon'), (SOLVED, 'Résolu'), (ASSEMBLY_ERROR, 'Erreur'),
    ]

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    project_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=OBJECT)
    # Non nul seulement si project_type == ASSEMBLY (cf. to_do_3D.md, Lot 5.2).
    assembly_status = models.CharField(
        max_length=10, choices=ASSEMBLY_STATUS_CHOICES, null=True, blank=True,
    )
    # Non nul quand ce `Project` est une sous-partie CAO d'un `Project(ASSEMBLY)` —
    # garde son propre historique CadSketch/CadOperation et son propre Mesh (Lot
    # 5.1, inchangé), simplement rattachée à un parent pour ne plus apparaître comme
    # une entrée top-level dans la liste des projets (cf. décision utilisateur du
    # 2026-07-26 : les pièces de Lot 5.1 sont des sous-parties d'un Project, pas des
    # projets indépendants — voir CadAssemblyInstance.source_project qui référence
    # ce même sous-projet pour le placement dans l'assemblage).
    parent_project = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='sub_parts',
    )
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

    @property
    def root_project(self) -> 'Project':
        """
        Le projet top-level dont dépend TOUT le contrôle d'accès de ce projet
        (lui-même s'il est déjà top-level) — cf. api/permissions.py et
        api/storage_backend.py.

        Chantier « accès direct storage » (2026-07-30, Option A) : un
        sous-projet CAO (Lot 5.2, `parent_project` non nul) n'a JAMAIS son
        propre `ProjectShare` ni son propre partage storage — il hérite
        ENTIÈREMENT de son projet racine (relation de COMPOSITION, pas un
        rangement façon dossier : les `CadAssemblyInstance` d'un assemblage ne
        référencent que des sous-parties du MÊME assemblage — jamais réutilisées
        ailleurs, cf. `CadAssemblyInstanceListCreateView.post`). Son propre
        `owner_email` (l'auteur de la création, pas forcément le propriétaire du
        projet racine — cf. `SubPartListCreateView.post`) n'est donc jamais
        consulté pour l'autorisation.

        Une sous-partie ne peut elle-même avoir de sous-partie (refus explicite
        dans `SubPartListCreateView.post`), donc la remontée s'arrête en
        pratique après un seul niveau — gère une profondeur plus grande par
        prudence (garde-fou anti-cycle), sans jamais la supposer.
        """
        node = self
        seen = {node.pk}
        while node.parent_project_id is not None:
            parent = Project.objects.only('id', 'owner_email', 'parent_project_id').get(
                pk=node.parent_project_id,
            )
            if parent.pk in seen:
                break  # garde-fou anti-cycle : ne devrait jamais se produire
            node = parent
            seen.add(node.pk)
        return node


class ProjectShare(models.Model):
    """
    Accès donné à quelqu'un d'autre sur un projet TOP-LEVEL, désigné par son
    e-mail — même pattern que `TreeShare` (arbre-genealogique/backend/api/models.py) :
    l'invitation ne suppose pas que la personne se soit déjà connectée, l'e-mail
    est le pivot d'identité de l'app (cf. UserRecord.email, alimenté depuis le
    JWT).

    Le propriétaire du projet n'apparaît pas ici : sa qualité vient de
    `Project.owner_email`, et lui seul gère les partages et peut supprimer le
    projet (cf. api/permissions.py, `check_owner`).

    Réservé aux projets top-level (`parent_project` nul) — voir
    `Project.root_project` et api/permissions.py. Un sous-projet CAO (Lot 5.2)
    hérite ENTIÈREMENT du partage de son projet racine, jamais de
    `ProjectShare` indépendant (cf. rapport de chantier « accès direct
    storage » du 2026-07-30, décision utilisateur Option A). `ProjectShareViewSet`
    refuse la création d'un `ProjectShare` sur un projet dont `parent_project`
    n'est pas nul.
    """

    class Role(models.TextChoices):
        VIEWER = 'VIEWER', 'Lecture'
        EDITOR = 'EDITOR', 'Édition'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='shares')
    email = models.EmailField(max_length=255, db_index=True)
    role = models.CharField(max_length=6, choices=Role.choices, default=Role.VIEWER)
    invited_by = models.EmailField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'project_shares'
        ordering = ['email']
        constraints = [
            models.UniqueConstraint(fields=['project', 'email'], name='uniq_project_share_per_project'),
        ]

    def __str__(self) -> str:
        return f'{self.email} @ project#{self.project_id}'


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
    CAD_BUILD = 'CAD_BUILD'
    CAD_ASSEMBLE = 'CAD_ASSEMBLE'
    # Mode d'initialisation « Photos client » : conversion d'un maillage déjà
    # reconstruit par le client avec son propre logiciel de photogrammétrie
    # (Meshroom/RealityScan/3DF Zephyr…) vers le format pivot, cf.
    # MeshImportLaunchView/tasks.run_mesh_import. Les photos sources, elles,
    # passent par le même endpoint que le mode Photo (PhotoUploadView) — rien
    # de spécifique à créer pour elles.
    MESH_IMPORT = 'MESH_IMPORT'
    KIND_CHOICES = [
        (RECONSTRUCTION, 'Reconstruction'),
        (REPAIR, 'Réparation impression'),
        (SEGMENTATION_PARTS, 'Segmentation parties/jointures'),
        (SEGMENTATION_FACADE, 'Segmentation bâtiment'),
        (CAD_BUILD, 'Construction CAO'),
        (CAD_ASSEMBLE, 'Assemblage CAO'),
        (MESH_IMPORT, 'Import maillage client'),
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
    # ── Lot 5 (Conception CAO) : B-rep exact, produit uniquement par Job(CAD_BUILD)
    # /Job(CAD_ASSEMBLE) — nul pour un Mesh issu de la photogrammétrie (Lots 1-4),
    # qui n'a pas de représentation B-rep. linear/angular_deflection sont les
    # paramètres de tessellation OCCT (BRepMesh_IncrementalMesh) utilisés pour
    # produire gltf_file à partir de step_file — cf. to_do_3D.md, spike Lot 5.0.
    step_file = models.FileField(upload_to=mesh_upload_path, max_length=500, null=True, blank=True)
    linear_deflection = models.FloatField(null=True, blank=True)
    angular_deflection = models.FloatField(null=True, blank=True)
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


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 5 : Conception CAO manuelle et Assemblage
# ──────────────────────────────────────────────────────────────────────────────
# Deuxième façon de peupler Project/Mesh, en parallèle de la Reconstruction
# (Lot 1) — pas de tables CadPart/CadMesh isolées (cf. to_do_3D.md, principe
# d'intégration). Préfixe `Cad*` pour éviter toute collision avec Part/Joint
# (Lot 3), qui désignent un concept différent (sous-ensemble de faces d'un Mesh
# triangulé, pas une pièce paramétrique).
class CadSketch(models.Model):
    """
    Un sketch 2D posé sur un plan de référence d'un `Project` CAO — segments,
    polylignes/polygones fermés, cercles, arcs, courbes B-spline échantillonnées
    (+ épaisseur pour les courbes non fermées, cf. to_do_3D.md). Consommé par les
    `CadOperation` EXTRUDE/REVOLVE du même `Project`. V1 = formulaires numériques,
    pas de canvas interactif (cf. to_do_3D.md — Hors périmètre).
    """

    XY = 'XY'
    XZ = 'XZ'
    YZ = 'YZ'
    PLANE_CHOICES = [(XY, 'XY'), (XZ, 'XZ'), (YZ, 'YZ')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='cad_sketches')
    name = models.CharField(max_length=100)
    plane = models.CharField(max_length=2, choices=PLANE_CHOICES, default=XY)
    # Liste de dicts {type: segment|polyline|circle|arc|bspline, ...} — cf.
    # cad_build.py pour le détail des champs attendus par type d'entité.
    entities = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'cad_sketches'
        ordering = ['project', 'name']

    def __str__(self) -> str:
        return self.name


class CadOperation(models.Model):
    """
    Une étape de l'historique de modélisation CAO d'un `Project` — évaluée dans
    l'ordre (`order`) par `Job(CAD_BUILD)` pour produire un nouveau `Mesh` (cf.
    cad_build.py). Référence une face/arête d'une opération antérieure par index :
    réévaluée en entier à chaque édition, pas de cache incrémental (limite
    topologique assumée, cf. to_do_3D.md — avertir si le nombre de faces change
    après réévaluation plutôt que d'appliquer aveuglément les opérations suivantes
    sur de mauvais indices).
    """

    PRIMITIVE_BOX = 'PRIMITIVE_BOX'
    PRIMITIVE_SPHERE = 'PRIMITIVE_SPHERE'
    PRIMITIVE_CYLINDER = 'PRIMITIVE_CYLINDER'
    PRIMITIVE_CONE = 'PRIMITIVE_CONE'
    PRIMITIVE_TORUS = 'PRIMITIVE_TORUS'
    EXTRUDE = 'EXTRUDE'
    REVOLVE = 'REVOLVE'
    SURFACE_FROM_WIRE = 'SURFACE_FROM_WIRE'
    BOOLEAN_UNION = 'BOOLEAN_UNION'
    BOOLEAN_CUT = 'BOOLEAN_CUT'
    BOOLEAN_INTERSECT = 'BOOLEAN_INTERSECT'
    PATTERN_CIRCULAR = 'PATTERN_CIRCULAR'
    PATTERN_LINEAR = 'PATTERN_LINEAR'
    GEAR_TEETH = 'GEAR_TEETH'
    TYPE_CHOICES = [
        (PRIMITIVE_BOX, 'Primitive : boîte'),
        (PRIMITIVE_SPHERE, 'Primitive : sphère'),
        (PRIMITIVE_CYLINDER, 'Primitive : cylindre'),
        (PRIMITIVE_CONE, 'Primitive : cône'),
        (PRIMITIVE_TORUS, 'Primitive : tore'),
        (EXTRUDE, 'Extrusion'),
        (REVOLVE, 'Révolution'),
        (SURFACE_FROM_WIRE, 'Surface depuis un contour'),
        (BOOLEAN_UNION, 'Booléen : union'),
        (BOOLEAN_CUT, 'Booléen : différence'),
        (BOOLEAN_INTERSECT, 'Booléen : intersection'),
        (PATTERN_CIRCULAR, 'Répétition circulaire'),
        (PATTERN_LINEAR, 'Répétition linéaire'),
        (GEAR_TEETH, 'Denture (engrenage)'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='cad_operations')
    order = models.PositiveIntegerField(default=0)
    operation_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    # Contenu par type, cf. cad_build.py pour le détail exact des clés attendues :
    #  PRIMITIVE_* : dimensions + placement
    #  EXTRUDE/REVOLVE : sketch_id (CadSketch), mode ADD|CUT|INTERSECT, distance/
    #                    angle/axe
    #  BOOLEAN_* : source_a/source_b (id de CadOperation antérieures)
    #  PATTERN_* : source (id), count, step/angle
    #  GEAR_TEETH : source (id), face_index, module, teeth_count, pressure_angle,
    #               width, internal (bool)
    params = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'cad_operations'
        ordering = ['project', 'order']

    def __str__(self) -> str:
        return f'{self.project_id}#{self.order} {self.operation_type}'


class CadAssemblyInstance(models.Model):
    """
    Une pièce placée dans un `Project(project_type=ASSEMBLY)` — référence un
    `Project` CAO source ET la version précise de son `Mesh` (pin, cf. to_do_3D.md
    — limite topologique n°2 : un re-pointage explicite vers un `Mesh` plus récent
    doit avertir que les références des `CadAssemblyConstraint` peuvent ne plus
    désigner les mêmes faces, pas une invalidation spontanée). `placement` reste
    nul tant que `Job(CAD_ASSEMBLE)` n'a pas résolu l'assemblage.
    """

    assembly_project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='cad_instances')
    source_project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='cad_instances_used_in')
    # PROTECT : un Mesh pointé par une instance d'assemblage ne doit pas pouvoir
    # disparaître silencieusement — cf. limite topologique n°2 ci-dessus.
    source_mesh = models.ForeignKey(Mesh, on_delete=models.PROTECT, related_name='cad_instances')
    label = models.CharField(max_length=100, blank=True)
    # Translation + rotation résolues par le solveur (JSON : {position: [x,y,z],
    # quaternion_xyzw: [x,y,z,w]}) — nul avant résolution.
    placement = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'cad_assembly_instances'
        ordering = ['assembly_project', 'id']

    def __str__(self) -> str:
        return self.label or f'{self.source_project.name} #{self.pk}'


class CadAssemblyConstraint(models.Model):
    """
    Une contrainte de positionnement entre deux `CadAssemblyInstance` (une seule
    si FIXED, ancrée au monde) d'un `Project(ASSEMBLY)` — résolue par
    `Job(CAD_ASSEMBLE)` via FreeCAD headless (cf. cad_assemble.py). Positionnement
    statique uniquement : le mouvement reste du ressort du Lot 3 (Part/Joint),
    posé après résolution sur le `Mesh` global résultant — aucun changement côté
    Lot 3 (cf. to_do_3D.md).
    """

    COINCIDENT = 'COINCIDENT'
    CONTACT = 'CONTACT'
    PARALLEL = 'PARALLEL'
    CONCENTRIC = 'CONCENTRIC'
    DISTANCE = 'DISTANCE'
    ANGLE = 'ANGLE'
    FIXED = 'FIXED'
    GEAR_MESH = 'GEAR_MESH'
    TYPE_CHOICES = [
        (COINCIDENT, 'Coïncidence'),
        (CONTACT, 'Contact / coplanaire'),
        (PARALLEL, 'Parallélisme'),
        (CONCENTRIC, 'Concentricité'),
        (DISTANCE, 'Distance'),
        (ANGLE, 'Angle'),
        (FIXED, 'Fixation au monde'),
        (GEAR_MESH, 'Engrènement'),
    ]

    assembly_project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='cad_constraints')
    constraint_type = models.CharField(max_length=12, choices=TYPE_CHOICES)
    instance_a = models.ForeignKey(
        CadAssemblyInstance, on_delete=models.CASCADE, related_name='constraints_as_a',
    )
    # {kind: face|edge|vertex, index}
    reference_a = models.JSONField()
    # Nul seulement si constraint_type == FIXED (ancrée au monde, pas de 2e pièce).
    instance_b = models.ForeignKey(
        CadAssemblyInstance, on_delete=models.CASCADE, related_name='constraints_as_b',
        null=True, blank=True,
    )
    reference_b = models.JSONField(null=True, blank=True)
    # DISTANCE: {'distance_mm': ...} ; ANGLE: {'angle_deg': ...} ; GEAR_MESH:
    # dérivés automatiquement par cad_assemble.py à partir du module/nombre de
    # dents des CadOperation(GEAR_TEETH) de chaque pièce — jamais saisis à la
    # main (cf. to_do_3D.md).
    params = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'cad_assembly_constraints'
        ordering = ['assembly_project', 'id']
