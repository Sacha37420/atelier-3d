import { Component, OnDestroy, ViewChild, inject, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  ApiService, Project, ProjectDetail, Job, ExportFormat, PrintQuaternion,
} from '../../core/api.service';
import { MeshViewerComponent, CalibrationMeasurement } from '../../components/mesh-viewer/mesh-viewer.component';

const POLL_INTERVAL_MS = 2000;

/**
 * Page Impression 3D (Lot 2) — indépendante de la page Reconstruction (Lot 1) :
 * réutilisable sur n'importe quel projet ayant déjà un maillage, pas seulement
 * celui qu'on vient de reconstruire. `/impression` affiche un sélecteur de
 * projet ; `/impression/:id` affiche l'atelier complet (calibration,
 * orientation, réparation, export) pour le projet choisi.
 */
@Component({
  selector: 'app-impression',
  standalone: true,
  imports: [FormsModule, RouterLink, MeshViewerComponent],
  templateUrl: './impression.component.html',
  styleUrl: './impression.component.scss',
})
export class ImpressionComponent implements OnDestroy {
  private api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  readonly projects = signal<Project[]>([]);
  readonly selectedProjectId = signal<number | null>(null);
  readonly project = signal<ProjectDetail | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  readonly calibrationMode = signal(false);
  readonly pendingMeasurement = signal<CalibrationMeasurement | null>(null);
  realDistanceMeters: number | null = null;

  readonly repairing = signal(false);
  readonly repairTargetMode = signal<'triangles' | 'size'>('triangles');
  repairTargetTriangles: number | null = null;
  repairTargetSizeMb: number | null = null;

  readonly orientationMode = signal(false);
  readonly orientSuggesting = signal(false);
  readonly currentOrientation = signal<PrintQuaternion>([0, 0, 0, 1]);
  readonly exportFormat = signal<ExportFormat>('stl');
  readonly exporting = signal(false);

  @ViewChild(MeshViewerComponent) private viewer?: MeshViewerComponent;

  private pollHandle?: ReturnType<typeof setInterval>;

  readonly selectableProjects = computed(() => this.projects().filter((p) => p.has_mesh));

  readonly latestMesh = computed(() => {
    const meshes = this.project()?.meshes ?? [];
    return meshes.length > 0 ? meshes[0] : null;
  });

  readonly meshViewerUrl = computed(() => {
    const mesh = this.latestMesh();
    return mesh?.gltf_file ? this.api.mediaUrl(mesh.gltf_file) : null;
  });

  // Le verrou global (un seul job lourd à la fois, tous modules/projets confondus)
  // n'est visible ici qu'à travers les jobs DE CE projet — un job actif sur un
  // autre projet ne s'affiche pas en amont, mais tout lancement recevra quand
  // même le 409 du backend (géré dans launchRepair()).
  readonly activeJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => j.status === 'PENDING' || j.status === 'RUNNING') ?? null;
  });

  readonly activeRepairJob = computed(() => {
    const job = this.activeJob();
    return job?.kind === 'REPAIR' ? job : null;
  });

  readonly lastFinishedJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => j.status === 'DONE' || j.status === 'ERROR') ?? null;
  });

  constructor() {
    this.api.getProjects().subscribe({ next: (list) => this.projects.set(list) });
    this.route.paramMap.subscribe((params) => {
      const idParam = params.get('id');
      this.stopPoll();
      if (idParam) {
        this.selectedProjectId.set(Number(idParam));
        this.resetMeshState();
        this.reload();
      } else {
        this.selectedProjectId.set(null);
        this.project.set(null);
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
    this.router.navigate(['/impression', id]);
  }

  reload(): void {
    const id = this.selectedProjectId();
    if (!id) return;
    this.loading.set(true);
    this.api.getProject(id).subscribe({
      next: (project) => {
        this.project.set(project);
        this.loading.set(false);
        const active = this.activeJob();
        if (active) this.startPoll(active.id);
      },
      error: () => { this.error.set("Impossible de charger le projet."); this.loading.set(false); },
    });
  }

  private resetMeshState(): void {
    this.calibrationMode.set(false);
    this.pendingMeasurement.set(null);
    this.realDistanceMeters = null;
    this.orientationMode.set(false);
    this.currentOrientation.set([0, 0, 0, 1]);
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

  // ── Calibration d'échelle (deux points cliqués dans le viewer 3D) ──────────
  toggleCalibration(): void {
    this.calibrationMode.update((v) => !v);
    this.pendingMeasurement.set(null);
    this.realDistanceMeters = null;
  }

  onMeasured(measurement: CalibrationMeasurement): void {
    this.pendingMeasurement.set(measurement);
  }

  saveCalibration(): void {
    const measurement = this.pendingMeasurement();
    const id = this.selectedProjectId();
    if (!measurement || !id || !this.realDistanceMeters || this.realDistanceMeters <= 0) return;
    const scale = this.realDistanceMeters / measurement.meshDistance;
    this.api.updateProject(id, { scale_meters_per_unit: scale }).subscribe({
      next: () => {
        this.calibrationMode.set(false);
        this.pendingMeasurement.set(null);
        this.realDistanceMeters = null;
        this.reload();
      },
    });
  }

  // ── Réparation watertight ────────────────────────────────────────────────
  launchRepair(): void {
    const id = this.selectedProjectId();
    if (!id) return;
    const opts: { target_triangles?: number; target_size_mb?: number } = {};
    if (this.repairTargetMode() === 'triangles' && this.repairTargetTriangles) {
      opts.target_triangles = this.repairTargetTriangles;
    } else if (this.repairTargetMode() === 'size' && this.repairTargetSizeMb) {
      opts.target_size_mb = this.repairTargetSizeMb;
    }
    this.repairing.set(true);
    this.api.launchRepair(id, opts).subscribe({
      next: (job) => { this.repairing.set(false); this.reload(); this.startPoll(job.id); },
      error: (err) => {
        this.repairing.set(false);
        this.error.set(
          err?.status === 409
            ? "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, tous modules confondus."
            : "Échec du lancement de la réparation.",
        );
      },
    });
  }

  // ── Orientation d'impression ─────────────────────────────────────────────
  toggleOrientation(): void {
    this.orientationMode.update((v) => !v);
  }

  suggestOrientation(): void {
    const mesh = this.latestMesh();
    if (!mesh) return;
    this.orientSuggesting.set(true);
    this.api.getAutoOrient(mesh.id).subscribe({
      next: (s) => {
        this.orientSuggesting.set(false);
        this.viewer?.applySuggestedOrientation(s.quaternion);
      },
      error: () => {
        this.orientSuggesting.set(false);
        this.error.set("Échec du calcul d'orientation.");
      },
    });
  }

  onOrientationChanged(quaternion: PrintQuaternion): void {
    this.currentOrientation.set(quaternion);
  }

  // ── Export STL/3MF ───────────────────────────────────────────────────────
  exportMesh(): void {
    const mesh = this.latestMesh();
    const project = this.project();
    if (!mesh || !project || !project.has_scale) return;

    this.exporting.set(true);
    this.api.exportMesh(mesh.id, this.exportFormat(), this.currentOrientation()).subscribe({
      next: (blob) => {
        this.exporting.set(false);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${project.name}_v${mesh.version}.${this.exportFormat()}`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: (err) => {
        this.exporting.set(false);
        this.error.set(
          err?.status === 409
            ? "Échelle non calibrée — impossible d'exporter tant que le maillage n'a pas d'échelle métrique connue."
            : "Échec de l'export.",
        );
      },
    });
  }
}
