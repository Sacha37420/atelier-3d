/**
 * À quelles adresses le jeton de l'utilisateur est-il joint ?
 *
 * Seulement à celles qui tombent SOUS une base configurée : même origine, et
 * chemin égal à celui de la base ou prolongé par « / ». Tout le reste part
 * sans jeton — une URL fabriquée, une autre origine, un chemin voisin
 * (`/lab-admin-api-autre`), une remontée (`/x/../ailleurs`, résolue avant la
 * comparaison).
 *
 * Fichier sans dépendance à Angular : testé sous Node (`token-scope.spec.ts`).
 */
export function carriesToken(url: string, bases: readonly string[], pageOrigin: string): boolean {
  const target = parse(url, pageOrigin);
  if (!target || target.username || target.password) return false;
  for (const base of bases) {
    const b = parse(base, pageOrigin);
    if (!b || b.origin !== target.origin) continue;
    const prefix = b.pathname.replace(/\/+$/, '');
    if (prefix === '' || target.pathname === prefix || target.pathname.startsWith(`${prefix}/`)) return true;
  }
  return false;
}

function parse(url: string, pageOrigin: string): URL | null {
  if (typeof url !== 'string' || !url) return null;
  try {
    const u = new URL(url, pageOrigin);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u : null;
  } catch {
    return null;
  }
}
