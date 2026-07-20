import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { from, switchMap, catchError } from 'rxjs';
import { KeycloakService } from './keycloak.service';

export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const kc = inject(KeycloakService);
  const token = kc.getToken();

  // Chemin synchrone : token présent et valide — aucune attente async
  if (token && !kc.isTokenExpired(30)) {
    return next(req.clone({ setHeaders: { Authorization: `Bearer ${token}` } }));
  }

  // Chemin async : token expiré ou absent — tentative de refresh
  return from(kc.updateToken(-1)).pipe(
    switchMap(() => {
      const fresh = kc.getToken();
      if (fresh) req = req.clone({ setHeaders: { Authorization: `Bearer ${fresh}` } });
      return next(req);
    }),
    catchError(() => next(req)),
  );
};
