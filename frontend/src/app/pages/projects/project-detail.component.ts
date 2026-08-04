import { Component, OnDestroy, inject, signal, computed } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import {
  ApiService, ProjectDetail, Preset, ReconstructionEstimate, Job,
  CadSketch, CadPlane, CadEntity, CadEntityType,
  CadOperation, CadOperationType, CAD_OPERATION_LABELS,
  Project, CadAssemblyInstance, CadAssemblyConstraint, ConstraintType, CONSTRAINT_TYPE_LABELS,
  OutdatedInstanceInfo,
} from '../../core/api.service';
import { MeshViewerComponent } from '../../components/mesh-viewer/mesh-viewer.component';

const POLL_INTERVAL_MS = 2000;
const PRESET_LABELS: Record<Preset, string> = { rapide: 'Rapide', equilibre: 'Équilibré', precis: 'Précis' };

export type InitMode = 'photos' | 'cao' | 'photos_client';

/**
 * Page Projet (Lot 1 + Lot 5) : un `Project` n'a qu'une seule façon de
 * démarrer réellement en cours d'usage, mais TROIS méthodes équivalentes pour
 * initialiser son maillage — photos (Reconstruction serveur, Lot 1), photos
 * client (photogrammétrie faite par le client avec son propre logiciel, puis
 * import du résultat) ou conception CAO manuelle (sketchs/opérations, Lot
 * 5.1) : les trois produisent un `Mesh` standard, ensuite utilisable à
 * l'identique par Impression/Mouvements/Bâtiments. Les trois méthodes vivent
 * ICI, dans la même page projet (choix explicite via `initMode`), plutôt que
 * sur des pages/menu séparés — ce ne sont pas des fonctionnalités
 * différentes, juste trois façons de peupler le même `Project`/`Mesh`.
 *
 * La suite du pipeline une fois un maillage obtenu (calibration, réparation,
 * orientation, export — Lot 2) vit sur sa propre page réutilisable
 * (`/impression/:id`), pour rester découplée de la méthode d'origine.
 */
@Component({
  selector: 'app-project-detail',
  standalone: true,
  imports: [FormsModule, RouterLink, MeshViewerComponent, DecimalPipe],
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

  // ── Choix de la méthode d'initialisation du maillage ─────────────────────
  readonly initMode = signal<InitMode>('photos');
  setInitMode(mode: InitMode): void { this.initMode.set(mode); }

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
  // ces deux computed ne servent qu'à savoir laquelle des deux cartes
  // (Reconstruction / Construction CAO) doit afficher sa propre barre de
  // progression pour CE job précis.
  readonly activeReconstructionJob = computed(() => {
    const job = this.activeJob();
    return job?.kind === 'RECONSTRUCTION' ? job : null;
  });

  readonly activeCadBuildJob = computed(() => {
    const job = this.activeJob();
    return job?.kind === 'CAD_BUILD' ? job : null;
  });

  readonly activeMeshImportJob = computed(() => {
    const job = this.activeJob();
    return job?.kind === 'MESH_IMPORT' ? job : null;
  });

  readonly lastFinishedMeshImportJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => (j.status === 'DONE' || j.status === 'ERROR') && j.kind === 'MESH_IMPORT') ?? null;
  });

  readonly lastFinishedJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => j.status === 'DONE' || j.status === 'ERROR') ?? null;
  });

  readonly lastFinishedCadBuildJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => (j.status === 'DONE' || j.status === 'ERROR') && j.kind === 'CAD_BUILD') ?? null;
  });

  // ── Conception CAO (Lot 5.1) ─────────────────────────────────────────────
  readonly sketches = signal<CadSketch[]>([]);
  readonly operations = signal<CadOperation[]>([]);
  readonly cadOperationTypes = Object.keys(CAD_OPERATION_LABELS) as CadOperationType[];
  readonly operationLabels = CAD_OPERATION_LABELS;

  readonly addingSketch = signal(false);
  newSketchName = '';
  newSketchPlane: CadPlane = 'XY';
  newSketchEntities: CadEntity[] = [];
  newEntityType: CadEntityType = 'circle';
  entityField = {
    startX: 0, startY: 0, midX: 0, midY: 0, endX: 0, endY: 0,
    centerX: 0, centerY: 0, radius: 1,
    pointsText: '', closed: false, thickness: 1,
  };

  readonly addingOperation = signal(false);
  newOperationType: CadOperationType = 'PRIMITIVE_BOX';
  op = {
    length: 20, width: 20, height: 10, radius: 5,
    bottomRadius: 5, topRadius: 3, majorRadius: 10, minorRadius: 2,
    usePlacement: false, posX: 0, posY: 0, posZ: 0, rotX: 0, rotY: 0, rotZ: 0,
    sketchId: null as number | null, mode: 'ADD' as 'ADD' | 'CUT' | 'INTERSECT',
    useTarget: false, targetId: null as number | null,
    amount: 10, both: false,
    axisOriginX: 0, axisOriginY: 0, axisOriginZ: 0,
    axisDirX: 0, axisDirY: 0, axisDirZ: 1, angleDeg: 360,
    sourceAId: null as number | null, sourceBId: null as number | null,
    sourceId: null as number | null, count: 4,
    patRadius: 20, centerX: 0, centerY: 0, centerZ: 0,
    patAxisDirX: 0, patAxisDirY: 0, patAxisDirZ: 1, startAngleDeg: 0,
    stepX: 10, stepY: 0, stepZ: 0,
    faceIndex: 0, module: 1.5, teethNumber: 20, pressureAngle: 20, gearWidth: 10, internal: false,
  };

  readonly buildDeflectionLinear = signal(0.1);
  readonly buildDeflectionAngular = signal(0.3);
  readonly building = signal(false);

  // ── Assemblage (Lot 5.2) ──────────────────────────────────────────────────
  readonly isAssembly = computed(() => this.project()?.project_type === 'assembly');

  readonly subParts = signal<Project[]>([]);
  readonly addingSubPart = signal(false);
  newSubPartName = '';

  readonly cadInstances = signal<CadAssemblyInstance[]>([]);
  readonly addingInstance = signal(false);
  newInstanceSourceProjectId: number | null = null;
  newInstanceLabel = '';

  readonly cadConstraints = signal<CadAssemblyConstraint[]>([]);
  readonly constraintTypes = Object.keys(CONSTRAINT_TYPE_LABELS) as ConstraintType[];
  readonly constraintTypeLabels = CONSTRAINT_TYPE_LABELS;
  readonly addingConstraint = signal(false);
  newConstraint = {
    type: 'CONCENTRIC' as ConstraintType,
    instanceAId: null as number | null,
    refAKind: 'edge' as 'face' | 'edge' | 'vertex',
    refAIndex: 0,
    instanceBId: null as number | null,
    refBKind: 'edge' as 'face' | 'edge' | 'vertex',
    refBIndex: 0,
    distanceMm: 10,
    angleDeg: 0,
  };

  readonly assembling = signal(false);
  // Lot 5.2 — instances détectées comme périmées (source_mesh plus ancien que
  // la dernière version disponible de leur source_project) lors du dernier
  // essai de résolution refusé (409) : cf. launchAssemble()/resolveAnyway().
  readonly outdatedInstances = signal<OutdatedInstanceInfo[]>([]);

  readonly activeAssembleJob = computed(() => {
    const job = this.activeJob();
    return job?.kind === 'CAD_ASSEMBLE' ? job : null;
  });

  readonly lastFinishedAssembleJob = computed(() => {
    const jobs = this.project()?.jobs ?? [];
    return jobs.find((j) => (j.status === 'DONE' || j.status === 'ERROR') && j.kind === 'CAD_ASSEMBLE') ?? null;
  });

  subPartName(id: number): string {
    return this.subParts().find((p) => p.id === id)?.name ?? `#${id}`;
  }

  instanceLabel(id: number | null): string {
    if (id === null) return '—';
    return this.cadInstances().find((i) => i.id === id)?.label || `instance #${id}`;
  }

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
        if (project.project_type === 'assembly') this.reloadAssembly();
      },
      error: () => { this.error.set("Impossible de charger le projet."); this.loading.set(false); },
    });
    this.api.getCadSketches(this.projectId).subscribe({ next: (list) => this.sketches.set(list) });
    this.api.getCadOperations(this.projectId).subscribe({
      next: (list) => {
        this.operations.set(list);
        // Choix par défaut de l'onglet : celle des trois méthodes qui a déjà
        // des données prend le pas — sans ça, un projet CAO ou Photos client
        // déjà entamé rouvrirait toujours sur l'onglet Photos par défaut. Un
        // job MESH_IMPORT est un signal sans ambiguïté (photo_count seul ne
        // distingue pas Photos de Photos client, les deux modes déposent des
        // Photo) — vérifié avant le heuristique CAO ci-dessous.
        const project = this.project();
        if (project?.jobs.some((j) => j.kind === 'MESH_IMPORT')) {
          this.initMode.set('photos_client');
        } else if (project && project.photo_count === 0 && list.length > 0) {
          this.initMode.set('cao');
        }
      },
    });
  }

  private reloadAssembly(): void {
    this.api.getSubParts(this.projectId).subscribe({ next: (list) => this.subParts.set(list) });
    this.api.getCadInstances(this.projectId).subscribe({ next: (list) => this.cadInstances.set(list) });
    this.api.getCadConstraints(this.projectId).subscribe({ next: (list) => this.cadConstraints.set(list) });
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

  // ── Photos client : import du résultat d'un logiciel de photogrammétrie ──
  readonly importingMesh = signal(false);

  onMeshImportInputChange(ev: Event): void {
    const files = Array.from((ev.target as HTMLInputElement).files ?? []);
    (ev.target as HTMLInputElement).value = '';
    if (!files.length) return;
    this.importingMesh.set(true);
    this.api.uploadMeshImport(this.projectId, files).subscribe({
      next: (job) => { this.importingMesh.set(false); this.reload(); this.startPoll(job.id); },
      error: (err) => {
        this.importingMesh.set(false);
        this.error.set(
          err?.status === 409
            ? "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, tous modules confondus."
            : (err?.error?.detail ?? "Échec de l'import du maillage."),
        );
      },
    });
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

  // ── Conception CAO : sketchs ──────────────────────────────────────────────
  toggleAddSketch(): void {
    this.addingSketch.update((v) => !v);
    this.newSketchName = '';
    this.newSketchPlane = 'XY';
    this.newSketchEntities = [];
  }

  private parsePoints(text: string): [number, number][] {
    return text.split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0)
      .map((line) => {
        const [x, y] = line.split(',').map((v) => Number(v.trim()));
        return [x || 0, y || 0] as [number, number];
      });
  }

  addEntity(): void {
    const f = this.entityField;
    let entity: CadEntity;
    switch (this.newEntityType) {
      case 'segment':
        entity = { type: 'segment', start: [f.startX, f.startY], end: [f.endX, f.endY] };
        break;
      case 'circle':
        entity = { type: 'circle', center: [f.centerX, f.centerY], radius: f.radius };
        break;
      case 'arc':
        entity = { type: 'arc', start: [f.startX, f.startY], mid: [f.midX, f.midY], end: [f.endX, f.endY] };
        break;
      case 'polyline':
        entity = { type: 'polyline', points: this.parsePoints(f.pointsText), closed: f.closed };
        break;
      case 'bspline':
        entity = { type: 'bspline', points: this.parsePoints(f.pointsText), thickness: f.thickness };
        break;
    }
    this.newSketchEntities = [...this.newSketchEntities, entity];
  }

  removeEntity(index: number): void {
    this.newSketchEntities = this.newSketchEntities.filter((_, i) => i !== index);
  }

  entitySummary(e: CadEntity): string {
    switch (e.type) {
      case 'segment': return `segment (${e.start}) → (${e.end})`;
      case 'circle': return `cercle centre (${e.center}) rayon ${e.radius}`;
      case 'arc': return `arc (${e.start}) → (${e.mid}) → (${e.end})`;
      case 'polyline': return `polyligne ${e.points.length} points${e.closed ? ' (fermée)' : ''}`;
      case 'bspline': return `bspline ${e.points.length} points, épaisseur ${e.thickness ?? '—'}`;
    }
  }

  saveSketch(): void {
    if (!this.newSketchName.trim() || this.newSketchEntities.length === 0) return;
    this.api.createCadSketch(this.projectId, {
      name: this.newSketchName.trim(), plane: this.newSketchPlane, entities: this.newSketchEntities,
    }).subscribe({
      next: () => { this.toggleAddSketch(); this.reload(); },
      error: () => { this.error.set('Échec de création du sketch.'); },
    });
  }

  deleteSketch(id: number): void {
    this.api.deleteCadSketch(id).subscribe({ next: () => this.reload() });
  }

  // ── Conception CAO : historique d'opérations ─────────────────────────────
  toggleAddOperation(): void {
    this.addingOperation.update((v) => !v);
  }

  get isPrimitive(): boolean {
    return this.newOperationType.startsWith('PRIMITIVE_');
  }

  private buildParams(): Record<string, unknown> {
    const o = this.op;
    const placement = o.usePlacement
      ? { position: [o.posX, o.posY, o.posZ], rotation_deg: [o.rotX, o.rotY, o.rotZ] }
      : undefined;

    switch (this.newOperationType) {
      case 'PRIMITIVE_BOX':
        return { length: o.length, width: o.width, height: o.height, placement };
      case 'PRIMITIVE_SPHERE':
        return { radius: o.radius, placement };
      case 'PRIMITIVE_CYLINDER':
        return { radius: o.radius, height: o.height, placement };
      case 'PRIMITIVE_CONE':
        return { bottom_radius: o.bottomRadius, top_radius: o.topRadius, height: o.height, placement };
      case 'PRIMITIVE_TORUS':
        return { major_radius: o.majorRadius, minor_radius: o.minorRadius, placement };
      case 'EXTRUDE':
        return {
          sketch_id: o.sketchId, mode: o.mode, amount: o.amount, both: o.both,
          target: o.useTarget ? o.targetId : undefined,
        };
      case 'REVOLVE':
        return {
          sketch_id: o.sketchId, mode: o.mode, angle_deg: o.angleDeg,
          axis_origin: [o.axisOriginX, o.axisOriginY, o.axisOriginZ],
          axis_direction: [o.axisDirX, o.axisDirY, o.axisDirZ],
          target: o.useTarget ? o.targetId : undefined,
        };
      case 'SURFACE_FROM_WIRE':
        return { sketch_id: o.sketchId };
      case 'BOOLEAN_UNION':
      case 'BOOLEAN_CUT':
      case 'BOOLEAN_INTERSECT':
        return { source_a: o.sourceAId, source_b: o.sourceBId };
      case 'PATTERN_CIRCULAR':
        return {
          source: o.sourceId, count: o.count, radius: o.patRadius,
          center: [o.centerX, o.centerY, o.centerZ],
          axis_direction: [o.patAxisDirX, o.patAxisDirY, o.patAxisDirZ],
          start_angle_deg: o.startAngleDeg,
        };
      case 'PATTERN_LINEAR':
        return { source: o.sourceId, count: o.count, step: [o.stepX, o.stepY, o.stepZ] };
      case 'GEAR_TEETH':
        return {
          source: o.sourceId, face_index: o.faceIndex, module: o.module,
          teeth_number: o.teethNumber, pressure_angle: o.pressureAngle,
          width: o.gearWidth, internal: o.internal,
        };
    }
  }

  addOperation(): void {
    this.api.createCadOperation(this.projectId, { operation_type: this.newOperationType, params: this.buildParams() }).subscribe({
      next: () => { this.addingOperation.set(false); this.reload(); },
      error: (err) => {
        this.error.set(err?.error?.detail ?? "Échec de création de l'opération.");
      },
    });
  }

  deleteOperation(id: number): void {
    this.api.deleteCadOperation(id).subscribe({ next: () => this.reload() });
  }

  operationSummary(op: CadOperation): string {
    const p = op.params;
    const keys = Object.keys(p).filter((k) => p[k] !== undefined && p[k] !== null);
    return keys.map((k) => `${k}=${JSON.stringify(p[k])}`).join(', ');
  }

  // ── Conception CAO : construction (Job CAD_BUILD) ────────────────────────
  launchBuild(): void {
    this.building.set(true);
    this.api.launchCadBuild(this.projectId, {
      linear_deflection: this.buildDeflectionLinear(),
      angular_deflection: this.buildDeflectionAngular(),
    }).subscribe({
      next: (job) => { this.building.set(false); this.reload(); this.startPoll(job.id); },
      error: (err) => {
        this.building.set(false);
        this.error.set(
          err?.status === 409
            ? "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, tous modules confondus."
            : (err?.error?.detail ?? 'Échec du lancement de la construction.'),
        );
      },
    });
  }

  // ── Assemblage : sous-parties ─────────────────────────────────────────────
  toggleAddSubPart(): void {
    this.addingSubPart.update((v) => !v);
    this.newSubPartName = '';
  }

  addSubPart(): void {
    if (!this.newSubPartName.trim()) return;
    this.api.createSubPart(this.projectId, { name: this.newSubPartName.trim() }).subscribe({
      next: () => { this.toggleAddSubPart(); this.reloadAssembly(); },
      error: () => { this.error.set('Échec de création de la sous-partie.'); },
    });
  }

  /** Supprime définitivement une sous-partie (et son maillage, sketchs, historique
   * CAO) — refusé (409) si elle est encore référencée par une CadAssemblyInstance
   * de cet assemblage ou d'un autre (cf. ProjectDetailView.delete côté backend). */
  deleteSubPart(id: number, name: string): void {
    const confirmed = window.confirm(
      `Supprimer définitivement la sous-partie « ${name} » ? Maillage, sketchs et historique CAO seront ` +
      'perdus. Cette action est irréversible.',
    );
    if (!confirmed) return;
    this.api.deleteProject(id).subscribe({
      next: () => this.reloadAssembly(),
      error: (err) => {
        this.error.set(
          err?.status === 409
            ? (err?.error?.detail ?? 'Suppression refusée : cette sous-partie est encore référencée.')
            : 'Échec de la suppression de la sous-partie.',
        );
      },
    });
  }

  // ── Assemblage : instances ────────────────────────────────────────────────
  toggleAddInstance(): void {
    this.addingInstance.update((v) => !v);
    this.newInstanceSourceProjectId = null;
    this.newInstanceLabel = '';
  }

  addInstance(): void {
    if (!this.newInstanceSourceProjectId) return;
    this.api.createCadInstance(this.projectId, {
      source_project: this.newInstanceSourceProjectId,
      label: this.newInstanceLabel.trim() || undefined,
    }).subscribe({
      next: () => { this.toggleAddInstance(); this.reloadAssembly(); },
      error: (err) => { this.error.set(err?.error?.detail ?? "Échec d'ajout de l'instance."); },
    });
  }

  deleteInstance(id: number): void {
    this.api.deleteCadInstance(id).subscribe({ next: () => this.reloadAssembly() });
  }

  /** Lot 5.2 — re-pointage explicite vers la dernière version du Mesh de la
   * sous-partie source (cf. CadAssemblyInstance.is_outdated). Avertit avant
   * d'agir : les contraintes déjà posées sur cette instance référencent des
   * faces/arêtes par index de l'ancien STEP, qui peuvent ne plus désigner la
   * même géométrie une fois la sous-partie régénérée (limite topologique n°2,
   * cf. to_do_3D.md). */
  repointInstanceToLatest(inst: CadAssemblyInstance): void {
    if (inst.latest_mesh_id === null) return;
    const confirmed = window.confirm(
      `Repointer « ${inst.label || this.subPartName(inst.source_project)} » vers la version `
      + `${inst.latest_mesh_version} (actuellement v${inst.source_mesh_version}) ?\n\n`
      + "Les contraintes déjà posées sur cette instance référencent des faces/arêtes de l'ancienne "
      + 'version — elles peuvent ne plus désigner la même géométrie après ce changement. '
      + 'Vérifiez-les une fois le repointage effectué.',
    );
    if (!confirmed) return;
    this.api.repointCadInstance(inst.id, inst.latest_mesh_id).subscribe({
      next: () => this.reloadAssembly(),
      error: (err) => { this.error.set(err?.error?.detail ?? 'Échec du repointage.'); },
    });
  }

  // ── Assemblage : contraintes ──────────────────────────────────────────────
  toggleAddConstraint(): void {
    this.addingConstraint.update((v) => !v);
  }

  get constraintNeedsInstanceB(): boolean {
    return this.newConstraint.type !== 'FIXED';
  }

  addConstraint(): void {
    const c = this.newConstraint;
    if (!c.instanceAId) return;
    const params: Record<string, unknown> = {};
    if (c.type === 'DISTANCE') params['distance_mm'] = c.distanceMm;
    if (c.type === 'ANGLE') params['angle_deg'] = c.angleDeg;

    this.api.createCadConstraint(this.projectId, {
      constraint_type: c.type,
      instance_a: c.instanceAId,
      reference_a: { kind: c.refAKind, index: c.refAIndex },
      instance_b: this.constraintNeedsInstanceB ? c.instanceBId : null,
      reference_b: this.constraintNeedsInstanceB ? { kind: c.refBKind, index: c.refBIndex } : null,
      params,
    }).subscribe({
      next: () => { this.addingConstraint.set(false); this.reloadAssembly(); },
      error: (err) => { this.error.set(err?.error?.detail ?? 'Échec de pose de la contrainte.'); },
    });
  }

  deleteConstraint(id: number): void {
    this.api.deleteCadConstraint(id).subscribe({ next: () => this.reloadAssembly() });
  }

  constraintSummary(c: CadAssemblyConstraint): string {
    const a = `${this.instanceLabel(c.instance_a)}.${c.reference_a.kind}${c.reference_a.index}`;
    if (c.constraint_type === 'FIXED') return `${a} → monde`;
    const b = c.instance_b !== null && c.reference_b
      ? `${this.instanceLabel(c.instance_b)}.${c.reference_b.kind}${c.reference_b.index}`
      : '?';
    return `${a} ↔ ${b}`;
  }

  // ── Assemblage : résolution (Job CAD_ASSEMBLE) ───────────────────────────
  launchAssemble(): void {
    this.outdatedInstances.set([]);
    this.assembling.set(true);
    this.api.launchCadAssemble(this.projectId).subscribe({
      next: (job) => { this.assembling.set(false); this.reload(); this.startPoll(job.id); },
      error: (err) => {
        this.assembling.set(false);
        if (err?.status === 409 && err?.error?.outdated_instances?.length) {
          // Limite topologique n°2 (to_do_3D.md) : au moins une instance pointe
          // un Mesh plus ancien que la dernière version de sa sous-partie —
          // avertir plutôt que résoudre silencieusement sur une géométrie périmée.
          this.outdatedInstances.set(err.error.outdated_instances);
          this.error.set(err.error.detail);
        } else if (err?.status === 409) {
          this.error.set("Un job lourd est déjà en cours pour l'atelier — un seul à la fois, tous modules confondus.");
        } else {
          this.error.set(err?.error?.detail ?? "Échec du lancement de l'assemblage.");
        }
      },
    });
  }

  /** Résout quand même malgré les instances périmées signalées par launchAssemble(). */
  resolveAssembleAnyway(): void {
    this.outdatedInstances.set([]);
    this.assembling.set(true);
    this.api.launchCadAssemble(this.projectId, { confirm_outdated: true }).subscribe({
      next: (job) => { this.assembling.set(false); this.reload(); this.startPoll(job.id); },
      error: (err) => {
        this.assembling.set(false);
        this.error.set(
          err?.status === 409
            ? "Un job lourd est déjà en cours pour l'atelier — un seul à la fois, tous modules confondus."
            : (err?.error?.detail ?? "Échec du lancement de l'assemblage."),
        );
      },
    });
  }
}
