import { Routes } from '@angular/router';
import { ProjectsListComponent } from './pages/projects/projects-list.component';
import { ProjectDetailComponent } from './pages/projects/project-detail.component';
import { ImpressionComponent } from './pages/impression/impression.component';
import { MouvementsComponent } from './pages/mouvements/mouvements.component';
import { BatimentsComponent } from './pages/batiments/batiments.component';

// Pas de route /conception séparée : la conception CAO est une méthode
// d'initialisation du maillage parmi d'autres (avec les photos), choisie
// DANS la page d'un projet (/projects/:id) — pas une destination de menu à
// part, cf. project-detail.component.ts.
export const routes: Routes = [
  { path: '',                redirectTo: 'projects', pathMatch: 'full' },
  { path: 'projects',        component: ProjectsListComponent },
  { path: 'projects/:id',    component: ProjectDetailComponent },
  { path: 'impression',      component: ImpressionComponent },
  { path: 'impression/:id',  component: ImpressionComponent },
  { path: 'mouvements',      component: MouvementsComponent },
  { path: 'mouvements/:id',  component: MouvementsComponent },
  { path: 'batiments',       component: BatimentsComponent },
  { path: 'batiments/:id',   component: BatimentsComponent },
  { path: '**',               redirectTo: 'projects' },
];
