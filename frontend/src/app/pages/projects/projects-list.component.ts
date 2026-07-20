import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiService, Project, ProjectType } from '../../core/api.service';

@Component({
  selector: 'app-projects-list',
  standalone: true,
  imports: [FormsModule, RouterLink],
  templateUrl: './projects-list.component.html',
  styleUrl: './projects-list.component.scss',
})
export class ProjectsListComponent {
  private api = inject(ApiService);

  readonly projects = signal<Project[]>([]);
  readonly loading = signal(true);
  readonly creating = signal(false);
  readonly error = signal<string | null>(null);

  newName = '';
  newType: ProjectType = 'objet';

  constructor() {
    this.reload();
  }

  reload(): void {
    this.loading.set(true);
    this.api.getProjects().subscribe({
      next: (projects) => { this.projects.set(projects); this.loading.set(false); },
      error: () => { this.error.set("Impossible de charger les projets."); this.loading.set(false); },
    });
  }

  createProject(): void {
    const name = this.newName.trim();
    if (!name) return;
    this.creating.set(true);
    this.api.createProject({ name, project_type: this.newType }).subscribe({
      next: () => { this.newName = ''; this.creating.set(false); this.reload(); },
      error: () => { this.error.set("Échec de la création du projet."); this.creating.set(false); },
    });
  }
}
