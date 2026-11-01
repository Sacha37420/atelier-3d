/**
 * Adresse de retour après la connexion Keycloak.
 *
 * Avant : toujours `env.appUrl` — un lien direct (`/projets/<app>`, une page
 * d'admin) ou un simple rechargement retombait sur l'accueil. Maintenant : la
 * page demandée, à trois conditions qui tiennent ensemble la sûreté :
 *
 *  - elle vient de `window.location` et de rien d'autre — jamais d'un paramètre
 *    (`?redirect=`, `?next=`…) : ce n'est pas un redirecteur ouvert ;
 *  - elle est sur la même origine que l'app ET sous son préfixe (`/lab-admin/`) ;
 *    sinon on retombe sur `appUrl` ;
 *  - le fragment est retiré (keycloak-js y pose `state`/`code` au retour).
 *
 * La garde de route s'applique de toute façon après le retour : revenir sur
 * une page d'admin ne donne aucun droit, on y tombe sur « Accès restreint ».
 */
export function loginRedirectUri(currentHref: string, appUrl: string | undefined, origin: string): string {
  const fallback = appUrl ?? origin;
  let here: URL;
  let base: URL;
  try {
    here = new URL(currentHref);
    base = new URL(fallback);
  } catch {
    return fallback;
  }
  if (here.origin !== base.origin || (here.protocol !== 'https:' && here.protocol !== 'http:')) return fallback;
  // Préfixe de chemin strict : « /lab-admin/ » n'englobe pas « /lab-admin-api/… ».
  const prefix = base.pathname.endsWith('/') ? base.pathname : base.pathname + '/';
  const path = here.pathname;
  if (path !== prefix && !path.startsWith(prefix) && path + '/' !== prefix) return fallback;
  // Chemin normalisé par URL() : aucun « .. » ne subsiste.
  return here.origin + path + here.search;
}
