import { Routes } from '@angular/router';
import { HomeComponent }    from './pages/home/home.component';
import { ProfileComponent } from './pages/profile/profile.component';
import { ProjectsListComponent } from './pages/projects/projects-list.component';
import { ProjectDetailComponent } from './pages/projects/project-detail.component';

export const routes: Routes = [
  { path: '',                component: HomeComponent },
  { path: 'profile',         component: ProfileComponent },
  { path: 'projects',        component: ProjectsListComponent },
  { path: 'projects/:id',    component: ProjectDetailComponent },
  { path: '**',               redirectTo: '' },
];
