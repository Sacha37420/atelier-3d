from django.urls import path
from .views import (
    MeView, DepartmentListView, UserListView,
    ProjectListCreateView, ProjectDetailView, PhotoUploadView, VideoUploadView,
    ReconstructionEstimateView, ReconstructionLaunchView, JobListView, JobDetailView,
)

urlpatterns = [
    path('me/',          MeView.as_view()),
    path('departments/', DepartmentListView.as_view()),
    path('users/',       UserListView.as_view()),

    path('projects/',                              ProjectListCreateView.as_view()),
    path('projects/<int:pk>/',                      ProjectDetailView.as_view()),
    path('projects/<int:pk>/photos/',                PhotoUploadView.as_view()),
    path('projects/<int:pk>/photos/<int:photo_id>/', PhotoUploadView.as_view()),
    path('projects/<int:pk>/video/',                 VideoUploadView.as_view()),
    path('projects/<int:pk>/reconstruct/',           ReconstructionLaunchView.as_view()),
    path('projects/<int:pk>/reconstruct/estimate/',  ReconstructionEstimateView.as_view()),

    path('jobs/',          JobListView.as_view()),
    path('jobs/<int:pk>/', JobDetailView.as_view()),
]
