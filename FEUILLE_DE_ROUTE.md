# Feuille de route — atelier-3d

Ce fichier est lu par une machine : `validate-app.sh` refuse de déployer s'il manque ou s'il ne
contient aucun lot au format ci-dessous, et `app-test.sh` lit la commande de test. Garder les titres
tels quels.

App livrée avant l'obligation de feuille de route : le Lot 1 résume l'existant. Les lots suivants
viennent de l'analyse du 2026-10-31 ; la vue d'ensemble est dans `to_do_global.md`, à la racine du
dépôt du lab. Note d'analyse détaillée sur Bâtiments et les trois modes d'initialisation :
`to_do_atelier_3d.md`, à côté de ce fichier (2026-07-31, verdict toujours d'actualité, numéros de
ligne périmés). Légende : [C] constaté, [D] déduit.

## Commande de test
python manage.py test

## Lot 1 — Existant : photogrammétrie, import, CAO, Impression, Mouvements, Bâtiments   ·   statut : livré
**Objectif :** reconstruction COLMAP/OpenMVS, import de maillages produits chez le client, conception
CAO (build123d, FreeCAD), modules Impression, Mouvements et Bâtiments. Fichiers dans `storage`
(`LabStorage`). Accès : `developers,famille,amis`.
**Tâches :**
- [x] Lots 1 à 5 du projet (CAO et assemblage terminés le 2026-07-31)
- [x] Correctifs du chantier tests (`chantiers/tests/rapports/atelier.md`), dont la purge `storage` (`f203b47`)
**Tests à passer avant de clore le lot :**
- [x] 174 tests backend (9 fichiers) ; `cloisonnement.spec.ts` identique au gabarit
**Vérifié le :** 2026-10-31 — relevé par lecture (version en service = `44e0099`), tests non rejoués.

## Lot 2 — Jobs robustes   ·   statut : à faire
**Objectif :** un redéploiement ou une entrée mal formée ne bloque plus personne.
**Constat :**
- [C] Aucune libération des jobs orphelins au démarrage du worker (pas de `worker_ready` dans
  `api/`, contrairement à bilan-thermique `tasks.py:282`). [D] Un redéploiement pendant un job laisse
  un Job `RUNNING`, et tout le monde reçoit 409.
- [C] `face_ids` n'est contrôlé que comme liste non vide (`views.py:762,811`) : 500 possible.
- [C] Course du verrou global (`views.py:492,564,627,1159`, `select_for_update` sans ligne à
  verrouiller) : **laissée telle quelle par décision du propriétaire**.
**Tâches :**
- [ ] Au signal `worker_ready`, passer en `ERROR` les jobs `RUNNING`/`PENDING` orphelins
- [ ] Valider `face_ids` : entiers, dans les bornes du maillage → 400
- [ ] Ⓟ Vrai verrou (`pg_advisory_xact_lock`), si le propriétaire revient sur sa décision
**Tests à passer avant de clore le lot :**
- [ ] Test « worker redémarré » : le job orphelin passe en `ERROR`, un nouveau job est accepté
- [ ] `face_ids` mal formés → 400
- [ ] Si verrou : `CourseDuVerrouTests` rend `[202, 409]`
**Vérifié le :**

## Lot 3 — Bâtiments et projets « Photos client »   ·   statut : à faire
**Objectif :** l'utilisateur comprend pourquoi Bâtiments est indisponible, puis peut l'utiliser.
**Constat [C] :** `batiments.component.ts:57` filtre sur `has_resolved_poses`, sans aucune
explication sur la page projet (`to_do_atelier_3d.md`, « correctif immédiat » non fait).
**Tâches :**
- [ ] Message explicatif sur la page projet et dans Bâtiments
- [ ] Ⓟ Piste A : importer les poses de Meshroom (`cameras.sfm`), si le besoin se confirme
**Tests à passer avant de clore le lot :**
- [ ] Un projet Photos client affiche le message
- [ ] Piste A : un `cameras.sfm` importé produit `has_resolved_poses = true`
**Vérifié le :**

## Lot 4 — Jeton hors des URL et gabarit front   ·   statut : à faire
**Objectif :** même comportement que le gabarit corrigé (T3), sans JWT dans les URL (T4).
**Tâches :**
- [ ] `?token=` vers `storage` (`core/api.service.ts:361`) → `fetch` + `Blob` ; mettre à jour la docstring `mediaUrl` (l. 349-355), périmée
- [ ] Polices en chemin relatif (`styles.scss:3-11`) ; `tokenBases()` ; PKCE. Le `redirectUri` est déjà sur la page courante.
**Tests à passer avant de clore le lot :**
- [ ] Aucune requête porte `token=` ; les WOFF2 répondent 200 ; `code_challenge` est présent à la connexion
**Vérifié le :**

## Lot 5 — Ménage et chemin à portes   ·   statut : à faire
**Objectif :** code juste, déploiement par le moteur (T1).
**Tâches :**
- [ ] Renommer le service `worker` en `atelier-3d-worker` (le `container_name` est déjà bon)
- [ ] Corriger le commentaire de `models.py:82` (champ `Photo.calibration_points` inexistant) ; retirer `Department` et `UserRecord` du gabarit s'ils sont morts
- [ ] Ⓟ Rattraper les fichiers et partages `atelier-3d-project-<id>` des projets supprimés avant `f203b47`
- [ ] `README.md` : modes d'initialisation, modules, ressources (scratch, temps de calcul)
**Tests à passer avant de clore le lot :**
- [ ] `validate-app.sh atelier-3d --quality-only` vert (après correction du faux positif `[service-account]` côté cadriciel)
- [ ] Cloisonnement `ok` par le moteur
**Vérifié le :**
