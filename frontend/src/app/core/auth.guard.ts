import { CanActivateFn } from '@angular/router';

/**
 * Guard passthrough : Keycloak gère la redirection vers la page de login
 * via le mode 'login-required' configuré dans KeycloakService.init().
 * Ce guard peut être étendu pour vérifier des rôles ou groupes spécifiques.
 */
export const authGuard: CanActivateFn = () => true;
