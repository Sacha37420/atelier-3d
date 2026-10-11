"""Tests backend d'atelier-3d (chantier « tests », contrat v1).

    python manage.py test

Aucun réseau, aucun calcul lourd : Keycloak (JWKS, jeton du compte de service)
et l'app `storage` sont simulés au niveau HTTP (cf. helpers.FauxStorage), les
tâches Celery sont jouées en local (`.apply()`), COLMAP / OpenMVS / FreeCAD /
pymeshlab / FastSAM ne sont jamais lancés.
"""
