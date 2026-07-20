"""Application Celery pour les tâches asynchrones (reconstruction 3D, calculs longs)."""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('atelier_3d')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
