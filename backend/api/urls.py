from django.urls import path
from .views import (
    MeView, DepartmentListView, UserListView,
    ProjectListCreateView, ProjectDetailView,
    ProjectShareListCreateView, ProjectShareDetailView,
    PhotoUploadView, VideoUploadView,
    ReconstructionEstimateView, ReconstructionLaunchView, JobListView, JobDetailView,
    RepairLaunchView, MeshAutoOrientView, MeshExportView,
    PartListCreateView, PartSuggestView, PartDetailView,
    JointListCreateView, JointDetailView, SuggestJointAxisView,
    PhotoRegionsView, PhotoLabelListCreateView, PhotoLabelDetailView,
    FacadeEstimateView, FacadeLaunchView, SemanticClassListView,
    CadSketchListCreateView, CadSketchDetailView,
    CadOperationListCreateView, CadOperationDetailView, CadBuildLaunchView,
    SubPartListCreateView,
    CadAssemblyInstanceListCreateView, CadAssemblyInstanceDetailView,
    CadAssemblyConstraintListCreateView, CadAssemblyConstraintDetailView,
    CadAssembleLaunchView,
    PublicAssemblyListView, PublicAssemblyDetailView, PublicAssemblyMeshFileView,
)

urlpatterns = [
    path('me/',          MeView.as_view()),
    path('departments/', DepartmentListView.as_view()),
    path('users/',       UserListView.as_view()),

    path('projects/',                              ProjectListCreateView.as_view()),
    path('projects/<int:pk>/',                      ProjectDetailView.as_view()),
    path('projects/<int:pk>/shares/',                ProjectShareListCreateView.as_view()),
    path('project-shares/<int:pk>/',                 ProjectShareDetailView.as_view()),
    path('projects/<int:pk>/photos/',                PhotoUploadView.as_view()),
    path('projects/<int:pk>/photos/<int:photo_id>/', PhotoUploadView.as_view()),
    path('projects/<int:pk>/video/',                 VideoUploadView.as_view()),
    path('projects/<int:pk>/reconstruct/',           ReconstructionLaunchView.as_view()),
    path('projects/<int:pk>/reconstruct/estimate/',  ReconstructionEstimateView.as_view()),
    path('projects/<int:pk>/repair/',                RepairLaunchView.as_view()),
    path('projects/<int:pk>/facade/',                FacadeLaunchView.as_view()),
    path('projects/<int:pk>/facade/estimate/',       FacadeEstimateView.as_view()),

    path('meshes/<int:pk>/auto-orient/', MeshAutoOrientView.as_view()),
    path('meshes/<int:pk>/export/',      MeshExportView.as_view()),
    path('meshes/<int:mesh_id>/parts/',          PartListCreateView.as_view()),
    path('meshes/<int:mesh_id>/parts/suggest/',  PartSuggestView.as_view()),
    path('meshes/<int:mesh_id>/joints/',         JointListCreateView.as_view()),
    path('meshes/<int:mesh_id>/semantic-classes/', SemanticClassListView.as_view()),

    path('parts/<int:pk>/',              PartDetailView.as_view()),
    path('parts/<int:pk>/suggest-axis/', SuggestJointAxisView.as_view()),
    path('joints/<int:pk>/',             JointDetailView.as_view()),

    path('photos/<int:pk>/regions/',      PhotoRegionsView.as_view()),
    path('photos/<int:photo_id>/labels/', PhotoLabelListCreateView.as_view()),
    path('photo-labels/<int:pk>/',        PhotoLabelDetailView.as_view()),

    path('jobs/',          JobListView.as_view()),
    path('jobs/<int:pk>/', JobDetailView.as_view()),

    path('projects/<int:pk>/cad-sketches/',   CadSketchListCreateView.as_view()),
    path('cad-sketches/<int:pk>/',            CadSketchDetailView.as_view()),
    path('projects/<int:pk>/cad-operations/', CadOperationListCreateView.as_view()),
    path('cad-operations/<int:pk>/',          CadOperationDetailView.as_view()),
    path('projects/<int:pk>/cad-build/',      CadBuildLaunchView.as_view()),

    path('projects/<int:pk>/sub-parts/',       SubPartListCreateView.as_view()),
    path('projects/<int:pk>/cad-instances/',   CadAssemblyInstanceListCreateView.as_view()),
    path('cad-instances/<int:pk>/',            CadAssemblyInstanceDetailView.as_view()),
    path('projects/<int:pk>/cad-constraints/', CadAssemblyConstraintListCreateView.as_view()),
    path('cad-constraints/<int:pk>/',          CadAssemblyConstraintDetailView.as_view()),
    path('projects/<int:pk>/cad-assemble/',    CadAssembleLaunchView.as_view()),

    # Lot 5.3 — API publique, sans authentification (cf. to_do_3D.md, exception actée)
    path('public/assemblies/',                    PublicAssemblyListView.as_view()),
    path('public/assemblies/<int:pk>/',           PublicAssemblyDetailView.as_view()),
    path('public/assemblies/<int:pk>/<str:fmt>/', PublicAssemblyMeshFileView.as_view()),
]
