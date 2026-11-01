import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map, of } from 'rxjs';

import { tokenBases } from './env';
import { carriesToken } from './token-scope';

/**
 * Lecture des fichiers protégés (photos…) sans jamais mettre le jeton dans une URL.
 *
 * Une balise <img src> ne peut pas porter d'en-tête Authorization : le fichier
 * est donc lu par HttpClient (`responseType: 'blob'`, le jeton est posé par
 * l'intercepteur, seulement vers `tokenBases()`), puis affiché par une URL
 * `blob:` locale. Celui qui crée une URL `blob:` la révoque
 * (`URL.revokeObjectURL`) quand il n'en a plus besoin — la directive
 * `appAuthSrc` le fait à la destruction de son élément.
 *
 * Une adresse hors de `tokenBases()` (média externe, route publique d'un autre site…) est
 * rendue telle quelle : elle n'a pas besoin du jeton et ne doit pas le recevoir.
 */
@Injectable({ providedIn: 'root' })
export class AuthBlobService {
  private http = inject(HttpClient);

  /** Vrai si `url` est servie par une API du lab qui exige le jeton. */
  needsToken(url: string): boolean {
    return carriesToken(url, tokenBases(), window.location.origin);
  }

  /**
   * Rend une adresse affichable : `blob:` (à révoquer par l'appelant, voir
   * `isObjectUrl`) pour une API du lab, l'adresse elle-même sinon.
   */
  displayUrl(url: string): Observable<string> {
    if (!this.needsToken(url)) return of(url);
    return this.blob(url).pipe(map(b => URL.createObjectURL(b)));
  }

  blob(url: string): Observable<Blob> {
    return this.http.get(url, { responseType: 'blob' });
  }

  /** Télécharge `url` (avec le jeton s'il le faut) sous le nom `filename`. */
  download(url: string, filename: string): void {
    this.displayUrl(url).subscribe(href => {
      const a = document.createElement('a');
      a.href = href;
      a.download = filename;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      if (isObjectUrl(href)) setTimeout(() => URL.revokeObjectURL(href), 60_000);
    });
  }
}

export function isObjectUrl(url: string | null | undefined): url is string {
  return typeof url === 'string' && url.startsWith('blob:');
}
