from django.conf import settings


def add_bearer_security(result, generator, request, public):
    """Postprocessing hook for drf-spectacular to ensure components.securitySchemes
    contains a Bearer/OAuth2 definition for Keycloak.
    """
    comps = result.setdefault("components", {})
    sec = comps.setdefault("securitySchemes", {})
    # Do not overwrite if present
    if "BearerAuth" in sec:
        return result

    # Prefer a WAN-facing issuer URL constructed from KEYCLOAK_PUBLIC_URL and
    # KEYCLOAK_REALM when available (so Swagger UI points to the public Keycloak).
    public_url = getattr(settings, "KEYCLOAK_PUBLIC_URL", None)
    realm = getattr(settings, "KEYCLOAK_REALM", None)
    if public_url and realm:
        issuer = f"{public_url.rstrip('/')}/realms/{realm}"
    else:
        issuer = getattr(settings, "KEYCLOAK_ISSUER_URI", "http://keycloak:8080/realms/ssolab")
    sec["BearerAuth"] = {
        "type": "oauth2",
        "flows": {
            "authorizationCode": {
                "authorizationUrl": f"{issuer}/protocol/openid-connect/auth",
                "tokenUrl": f"{issuer}/protocol/openid-connect/token",
                "scopes": {
                    "openid": "OpenID Connect scope",
                    "profile": "Profile scope",
                    "email": "Email scope",
                },
            }
        },
    }
    return result
