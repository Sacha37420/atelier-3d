#!/bin/sh
# Généré par new-app.sh — injecte la configuration Keycloak dans env.js
set -e

ASSETS=/usr/share/nginx/html/assets
mkdir -p "$ASSETS"

if [ "${DOMAIN:-CHANGE_ME}" != "CHANGE_ME" ] && [ -n "${DOMAIN:-}" ]; then
  cat > "$ASSETS/env.js" << JSEOF
window.__env = {
  keycloakUrl:      "${KEYCLOAK_PUBLIC_URL:-http://localhost:8080}",
  keycloakRealm:    "${KEYCLOAK_REALM:-ssolab}",
  keycloakClientId: "${KEYCLOAK_CLIENT_ID}",
  apiUrl:           "https://${DOMAIN}/atelier-3d-api",
  appUrl:           "https://${DOMAIN}/atelier-3d/"
};
JSEOF
else
  cat > "$ASSETS/env.js" << JSEOF
window.__env = {
  keycloakUrl:      "${KEYCLOAK_PUBLIC_URL:-http://localhost:8080}",
  keycloakRealm:    "${KEYCLOAK_REALM:-ssolab}",
  keycloakClientId: "${KEYCLOAK_CLIENT_ID}",
  apiUrl:           window.location.protocol + '//' + window.location.hostname + ':${PORT_BACKEND:-8000}',
  appUrl:           window.location.protocol + '//' + window.location.hostname + ':${PORT_FRONTEND:-4200}'
};
JSEOF
fi

chmod 644 "$ASSETS/env.js"
echo "[nginx] env.js généré."
