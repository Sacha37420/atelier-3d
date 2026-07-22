import { Component, inject, signal, computed, OnDestroy } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  ApiService, Project, ProjectDetail, Photo, PhotoLabel, SemanticClass, SemanticClassName,
  SEMANTIC_CLASS_LABELS, FacadeEstimate, Job,
} from '../../core/api.service';
import { MeshViewerComponent } from '../../components/mesh-viewer/mesh-viewer.component';

const POLL_INTERVAL_MS = 2000;
const CLASS_NAMES: SemanticClassName[] = ['mur', 'fenetre', 'porte', 'toit'];

/**
 * Page Bâtiments (Lot 4) : même pattern picker que Impression/Mouvements
 * (réutilisable sur n'importe quel projet ayant déjà des poses caméra
 * résolues, cf. to_do_3D.md — découplée de la page Reconstruction). Deux
 * volets : labellisation assistée (clic sur des régions FastSAM d'une photo,
 * quelques régions par classe) puis lancement du job SEGMENTATION_FACADE
 * (rétro-projection multi-vues + régularisation) une fois au moins une
 * région labellisée.
 */
@Component({
  selector: 'app-batiments',
  standalone: true,
  imports: [FormsModule, RouterLink, MeshViewerComponent],
  templateUrl: './batiments.component.html',
  styleUrl: './batiments.component.scss',
})
export class BatimentsComponent implements OnDestroy {
  private api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  readonly classNames = CLASS_NAMES;
  readonly classLabels = SEMANTIC_CLASS_LABELS;

  readonly projects = signal<Project[]>([]);
  readonly selectedProjectId = signal<number | null>(null);
  readonly project = signal<ProjectDetail | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  readonly semanticClasses = signal<SemanticClass[]>([]);

  // ── Labellisation assistée ────────────────────────────────────────────────
  readonly selectedPhotoId = signal<number | null>(null);
  readonly loadingRegions = signal(false);
  readonly photoLabels = signal<PhotoLabel[]>([]);
  readonly selectedClass = signal<SemanticClassName>('mur');

  // ── Job SEGMENTATION_FACADE ───────────────────────────────────────────────
  readonly estimate = signal<FacadeEstimate | null>(null);
  readonly launching = signal(false);

  private pollHandle?: ReturnType<typeof setInterval>;

  readonly selectableProjects = computed(() => this.projects().filter((p) => p.has_resolved_poses));

  readonly latestMesh = computed(() => {
    const meshes = this.project()?.meshes ?? [];
    return meshes.length > 0 ? meshes[0] : null;
  });

  readonly meshViewerUrl = computed(() => {
    const mesh = this.latestMesh();
    return mesh?.gltf_file ? this.api.mediaUrl(mesh.gltf_file) : null;
  });

  readonly resolvedPhotos = computed(() => (this.project()?.photos ?? []).filter((p) => p.pose_resolved));

  readonly selectedPhoto = computed(() => {
    const id = this.selectedPhotoId();
    return this.project()?.photos.find((p) => p.id === id) ?? null;
  });

  readonly activeJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => j.status === 'PENDING' || j.status === 'RUNNING') ?? null;
  });

  // Le verrou global n'autorise qu'un seul job actif tous modules confondus —
  // n'afficher la barre de progression ici que pour un job SEGMENTATION_FACADE
  // (cf. limite connue documentée dans la mémoire atelier-3d-navigation-par-lot).
  readonly activeFacadeJob = computed(() => {
    const job = this.activeJob();
    return job?.kind === 'SEGMENTATION_FACADE' ? job : null;
  });

  readonly lastFinishedFacadeJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => j.kind === 'SEGMENTATION_FACADE' && (j.status === 'DONE' || j.status === 'ERROR')) ?? null;
  });

  constructor() {
    this.api.getProjects().subscribe({ next: (list) => this.projects.set(list) });
    this.route.paramMap.subscribe((params) => {
      const idParam = params.get('id');
      if (idParam) {
        this.selectedProjectId.set(Number(idParam));
        this.reload();
      } else {
        this.selectedProjectId.set(null);
        this.project.set(null);
        this.semanticClasses.set([]);
      }
    });
  }

  ngOnDestroy(): void {
    this.stopPoll();
  }

  mediaUrl(path: string | null): string {
    return this.api.mediaUrl(path);
  }

  selectProject(id: number | null): void {
    if (!id) return;
    this.router.navigate(['/batiments', id]);
  }

  reload(): void {
    const id = this.selectedProjectId();
    if (!id) return;
    this.loading.set(true);
    this.selectedPhotoId.set(null);
    this.photoLabels.set([]);
    this.api.getProject(id).subscribe({
      next: (project) => {
        this.project.set(project);
        this.loading.set(false);
        this.refreshEstimate();
        const mesh = project.meshes[0];
        if (mesh) {
          this.api.getSemanticClasses(mesh.id).subscribe({ next: (cls) => this.semanticClasses.set(cls) });
        } else {
          this.semanticClasses.set([]);
        }
        const active = this.activeJob();
        if (active) this.startPoll(active.id);
      },
      error: () => { this.error.set("Impossible de charger le projet."); this.loading.set(false); },
    });
  }

  // ── Labellisation assistée ────────────────────────────────────────────────
  selectClass(name: SemanticClassName): void {
    this.selectedClass.set(name);
  }

  openPhotoForLabeling(photo: Photo): void {
    this.error.set(null);
    this.selectedPhotoId.set(photo.id);
    this.photoLabels.set([]);
    if (photo.region_overlay) {
      this.reloadLabels(photo.id);
      return;
    }
    this.loadingRegions.set(true);
    this.api.getPhotoRegions(photo.id).subscribe({
      next: (updated) => {
        this.loadingRegions.set(false);
        this.patchPhotoInProject(updated);
        this.reloadLabels(photo.id);
      },
      error: (err) => {
        this.loadingRegions.set(false);
        this.error.set(err?.error?.detail ?? "Échec de la segmentation 2D de la photo.");
      },
    });
  }

  closeLabeling(): void {
    this.selectedPhotoId.set(null);
    this.photoLabels.set([]);
  }

  onPhotoClick(ev: MouseEvent): void {
    const photo = this.selectedPhoto();
    if (!photo || !photo.region_overlay) return;
    const target = ev.currentTarget as HTMLElement;
    const rect = target.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width;
    const y = (ev.clientY - rect.top) / rect.height;
    this.api.createPhotoLabel(photo.id, x, y, this.selectedClass()).subscribe({
      next: () => this.reloadLabels(photo.id),
      error: (err) => this.error.set(err?.error?.detail ?? "Échec de la pose du label."),
    });
  }

  deleteLabel(label: PhotoLabel): void {
    this.api.deletePhotoLabel(label.id).subscribe({ next: () => this.reloadLabels(label.photo) });
  }

  private reloadLabels(photoId: number): void {
    this.api.getPhotoLabels(photoId).subscribe({ next: (labels) => this.photoLabels.set(labels) });
  }

  private patchPhotoInProject(photo: Photo): void {
    const project = this.project();
    if (!project) return;
    this.project.set({ ...project, photos: project.photos.map((p) => (p.id === photo.id ? photo : p)) });
  }

  // ── Job SEGMENTATION_FACADE ───────────────────────────────────────────────
  private refreshEstimate(): void {
    const id = this.selectedProjectId();
    if (!id) return;
    this.api.getFacadeEstimate(id).subscribe({ next: (e) => this.estimate.set(e) });
  }

  formatDuration(seconds: number): string {
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    if (h > 0) return `${h}h${m.toString().padStart(2, '0')}`;
    return `${m} min`;
  }

  launchFacade(): void {
    const project = this.project();
    const est = this.estimate();
    if (!project) return;

    const warning = est?.warning_threshold_exceeded
      ? "\n⚠ Cette estimation dépasse 2 heures (nombreuses photos / scénario drone) — vérifier avant de continuer."
      : '';
    const confirmed = window.confirm(
      `Lancer la segmentation bâtiment sur ${est?.n_photos ?? '?'} photo(s) à pose résolue ?\n` +
      `Durée estimée : ${est ? this.formatDuration(est.estimated_seconds) : '?'}.${warning}\n\n` +
      `Un seul job lourd peut tourner à la fois pour tout l'atelier.`,
    );
    if (!confirmed) return;

    this.launching.set(true);
    this.api.launchFacadeSegmentation(project.id).subscribe({
      next: (job) => { this.launching.set(false); this.reload(); this.startPoll(job.id); },
      error: (err) => {
        this.launching.set(false);
        this.error.set(
          err?.status === 409
            ? "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, tous modules confondus."
            : (err?.error?.detail ?? "Échec du lancement de la segmentation."),
        );
      },
    });
  }

  private startPoll(jobId: number): void {
    this.stopPoll();
    this.pollHandle = setInterval(() => {
      this.api.getJob(jobId).subscribe({
        next: (job) => {
          if (job.status === 'DONE' || job.status === 'ERROR') {
            this.stopPoll();
            this.reload();
          } else {
            this.patchJobInProject(job);
          }
        },
      });
    }, POLL_INTERVAL_MS);
  }

  private stopPoll(): void {
    if (this.pollHandle) { clearInterval(this.pollHandle); this.pollHandle = undefined; }
  }

  private patchJobInProject(job: Job): void {
    const project = this.project();
    if (!project) return;
    this.project.set({ ...project, jobs: project.jobs.map((j) => (j.id === job.id ? job : j)) });
  }
}
