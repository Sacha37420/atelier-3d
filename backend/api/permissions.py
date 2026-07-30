"""
Cloisonnement par propriétaire — Project, privé par défaut depuis le
2026-07-30 (chantier « sharing-model », Option A retenue par l'utilisateur :
plus aucun projet n'est visible à tout le groupe Keycloak par défaut).

Même principe que TreeOwnerMixin (arbre-genealogique/backend/api/views.py) :
propriétaire (Project.owner_email) = lecture + écriture + gestion des
partages ; ProjectShare.role == EDITOR = lecture + écriture ; VIEWER =
lecture seule. Adapté au style de vues d'atelier-3d (des APIView explicites
pour la plupart, pas uniquement des ModelViewSet avec un seul get_queryset
central) : plutôt qu'un mixin de classe, ce module expose des fonctions
autonomes que chaque vue appelle directement — get_visible_project()/
get_editable_project() remplacent get_object_or_404(Project, pk=pk) partout
où un projet est résolu depuis l'URL ; check_read()/check_write() couvrent
les ressources filles déjà récupérées autrement (mesh.project, photo.project,
part.mesh.project…).

── Sous-projets (Project.parent_project non nul) ──────────────────────────
Toujours résolu via Project.root_project : un sous-projet CAO (Lot 5.2) n'a
JAMAIS de ProjectShare propre, ni de rôle propre — seul compte le projet
racine. Voir Project.root_project (api/models.py) pour le détail complet du
raisonnement (relation de composition, pas un rangement façon dossier).

── 404 plutôt que 403 pour la lecture ──────────────────────────────────────
Un projet totalement invisible (ni propriétaire, ni partagé) répond 404, pas
403 : ne pas laisser deviner qu'il existe (même choix que
get_object_or_404(queryset_filtré) chez TreeOwnerMixin). Un projet visible en
lecture mais pas en écriture répond 403 sur une action d'écriture — refuser
poliment, pas cacher l'existence d'un projet qu'on peut déjà voir.
"""
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied

from .models import Project, ProjectShare


def visible_projects(email: str):
    """Projets (top-level ET sous-projets) lisibles par `email` — propriétaire
    ou membre d'un ProjectShare, TOUJOURS évalué sur le projet racine."""
    return Project.objects.filter(
        Q(parent_project__isnull=True, owner_email=email)
        | Q(parent_project__isnull=True, shares__email=email)
        | Q(parent_project__owner_email=email)
        | Q(parent_project__shares__email=email)
    ).distinct()


def editable_projects(email: str):
    """Comme visible_projects(), restreint à l'écriture (propriétaire, ou
    ProjectShare.role == EDITOR sur le projet racine)."""
    return Project.objects.filter(
        Q(parent_project__isnull=True, owner_email=email)
        | Q(parent_project__isnull=True, shares__email=email, shares__role=ProjectShare.Role.EDITOR)
        | Q(parent_project__owner_email=email)
        | Q(parent_project__shares__email=email, parent_project__shares__role=ProjectShare.Role.EDITOR)
    ).distinct()


def owned_projects(email: str):
    """Projets top-level dont `email` est le véritable propriétaire — seul
    niveau où gérer les ProjectShare ou supprimer le projet a un sens."""
    return Project.objects.filter(parent_project__isnull=True, owner_email=email)


def can_read(project: Project, email: str) -> bool:
    root = project.root_project
    if root.owner_email == email:
        return True
    return bool(email) and root.shares.filter(email=email).exists()


def can_write(project: Project, email: str) -> bool:
    root = project.root_project
    if root.owner_email == email:
        return True
    return bool(email) and root.shares.filter(email=email, role=ProjectShare.Role.EDITOR).exists()


def check_read(project: Project, request) -> None:
    if not can_read(project, getattr(request.user, 'email', '')):
        raise Http404()


def check_write(project: Project, request) -> None:
    email = getattr(request.user, 'email', '')
    if not can_read(project, email):
        raise Http404()
    if not can_write(project, email):
        raise PermissionDenied("Vous n'avez pas le droit de modifier ce projet.")


def check_owner(project: Project, request) -> None:
    """Réservé aux actions sur le projet racine lui-même : gérer les
    ProjectShare, le supprimer."""
    email = getattr(request.user, 'email', '')
    if not can_read(project, email):
        raise Http404()
    if project.root_project.owner_email != email:
        raise PermissionDenied('Seul le propriétaire du projet peut faire cela.')


def get_visible_project(request, pk) -> Project:
    """Équivalent de get_object_or_404(Project, pk=pk), mais 404 aussi pour un
    projet qui existe bel et bien tant que `request.user` ne peut pas le voir."""
    return get_object_or_404(visible_projects(getattr(request.user, 'email', '')), pk=pk)


def get_editable_project(request, pk) -> Project:
    return get_object_or_404(editable_projects(getattr(request.user, 'email', '')), pk=pk)


def get_owned_project(request, pk) -> Project:
    return get_object_or_404(owned_projects(getattr(request.user, 'email', '')), pk=pk)


def role_on(project: Project, email: str) -> str | None:
    root = project.root_project
    if root.owner_email == email:
        return 'OWNER'
    share = root.shares.filter(email=email).first()
    return share.role if share else None
