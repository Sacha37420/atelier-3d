from rest_framework import serializers
from .models import Department, UserRecord, Project, Photo, Job, Mesh


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
        fields = ['id', 'file', 'order', 'camera_pose', 'pose_resolved', 'created_at']
        read_only_fields = ['camera_pose']


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
                  'vertex_count', 'face_count', 'created_at']
        read_only_fields = fields


class ProjectSerializer(serializers.ModelSerializer):
    photo_count = serializers.IntegerField(source='photos.count', read_only=True)
    has_scale = serializers.BooleanField(read_only=True)
    has_active_job = serializers.BooleanField(read_only=True)

    class Meta:
        model = Project
        fields = ['id', 'name', 'description', 'project_type', 'scale_meters_per_unit',
                  'has_scale', 'has_active_job', 'photo_count', 'owner_email',
                  'created_at', 'updated_at']
        read_only_fields = ['owner_email', 'created_at', 'updated_at']


class ProjectDetailSerializer(ProjectSerializer):
    photos = PhotoSerializer(many=True, read_only=True)
    jobs = JobSerializer(many=True, read_only=True)
    meshes = MeshSerializer(many=True, read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ['photos', 'jobs', 'meshes']
