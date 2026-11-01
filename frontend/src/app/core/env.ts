/**
 * Configuration injectée au démarrage du conteneur (`assets/env.js`, écrit par
 * `nginx-entrypoint.sh`). UN SEUL endroit lit `window.__env`.
 */
export interface LabEnv {
  keycloakUrl?: string;
  keycloakRealm?: string;
  keycloakClientId?: string;
  appUrl?: string;
  apiUrl?: string;
  /** Base de l'API storage (`https://<DOMAIN>/storage-api`) ; absente en HTTP local sans domaine. */
  storageApiUrl?: string;
}

export function labEnv(): LabEnv {
  return (window as unknown as { __env?: LabEnv }).__env ?? {};
}

/** Base de l'API de cette app, sans barre finale. */
export function apiUrl(): string {
  return (labEnv().apiUrl ?? 'http://localhost:8092').replace(/\/+$/, '');
}

/** Base de l'API storage, sans barre finale ; `null` si elle n'est pas configurée. */
export function storageApiUrl(): string | null {
  const url = labEnv().storageApiUrl;
  return typeof url === 'string' && url.trim() ? url.trim().replace(/\/+$/, '') : null;
}

/**
 * Les SEULES adresses auxquelles le jeton de l'utilisateur est joint
 * (`auth.interceptor.ts`, `token-scope.ts`) : l'API de l'app, et storage —
 * le navigateur y lit directement photos, maillages glTF et STEP
 * (`ApiService.mediaUrl`, `LabStorage.url()` côté backend).
 */
export function tokenBases(): string[] {
  const bases = [apiUrl()];
  const storage = storageApiUrl();
  if (storage) bases.push(storage);
  return bases;
}
