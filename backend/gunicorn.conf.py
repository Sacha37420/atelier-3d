"""Configuration de gunicorn (serveur WSGI de l'app en service).

Remplace `manage.py runserver`, qui est un serveur de développement. Chargé par
le CMD du Dockerfile : `gunicorn -c gunicorn.conf.py config.wsgi:application`.
"""
import os

bind = '0.0.0.0:8000'

# Un processus, des fils d'exécution : le même modèle que runserver. Un état
# gardé en mémoire (verrou, cache, ContextVar…) reste donc partagé entre toutes
# les requêtes, comme avant. Le serveur n'a que 2 vCPU pour tout le lab : monter
# WEB_CONCURRENCY (dans le .env) seulement pour une app qui en a besoin ET qui ne
# garde aucun état en mémoire.
workers = int(os.environ.get('WEB_CONCURRENCY', '1'))
threads = int(os.environ.get('WEB_THREADS', '8'))
worker_class = 'gthread'

# Délai après lequel un processus qui ne donne plus signe de vie est relancé.
# Avec des fils d'exécution, une requête longue (téléversement, téléchargement,
# flux) ne le déclenche pas : c'est le processus qui est surveillé, pas la requête.
timeout = 120
graceful_timeout = 30

# Pas de journal d'accès : les URL peuvent porter des paramètres sensibles
# (`?key=`, `?ticket=`), et Django journalise déjà les erreurs.
accesslog = None
errorlog = '-'


def post_worker_init(worker):
    # `SCRIPT_NAME` (.env) est lu par config/settings.py (FORCE_SCRIPT_NAME) :
    # c'est le préfixe PUBLIC de l'API. Caddy le retire avant de transmettre
    # (`handle_path`), or gunicorn, s'il trouve cette variable d'environnement,
    # exige que chaque chemin reçu commence par elle — et répond 500 à tout.
    # Les réglages Django sont déjà chargés ici : on la retire pour gunicorn seul.
    os.environ.pop('SCRIPT_NAME', None)
