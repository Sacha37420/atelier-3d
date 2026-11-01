import { HttpClient } from '@angular/common/http';
import { Subscription } from 'rxjs';
import { GLTF, GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

import { AuthBlobService } from './auth-blob.service';

/**
 * Charge un glTF sans jamais mettre le jeton dans l'URL.
 *
 * Le fetch() interne de `GLTFLoader.load()` ne pose aucun en-tête
 * Authorization : pour une adresse d'une API du lab (`tokenBases()`), le
 * fichier est lu par HttpClient (`arraybuffer`, jeton posé par l'intercepteur)
 * puis décodé par `GLTFLoader.parse()`. Les maillages de l'app sont des `.glb`
 * autonomes (géométrie et textures dans le même fichier, cf. api/tasks.py) :
 * aucune ressource externe n'est à résoudre, d'où le chemin de base vide.
 * Une adresse hors des API du lab garde `load(url)`.
 *
 * Rend un abonnement à résilier si l'adresse change avant la fin de la lecture
 * (l'appelant doit aussi ignorer un `onLoad` tardif, `parse` n'étant pas
 * annulable).
 */
export function loadGltf(
  http: HttpClient,
  blobs: AuthBlobService,
  url: string,
  onLoad: (gltf: GLTF) => void,
  onError: (err: unknown) => void,
): Subscription | undefined {
  if (!blobs.needsToken(url)) {
    new GLTFLoader().load(url, onLoad, undefined, onError);
    return undefined;
  }
  return http.get(url, { responseType: 'arraybuffer' }).subscribe({
    next: buffer => new GLTFLoader().parse(buffer, '', onLoad, onError),
    error: onError,
  });
}
