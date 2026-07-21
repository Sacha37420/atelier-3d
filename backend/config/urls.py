from django.urls import path, include
from django.http import HttpResponseRedirect
from django.views.static import serve as static_serve
import os
import drf_spectacular_sidecar
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView
from django.conf import settings
from django.utils.functional import cached_property
from api.views import MediaView

# Vue personnalisée Swagger UI
class CustomSwaggerUIView(SpectacularSwaggerView):
    # Use the default drf_spectacular template to avoid missing custom templates
    template_name = "drf_spectacular/swagger_ui.html"
    extra_context = {
        "KEYCLOAK_ISSUER_URI": getattr(settings, "KEYCLOAK_ISSUER_URI", ""),
        "KEYCLOAK_CLIENT_ID": getattr(settings, "KEYCLOAK_CLIENT_ID", "swagger-ui"),
    }

urlpatterns = [
    # Serve drf_spectacular_sidecar assets under /api/docs/sidecar/<file>
    path('api/docs/sidecar/<path:path>', static_serve, {
        'document_root': os.path.join(os.path.dirname(drf_spectacular_sidecar.__file__), 'static', 'drf_spectacular_sidecar', 'swagger-ui-dist')
    }),
    # Serve oauth2 redirect page and script from the sidecar distribution
    path('api/docs/oauth2-redirect.html', static_serve, {
        'path': 'oauth2-redirect.html',
        'document_root': os.path.join(os.path.dirname(drf_spectacular_sidecar.__file__), 'static', 'drf_spectacular_sidecar', 'swagger-ui-dist')
    }, name='oauth2-redirect'),
    path('api/docs/oauth2-redirect.js', static_serve, {
        'path': 'oauth2-redirect.js',
        'document_root': os.path.join(os.path.dirname(drf_spectacular_sidecar.__file__), 'static', 'drf_spectacular_sidecar', 'swagger-ui-dist')
    }),
    path('', lambda request: HttpResponseRedirect(request.META.get('SCRIPT_NAME', '') + '/api/docs/')),
    path('api/', include('api.urls')),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', CustomSwaggerUIView.as_view(), name='swagger-ui'),
    # Photos et maillages (volume externe monté sur MEDIA_ROOT) — chemin relatif
    # 'media/' : derrière Caddy (handle_path), le PATH_INFO côté Django commence
    # déjà par 'media/'. Authentifié (MediaView, cf. api/views.py) : pas de
    # static_serve nu, sinon accessible sans connexion à quiconque devine l'URL.
    path('media/<path:path>', MediaView.as_view()),
]
