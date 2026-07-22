from rest_framework import serializers
from .models import Department, UserRecord, Project, Photo, Job, Mesh, Part, Joint, PhotoLabel, SemanticClass


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
                  'vertex_count', 'face_count', 'is_watertight', 'repair_report', 'created_at']
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

    class Meta:
        model = Project
        fields = ['id', 'name', 'description', 'project_type', 'scale_meters_per_unit',
                  'has_scale', 'has_active_job', 'has_mesh', 'has_resolved_poses', 'photo_count',
                  'owner_email', 'created_at', 'updated_at']
        read_only_fields = ['owner_email', 'created_at', 'updated_at']


class ProjectDetailSerializer(ProjectSerializer):
    photos = PhotoSerializer(many=True, read_only=True)
    jobs = JobSerializer(many=True, read_only=True)
    meshes = MeshSerializer(many=True, read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ['photos', 'jobs', 'meshes']


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
