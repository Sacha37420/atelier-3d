/**
 * Test end-to-end du cloisonnement de cette app (voir CLAUDE.md, section
 * "Tests end-to-end" pour le formalisme complet et comment ces tests sont
 * exécutés). Automatise la vérification manuelle documentée dans CLAUDE.md
 * (section "Sécurité — cloisonnement des applications") : après tout
 * changement de cloisonnement, tester dans les deux sens.
 *
 * Ce fichier est le SEUL fichier de test Playwright de cette app — un
 * répertoire e2e/ avec plusieurs specs n'est pas le format attendu par le
 * runner partagé (runner/lib/resolve-app.js pointe sur ce dossier entier,
 * mais scripts/setup_unit.sh catalogue un fichier par test, pas un dossier).
 *
 * Entièrement piloté par variables d'environnement, injectées par le runner
 * au moment du run réel — jamais de credentials en dur ici :
 *   E2E_APP_URL            URL publique de cette app (https://<domaine><chemin>/)
 *   E2E_MEMBER_USER/PASSWORD     compte toujours membre de tous les groupes du lab
 *   E2E_OUTSIDER_USER/PASSWORD   compte membre d'aucun groupe
 *
 * N'ajoutez PAS de test qui dépend du contenu spécifique de cette app (une
 * page, un texte précis) — ce fichier doit rester copiable tel quel d'une
 * app à l'autre. Pour des tests spécifiques à l'app, un AUTRE fichier est
 * hors formalisme actuel (voir CLAUDE.md avant d'en ajouter un).
 */
import { test, expect, type BrowserContext, type Page } from '@playwright/test';

const APP_URL = process.env['E2E_APP_URL'] ?? '';
const MEMBER_USER = process.env['E2E_MEMBER_USER'] ?? '';
const MEMBER_PASSWORD = process.env['E2E_MEMBER_PASSWORD'] ?? '';
const OUTSIDER_USER = process.env['E2E_OUTSIDER_USER'] ?? '';
const OUTSIDER_PASSWORD = process.env['E2E_OUTSIDER_PASSWORD'] ?? '';

test.beforeAll(() => {
  if (!APP_URL || !MEMBER_USER || !MEMBER_PASSWORD || !OUTSIDER_USER || !OUTSIDER_PASSWORD) {
    throw new Error(
      'Variables E2E_* manquantes — ce spec doit être lancé par le runner ' +
      '(runner/server.js), pas exécuté isolément sans environnement.',
    );
  }
});

/** Remplit le formulaire de login Keycloak standard et soumet. */
async function loginAt(page: Page, username: string, password: string): Promise<void> {
  await page.goto(APP_URL);
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(password);
  await page.locator('#kc-login').click();
}

/**
 * Vrai si la page n'a PAS atterri sur l'app (refus explicite du flow
 * require-<client>, OU un formulaire de login — les deux comptent comme
 * "pas admis").
 *
 * Ne vérifie PAS spécifiquement le texte "Access denied" : vérifié en
 * conditions réelles (2026-07-27, carto-lab) qu'un login refusé n'établit
 * JAMAIS de cookie KEYCLOAK_SESSION côté Keycloak (seulement des cookies
 * transitoires de flow, ex. AUTH_SESSION_ID) — donc revisiter l'app après un
 * refus republie un formulaire de login, pas la page "Access denied". C'est
 * le comportement Keycloak attendu (une session n'est émise qu'après succès
 * complet du flow, gate inclus), pas un bug : un non-membre sans AUCUN accès
 * ailleurs dans le lab ne peut jamais obtenir de session à faire "rejouer"
 * ici. Cette fonction couvre donc la régression réellement testable
 * génériquement — atterrir sur l'app, jamais — pas la reproduction exacte du
 * contournement historique (qui suppose une session déjà valide, donc un
 * compte ayant un accès légitime AILLEURS dans le lab — non simulable sans
 * muter des appartenances de groupe en direct pendant le test).
 */
async function isDenied(page: Page): Promise<boolean> {
  await page.waitForLoadState('networkidle');
  return !page.url().startsWith(APP_URL);
}

test('membre du groupe requis : accède à l\'app', async ({ page }) => {
  await loginAt(page, MEMBER_USER, MEMBER_PASSWORD);
  await page.waitForLoadState('networkidle');
  await expect(page).toHaveURL(new RegExp(`^${APP_URL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
});

// Tests 2 et 3 partagent UN SEUL contexte navigateur (donc les mêmes cookies)
// pour vérifier qu'une revisite après un premier refus n'atterrit toujours
// pas sur l'app — voir le commentaire d'isDenied() pour ce que ça couvre
// exactement (et sa limite face au contournement historique documenté dans
// CLAUDE.md). mode:'serial' impose l'ordre.
test.describe.configure({ mode: 'serial' });
test.describe('non-membre', () => {
  let context: BrowserContext;

  test.beforeAll(async ({ browser }) => {
    context = await browser.newContext();
  });

  test.afterAll(async () => {
    await context.close();
  });

  test('non-membre : refusé au premier login', async () => {
    const page = await context.newPage();
    await loginAt(page, OUTSIDER_USER, OUTSIDER_PASSWORD);
    expect(await isDenied(page)).toBe(true);
    await page.close();
  });

  test('non-membre avec session SSO déjà active : refusé aussi', async () => {
    // Nouvelle page, MÊME contexte donc mêmes cookies — pas de nouveau login :
    // simule un F5/nouvel onglet après le login refusé ci-dessus.
    const page = await context.newPage();
    await page.goto(APP_URL);
    expect(await isDenied(page)).toBe(true);
    await page.close();
  });
});
