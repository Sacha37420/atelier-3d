import { Component, OnDestroy, inject, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { ApiService, ProjectDetail, Preset, ReconstructionEstimate, Job } from '../../core/api.service';
import { MeshViewerComponent } from '../../components/mesh-viewer/mesh-viewer.component';

const POLL_INTERVAL_MS = 2000;
const PRESET_LABELS: Record<Preset, string> = { rapide: 'Rapide', equilibre: 'Équilibré', precis: 'Précis' };

/**
 * Page Reconstruction (Lot 1) : dépôt photos/vidéo, lancement du job
 * RECONSTRUCTION, aperçu du maillage résultant. La suite du pipeline
 * (calibration, réparation, orientation, export — Lot 2) vit sur sa propre
 * page réutilisable (`/impression/:id`, cf. ImpressionComponent) plutôt
 * qu'ici, pour rester découplée d'un projet précis.
 */
@Component({
  selector: 'app-project-detail',
  standalone: true,
  imports: [FormsModule, RouterLink, MeshViewerComponent],
  templateUrl: './project-detail.component.html',
  styleUrl: './project-detail.component.scss',
})
export class ProjectDetailComponent implements OnDestroy {
  private api = inject(ApiService);
  private route = inject(ActivatedRoute);

  readonly presetLabels = PRESET_LABELS;
  readonly presets: Preset[] = ['rapide', 'equilibre', 'precis'];

  readonly project = signal<ProjectDetail | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly uploading = signal(false);
  readonly dragOver = signal(false);
  readonly videoFps = 2;

  readonly selectedPreset = signal<Preset>('equilibre');
  readonly estimate = signal<ReconstructionEstimate | null>(null);
  readonly launching = signal(false);

  private projectId!: number;
  private pollHandle?: ReturnType<typeof setInterval>;

  readonly latestMesh = computed(() => {
    const meshes = this.project()?.meshes ?? [];
    return meshes.length > 0 ? meshes[0] : null;
  });

  readonly meshViewerUrl = computed(() => {
    const mesh = this.latestMesh();
    return mesh?.gltf_file ? this.api.mediaUrl(mesh.gltf_file) : null;
  });

  readonly activeJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => j.status === 'PENDING' || j.status === 'RUNNING') ?? null;
  });

  // Le verrou global n'autorise qu'un seul job actif tous modules confondus —
  // la carte Reconstruction ne doit afficher sa barre de progression que pour
  // un job RECONSTRUCTION (un job REPAIR lancé depuis la page Impression est
  // du même verrou mais n'a rien à voir avec cette carte).
  readonly activeReconstructionJob = computed(() => {
    const job = this.activeJob();
    return job?.kind === 'RECONSTRUCTION' ? job : null;
  });

  readonly lastFinishedJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => j.status === 'DONE' || j.status === 'ERROR') ?? null;
  });

  constructor() {
    this.projectId = Number(this.route.snapshot.paramMap.get('id'));
    this.reload();
  }

  ngOnDestroy(): void {
    this.stopPoll();
  }

  mediaUrl(path: string | null): string {
    return this.api.mediaUrl(path);
  }

  reload(): void {
    this.loading.set(true);
    this.api.getProject(this.projectId).subscribe({
      next: (project) => {
        this.project.set(project);
        this.loading.set(false);
        this.refreshEstimate();
        const active = this.activeJob();
        if (active) this.startPoll(active.id);
      },
      error: () => { this.error.set("Impossible de charger le projet."); this.loading.set(false); },
    });
  }

  // ── Upload photos (glisser-déposer) ────────────────────────────────────────
  onDragOver(ev: DragEvent): void { ev.preventDefault(); this.dragOver.set(true); }
  onDragLeave(): void { this.dragOver.set(false); }

  onDrop(ev: DragEvent): void {
    ev.preventDefault();
    this.dragOver.set(false);
    const files = Array.from(ev.dataTransfer?.files ?? []);
    const images = files.filter((f) => f.type.startsWith('image/'));
    const videos = files.filter((f) => f.type.startsWith('video/'));
    if (images.length) this.uploadPhotos(images);
    for (const v of videos) this.uploadVideo(v);
  }

  onPhotoInputChange(ev: Event): void {
    const files = Array.from((ev.target as HTMLInputElement).files ?? []);
    if (files.length) this.uploadPhotos(files);
    (ev.target as HTMLInputElement).value = '';
  }

  onVideoInputChange(ev: Event): void {
    const files = Array.from((ev.target as HTMLInputElement).files ?? []);
    if (files[0]) this.uploadVideo(files[0]);
    (ev.target as HTMLInputElement).value = '';
  }

  private uploadPhotos(files: File[]): void {
    this.uploading.set(true);
    this.api.uploadPhotos(this.projectId, files).subscribe({
      next: () => { this.uploading.set(false); this.reload(); },
      error: () => { this.error.set("Échec de l'envoi des photos."); this.uploading.set(false); },
    });
  }

  private uploadVideo(file: File): void {
    this.uploading.set(true);
    this.api.uploadVideo(this.projectId, file, this.videoFps).subscribe({
      next: () => { this.uploading.set(false); this.reload(); },
      error: () => {
        this.error.set("Échec de l'extraction des frames de la vidéo.");
        this.uploading.set(false);
      },
    });
  }

  deletePhoto(photoId: number): void {
    this.api.deletePhoto(this.projectId, photoId).subscribe({ next: () => this.reload() });
  }

  // ── Reconstruction : preset, estimation, lancement ─────────────────────────
  selectPreset(preset: Preset): void {
    this.selectedPreset.set(preset);
    this.refreshEstimate();
  }

  private refreshEstimate(): void {
    if ((this.project()?.photo_count ?? 0) === 0) { this.estimate.set(null); return; }
    this.api.getReconstructionEstimate(this.projectId, this.selectedPreset()).subscribe({
      next: (e) => this.estimate.set(e),
    });
  }

  formatDuration(seconds: number): string {
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    if (h > 0) return `${h}h${m.toString().padStart(2, '0')}`;
    return `${m} min`;
  }

  launchReconstruction(): void {
    const est = this.estimate();
    const project = this.project();
    if (!project || project.photo_count < 3) return;

    const warning = est?.warning_threshold_exceeded
      ? "\n⚠ Cette estimation dépasse 2 heures — vérifier le preset avant de continuer."
      : '';
    const scaleWarning = project.has_scale ? '' : "\n⚠ Échelle non calibrée : le maillage sortira à une échelle arbitraire.";
    const confirmed = window.confirm(
      `Lancer la reconstruction (preset ${this.presetLabels[this.selectedPreset()]}) sur ` +
      `${project.photo_count} photos ?\nDurée estimée : ${est ? this.formatDuration(est.estimated_seconds) : '?'}.` +
      `${warning}${scaleWarning}\n\nUn seul job lourd peut tourner à la fois pour tout l'atelier.`,
    );
    if (!confirmed) return;

    this.launching.set(true);
    this.api.launchReconstruction(this.projectId, this.selectedPreset()).subscribe({
      next: (job) => { this.launching.set(false); this.reload(); this.startPoll(job.id); },
      error: (err) => {
        this.launching.set(false);
        this.error.set(
          err?.status === 409
            ? "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, tous modules confondus."
            : "Échec du lancement de la reconstruction.",
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
    this.project.set({
      ...project,
      jobs: project.jobs.map((j) => (j.id === job.id ? job : j)),
    });
  }
}
