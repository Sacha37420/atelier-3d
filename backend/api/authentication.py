import jwt
from jwt import PyJWKClient, InvalidTokenError
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.conf import settings


class KeycloakUser:
    """Minimal user object constructed from Keycloak JWT claims."""

    is_authenticated = True
    is_active = True

    def __init__(self, claims: dict) -> None:
        self.email: str = claims.get('email', '')
        self.username: str = claims.get('preferred_username', self.email)
        self.claims = claims
        # Required by some DRF internals
        self.pk = self.email

    def __str__(self) -> str:
        return self.username


class KeycloakJWTAuthentication(BaseAuthentication):
    """
    Validates Keycloak Bearer JWT tokens using the realm's JWKS endpoint.

    The JWKS client is cached at the class level so keys are fetched once
    per process start-up and reused for all subsequent requests.
    """

    _jwks_client: PyJWKClient | None = None

    @classmethod
    def _get_jwks_client(cls) -> PyJWKClient:
        if cls._jwks_client is None:
            jwks_uri = (
                f'{settings.KEYCLOAK_ISSUER_URI}'
                '/protocol/openid-connect/certs'
            )
            cls._jwks_client = PyJWKClient(jwks_uri, cache_keys=True)
        return cls._jwks_client

    def authenticate(self, request):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return None

        token = auth_header[7:]

        try:
            client = self._get_jwks_client()
            signing_key = client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=['RS256'],
                options={
                    'verify_exp': True,
                    # Audience varies by Keycloak client configuration
                    'verify_aud': False,
                },
            )
        except InvalidTokenError as exc:
            raise AuthenticationFailed(f'Token invalide : {exc}') from exc

        # Le token doit avoir été émis POUR cette application.
        # Sans ce contrôle, n'importe quel token du realm est accepté : le realm
        # expose 'admin-cli' en client public autorisant le password grant, donc
        # tout compte — y compris un compte auto-inscrit — pourrait s'en servir
        # pour appeler cette API. On vérifie 'azp' plutôt que 'aud' car Keycloak
        # ne place pas le clientId dans 'aud' sans mapper d'audience dédié.
        if claims.get('azp') != settings.KEYCLOAK_CLIENT_ID:
            raise AuthenticationFailed(
                "Ce token a été émis pour un autre client que "
                f"'{settings.KEYCLOAK_CLIENT_ID}'."
            )

        # Cloisonnement par groupe. KEYCLOAK_REQUIRED_GROUPS vide ⇒ aucun filtre :
        # toute personne authentifiée sur ce client passe.
        required = {
            g.strip()
            for g in settings.KEYCLOAK_REQUIRED_GROUPS.split(',')
            if g.strip()
        }
        if required and not required & set(claims.get('groups') or []):
            raise AuthenticationFailed(
                'Accès réservé aux membres du/des groupe(s) : '
                f"{', '.join(sorted(required))}."
            )

        if not claims.get('email'):
            raise AuthenticationFailed(
                "Le token ne contient pas de claim 'email'. "
                "Vérifiez que le mapper 'email' est activé dans Keycloak."
            )

        return KeycloakUser(claims), token
