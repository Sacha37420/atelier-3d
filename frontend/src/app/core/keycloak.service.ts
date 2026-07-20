import { Injectable } from '@angular/core';
import Keycloak from 'keycloak-js';

interface EnvWindow {
  __env?: {
    keycloakUrl?:      string;
    keycloakRealm?:    string;
    keycloakClientId?: string;
    appUrl?:           string;
    apiUrl?:           string;
  };
}

@Injectable({ providedIn: 'root' })
export class KeycloakService {
  private kc!: Keycloak;

  /**
   * Initialise Keycloak avec login-required.
   * Si l'utilisateur n'est pas connecté, le navigateur est redirigé vers la page de login.
   * La promesse ne se résout qu'une fois l'utilisateur authentifié.
   */
  async init(): Promise<boolean> {
    const env = (window as unknown as EnvWindow).__env ?? {};

    this.kc = new Keycloak({
      url:      env.keycloakUrl      ?? (window.location.protocol + '//' + window.location.hostname + ':8080'),
      realm:    env.keycloakRealm    ?? 'ssolab',
      clientId: env.keycloakClientId ?? 'atelier-3d',
    });

    const redirectUri = env.appUrl ?? window.location.origin;

    return this.kc.init({
      onLoad:           'login-required',
      checkLoginIframe: false,
      redirectUri,
    });
  }

  get username(): string {
    return (this.kc?.tokenParsed as Record<string, unknown>)?.['preferred_username'] as string ?? '';
  }

  get email(): string {
    return (this.kc?.tokenParsed as Record<string, unknown>)?.['email'] as string ?? '';
  }

  /** Groupes LDAP/Keycloak de l'utilisateur (claim 'groups' dans le token). */
  get groups(): string[] {
    return (this.kc?.tokenParsed as Record<string, unknown>)?.['groups'] as string[] ?? [];
  }

  /** Token d'accès courant (Bearer). */
  getToken(): string | undefined {
    return this.kc?.token;
  }

  /** Vérifie si le token expire dans moins de minValidity secondes (synchrone). */
  isTokenExpired(minValidity = 0): boolean {
    return this.kc?.isTokenExpired(minValidity) ?? true;
  }

  /**
   * Renouvelle le token si sa validité restante est inférieure à minValidity secondes.
   * Retourne true si le token a été renouvelé, false sinon.
   */
  async updateToken(minValidity: number): Promise<boolean> {
    try {
      return await this.kc.updateToken(minValidity);
    } catch {
      return false;
    }
  }

  logout(): void {
    this.kc.logout({ redirectUri: window.location.origin });
  }
}
