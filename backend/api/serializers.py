from rest_framework import serializers
from .models import (
    Department, UserRecord, Project, ProjectShare, Photo, Job, Mesh, Part, Joint, PhotoLabel,
    SemanticClass, CadSketch, CadOperation, CadAssemblyInstance, CadAssemblyConstraint,
)


class DepartmentSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(source='members.count', read_only=True)

    class Meta:
        model = Department
        fields = ['id', 'name', 'description', 'member_count']


class UserRecordSerializer(serializers.ModelSerializer):
    department = DepartmentSerializer(read_only=True)

    class Meta:
        model = UserRecord
        fields = ['email', 'display_name', 'department', 'registered_at']


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 1
# ──────────────────────────────────────────────────────────────────────────────
class PhotoSerializer(serializers.ModelSerializer):
    pose_resolved = serializers.BooleanField(read_only=True)

    class Meta:
        model = Photo
        fields = ['id', 'file', 'order', 'camera_pose', 'pose_resolved',
                  'region_overlay', 'region_count', 'created_at']
        read_only_fields = ['camera_pose', 'region_overlay', 'region_count']


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = ['id', 'project', 'kind', 'status', 'progress', 'message', 'params',
                  'duration_seconds', 'created_at', 'updated_at']
        read_only_fields = fields


class MeshSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mesh
        fields = ['id', 'project', 'job', 'file', 'gltf_file', 'version',
                  'vertex_count', 'face_count', 'is_watertight', 'repair_report',
                  'step_file', 'linear_deflection', 'angular_deflection', 'created_at']
        read_only_fields = fields


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 3 : Mouvements
# ──────────────────────────────────────────────────────────────────────────────
class PartSerializer(serializers.ModelSerializer):
    face_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Part
        fields = ['id', 'mesh', 'name', 'face_ids', 'color', 'suggested',
                  'primitive_type', 'primitive_params', 'face_count', 'created_at', 'updated_at']
        read_only_fields = ['mesh', 'suggested', 'primitive_type', 'primitive_params',
                             'created_at', 'updated_at']


class JointSerializer(serializers.ModelSerializer):
    class Meta:
        model = Joint
        fields = ['id', 'parent_part', 'child_part', 'joint_type',
                  'axis_origin', 'axis_direction', 'limit_min', 'limit_max', 'created_at']
        read_only_fields = ['created_at']


class ProjectSerializer(serializers.ModelSerializer):
    photo_count = serializers.IntegerField(source='photos.count', read_only=True)
    has_scale = serializers.BooleanField(read_only=True)
    has_active_job = serializers.BooleanField(read_only=True)
    has_mesh = serializers.BooleanField(read_only=True)
    has_resolved_poses = serializers.BooleanField(read_only=True)
    sub_parts_count = serializers.IntegerField(source='sub_parts.count', read_only=True)
    # Rôle de celui qui regarde (OWNER/EDITOR/VIEWER), TOUJOURS calculé sur le
    # projet racine (cf. Project.root_project) — un sous-projet renvoie donc le
    # rôle hérité de son parent, pas un rôle qui lui serait propre (il n'en a
    # pas, cf. api/permissions.py). None si get_serializer_context() n'a pas
    # injecté 'role_of' (ex. réponses construites sans requête HTTP courante).
    my_role = serializers.SerializerMethodField()
    # 0 pour un sous-projet par construction : ProjectShare ne porte que sur
    # des projets top-level (cf. ProjectShareViewSet).
    shared_with_count = serializers.IntegerField(source='shares.count', read_only=True)

    class Meta:
        model = Project
        fields = ['id', 'name', 'description', 'project_type', 'scale_meters_per_unit',
                  'assembly_status', 'parent_project', 'has_scale', 'has_active_job', 'has_mesh',
                  'has_resolved_poses', 'sub_parts_count', 'photo_count', 'owner_email',
                  'my_role', 'shared_with_count', 'created_at', 'updated_at']
        read_only_fields = ['assembly_status', 'parent_project', 'owner_email', 'created_at', 'updated_at']

    def get_my_role(self, obj: Project):
        if role_of := self.context.get('role_of'):
            return role_of(obj)
        return None


class ProjectDetailSerializer(ProjectSerializer):
    photos = PhotoSerializer(many=True, read_only=True)
    jobs = JobSerializer(many=True, read_only=True)
    meshes = MeshSerializer(many=True, read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ['photos', 'jobs', 'meshes']


class ProjectShareSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectShare
        fields = ['id', 'project', 'email', 'role', 'invited_by', 'created_at']
        # 'project' est toujours fixé par la vue depuis l'URL (/api/projects/<id>/shares/),
        # jamais par le client — même convention que CadSketchSerializer/CadOperationSerializer.
        read_only_fields = ['project', 'invited_by', 'created_at']
        # DRF déduirait de la contrainte d'unicité (projet, e-mail) un validateur
        # qui refuserait une seconde invitation de la même personne. Or ce
        # geste-là veut dire « change son rôle » : la vue le traite en upsert
        # (cf. ProjectShareListCreateView.post), et validerait ici un 400
        # incompréhensible avant même d'y arriver.
        validators = []

    def validate_email(self, value: str) -> str:
        # L'e-mail est la clé d'identité (elle vient du JWT) : une casse ou une
        # espace de trop et l'invité ne retrouverait jamais le projet partagé.
        return value.strip().lower()


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 4 : Bâtiments
# ──────────────────────────────────────────────────────────────────────────────
class PhotoLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = PhotoLabel
        fields = ['id', 'photo', 'semantic_class', 'region_index', 'created_at']
        read_only_fields = ['photo', 'created_at']


class SemanticClassSerializer(serializers.ModelSerializer):
    face_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SemanticClass
        fields = ['id', 'mesh', 'name', 'color', 'face_ids', 'face_count', 'created_at']
        read_only_fields = fields


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 5 : Conception CAO manuelle et Assemblage
# ──────────────────────────────────────────────────────────────────────────────
class CadSketchSerializer(serializers.ModelSerializer):
    class Meta:
        model = CadSketch
        fields = ['id', 'project', 'name', 'plane', 'entities', 'created_at', 'updated_at']
        read_only_fields = ['project', 'created_at', 'updated_at']


class CadOperationSerializer(serializers.ModelSerializer):
    class Meta:
        model = CadOperation
        fields = ['id', 'project', 'order', 'operation_type', 'params', 'created_at', 'updated_at']
        read_only_fields = ['project', 'created_at', 'updated_at']


class CadAssemblyInstanceSerializer(serializers.ModelSerializer):
    # Limite topologique n°2 (cf. to_do_3D.md) : source_mesh est pinné à la
    # création, mais rien n'empêchait jusqu'ici qu'un Mesh plus récent existe
    # déjà pour source_project (la sous-partie a été reconstruite après coup)
    # sans que l'instance ni l'utilisateur n'en soient jamais informés.
    # is_outdated/latest_mesh_version exposent cette dérive au frontend (badge
    # + action de re-pointage) ; CadAssembleLaunchView refait le même calcul
    # côté serveur pour bloquer une résolution silencieuse (cf. views.py).
    source_mesh_version = serializers.IntegerField(source='source_mesh.version', read_only=True)
    latest_mesh_id = serializers.SerializerMethodField()
    latest_mesh_version = serializers.SerializerMethodField()
    is_outdated = serializers.SerializerMethodField()

    class Meta:
        model = CadAssemblyInstance
        fields = ['id', 'assembly_project', 'source_project', 'source_mesh', 'source_mesh_version',
                  'latest_mesh_id', 'latest_mesh_version', 'is_outdated', 'label', 'placement', 'created_at']
        read_only_fields = ['assembly_project', 'placement', 'created_at']

    def _latest_mesh(self, obj: CadAssemblyInstance):
        return obj.source_project.meshes.order_by('-version').first()

    def get_latest_mesh_id(self, obj: CadAssemblyInstance):
        latest = self._latest_mesh(obj)
        return latest.id if latest else None

    def get_latest_mesh_version(self, obj: CadAssemblyInstance):
        latest = self._latest_mesh(obj)
        return latest.version if latest else None

    def get_is_outdated(self, obj: CadAssemblyInstance) -> bool:
        latest = self._latest_mesh(obj)
        return latest is not None and latest.version > obj.source_mesh.version


class CadAssemblyConstraintSerializer(serializers.ModelSerializer):
    class Meta:
        model = CadAssemblyConstraint
        fields = ['id', 'assembly_project', 'constraint_type', 'instance_a', 'reference_a',
                  'instance_b', 'reference_b', 'params', 'created_at']
        read_only_fields = ['assembly_project', 'created_at']


# ──────────────────────────────────────────────────────────────────────────────
# ATELIER 3D — Lot 5.3 : API publique (lecture seule, sans authentification)
# ──────────────────────────────────────────────────────────────────────────────
class PublicAssemblySerializer(serializers.ModelSerializer):
    """
    Champs volontairement réduits par rapport à ProjectSerializer — pas
    d'owner_email/photo_count/my_role/shared_with_count (données de
    cloisonnement interne, sans objet pour un visiteur anonyme). `latest_mesh`
    ne pointe jamais directement un chemin storage : les URLs proxient via
    PublicAssemblyMeshFileView (cf. api/views.py), storage n'ayant aucun
    chemin de lecture anonyme (même raisonnement que public_plat_photo de
    restauration).
    """

    latest_mesh = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = ['id', 'name', 'description', 'assembly_status', 'latest_mesh', 'created_at', 'updated_at']
        read_only_fields = fields

    def get_latest_mesh(self, obj: Project):
        mesh = obj.meshes.order_by('-version').first()
        if mesh is None:
            return None
        return {
            'id': mesh.id,
            'version': mesh.version,
            'vertex_count': mesh.vertex_count,
            'face_count': mesh.face_count,
            'is_watertight': mesh.is_watertight,
            'gltf_url': f'/api/public/assemblies/{obj.id}/gltf/' if mesh.gltf_file else None,
            'step_url': f'/api/public/assemblies/{obj.id}/step/' if mesh.step_file else None,
        }
