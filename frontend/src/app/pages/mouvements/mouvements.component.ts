import { Component, ViewChild, inject, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  ApiService, Project, ProjectDetail, Part, Joint, JointType, Vec3, JointAxisSuggestion,
} from '../../core/api.service';
import { MeshViewerComponent, PaintedPart } from '../../components/mesh-viewer/mesh-viewer.component';
import { KinematicPreviewComponent } from '../../components/kinematic-preview/kinematic-preview.component';

const JOINT_TYPE_LABELS: Record<JointType, string> = { revolute: 'Pivot', prismatic: 'Glissière', fixed: 'Fixe' };

/**
 * Page Mouvements (Lot 3) : même pattern picker que la page Impression
 * (réutilisable sur n'importe quel projet ayant déjà un maillage, cf. demande
 * utilisateur de découpler les lots 2+ d'un projet précis) — découpage en
 * parties (peinture au pinceau 3D, l'outil principal, ou suggestion RANSAC en
 * fond), définition de jointures (axe suggéré depuis la zone de contact ou
 * posé à la main), aperçu cinématique avec un slider par jointure.
 */
@Component({
  selector: 'app-mouvements',
  standalone: true,
  imports: [FormsModule, RouterLink, DecimalPipe, MeshViewerComponent, KinematicPreviewComponent],
  templateUrl: './mouvements.component.html',
  styleUrl: './mouvements.component.scss',
})
export class MouvementsComponent {
  private api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  readonly jointTypeLabels = JOINT_TYPE_LABELS;
  readonly jointTypes: JointType[] = ['revolute', 'prismatic', 'fixed'];

  readonly projects = signal<Project[]>([]);
  readonly selectedProjectId = signal<number | null>(null);
  readonly project = signal<ProjectDetail | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  readonly parts = signal<Part[]>([]);
  readonly joints = signal<Joint[]>([]);
  readonly suggesting = signal(false);

  // ── Peinture / édition de partie ─────────────────────────────────────────
  readonly paintMode = signal(false);
  readonly editingPartId = signal<number | null>(null);
  newPartName = '';
  readonly savingPart = signal(false);

  // ── Création de jointure ─────────────────────────────────────────────────
  readonly selectedParentId = signal<number | null>(null);
  readonly selectedChildId = signal<number | null>(null);
  readonly newJointType = signal<JointType>('revolute');
  readonly axisSuggestion = signal<JointAxisSuggestion | null>(null);
  readonly suggestingAxis = signal(false);
  readonly placingAxis = signal(false);
  readonly axisOrigin = signal<Vec3>([0, 0, 0]);
  readonly axisDirection = signal<Vec3>([0, 0, 1]);
  readonly axisReady = signal(false);
  newLimitMin: number | null = null;
  newLimitMax: number | null = null;
  readonly creatingJoint = signal(false);

  @ViewChild(MeshViewerComponent) private viewer?: MeshViewerComponent;

  readonly selectableProjects = computed(() => this.projects().filter((p) => p.has_mesh));

  readonly latestMesh = computed(() => {
    const meshes = this.project()?.meshes ?? [];
    return meshes.length > 0 ? meshes[0] : null;
  });

  readonly meshViewerUrl = computed(() => {
    const mesh = this.latestMesh();
    return mesh?.gltf_file ? this.api.mediaUrl(mesh.gltf_file) : null;
  });

  /** Parties affichées en surbrillance atténuée pendant la peinture — exclut celle en cours d'édition. */
  readonly viewerParts = computed(() => {
    const editing = this.editingPartId();
    return this.parts()
      .filter((p) => p.id !== editing)
      .map((p): PaintedPart => ({ id: p.id, faceIds: p.face_ids, color: p.color }));
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
        this.parts.set([]);
        this.joints.set([]);
      }
    });
  }

  selectProject(id: number | null): void {
    if (!id) return;
    this.router.navigate(['/mouvements', id]);
  }

  reload(): void {
    const id = this.selectedProjectId();
    if (!id) return;
    this.loading.set(true);
    this.api.getProject(id).subscribe({
      next: (project) => {
        this.project.set(project);
        this.loading.set(false);
        const mesh = project.meshes[0];
        if (mesh) this.reloadPartsAndJoints(mesh.id);
      },
      error: () => { this.error.set("Impossible de charger le projet."); this.loading.set(false); },
    });
  }

  private reloadPartsAndJoints(meshId: number): void {
    this.api.getParts(meshId).subscribe({ next: (parts) => this.parts.set(parts) });
    this.api.getJoints(meshId).subscribe({ next: (joints) => this.joints.set(joints) });
  }

  // ── Parties ───────────────────────────────────────────────────────────────
  startNewPart(): void {
    this.editingPartId.set(null);
    this.newPartName = `Partie ${this.parts().length + 1}`;
    this.paintMode.set(true);
    this.viewer?.clearPaintSelection();
  }

  editPart(part: Part): void {
    this.editingPartId.set(part.id);
    this.newPartName = part.name;
    this.paintMode.set(true);
    this.viewer?.loadPaintSelection(part.face_ids);
  }

  cancelPaint(): void {
    this.paintMode.set(false);
    this.editingPartId.set(null);
    this.viewer?.clearPaintSelection();
  }

  savePart(): void {
    const mesh = this.latestMesh();
    if (!mesh) return;
    const faceIds = this.viewer?.getPaintedFaceIds() ?? [];
    if (faceIds.length === 0) {
      this.error.set("Peindre au moins une face avant d'enregistrer.");
      return;
    }
    const name = this.newPartName.trim() || 'Partie';
    this.savingPart.set(true);
    const editingId = this.editingPartId();
    const obs = editingId
      ? this.api.updatePart(editingId, { name, face_ids: faceIds })
      : this.api.createPart(mesh.id, name, faceIds);
    obs.subscribe({
      next: () => {
        this.savingPart.set(false);
        this.paintMode.set(false);
        this.editingPartId.set(null);
        this.viewer?.clearPaintSelection();
        this.reloadPartsAndJoints(mesh.id);
      },
      error: () => { this.savingPart.set(false); this.error.set("Échec de l'enregistrement de la partie."); },
    });
  }

  deletePart(part: Part): void {
    const mesh = this.latestMesh();
    if (!mesh) return;
    if (!window.confirm(`Supprimer la partie « ${part.name} » ? Les jointures qui l'utilisent seront aussi supprimées.`)) {
      return;
    }
    this.api.deletePart(part.id).subscribe({ next: () => this.reloadPartsAndJoints(mesh.id) });
  }

  suggestParts(): void {
    const mesh = this.latestMesh();
    if (!mesh) return;
    this.suggesting.set(true);
    this.api.suggestParts(mesh.id).subscribe({
      next: () => { this.suggesting.set(false); this.reloadPartsAndJoints(mesh.id); },
      error: () => { this.suggesting.set(false); this.error.set('Échec de la segmentation automatique.'); },
    });
  }

  // ── Jointures ─────────────────────────────────────────────────────────────
  partName(id: number | null): string {
    return this.parts().find((p) => p.id === id)?.name ?? '';
  }

  suggestAxis(): void {
    const a = this.selectedParentId();
    const b = this.selectedChildId();
    if (!a || !b) return;
    this.suggestingAxis.set(true);
    this.api.suggestJointAxis(a, b).subscribe({
      next: (r) => {
        this.suggestingAxis.set(false);
        this.axisSuggestion.set(r.suggestion);
        if (r.suggestion) {
          this.newJointType.set(r.suggestion.type);
          this.axisOrigin.set(r.suggestion.origin);
          this.axisDirection.set(r.suggestion.direction);
          this.axisReady.set(true);
        } else {
          this.axisReady.set(false);
        }
      },
      error: () => { this.suggestingAxis.set(false); this.error.set("Échec de la suggestion d'axe."); },
    });
  }

  togglePlaceAxis(): void {
    this.placingAxis.update((v) => !v);
  }

  onAxisPicked(axis: { origin: [number, number, number]; direction: [number, number, number] }): void {
    this.axisOrigin.set(axis.origin);
    this.axisDirection.set(axis.direction);
    this.axisReady.set(true);
    this.placingAxis.set(false);
  }

  createJoint(): void {
    const mesh = this.latestMesh();
    const parent = this.selectedParentId();
    const child = this.selectedChildId();
    if (!mesh || !parent || !child || !this.axisReady()) return;
    this.creatingJoint.set(true);
    this.api.createJoint(mesh.id, {
      parent_part: parent, child_part: child, joint_type: this.newJointType(),
      axis_origin: this.axisOrigin(), axis_direction: this.axisDirection(),
      limit_min: this.newLimitMin, limit_max: this.newLimitMax,
    }).subscribe({
      next: () => {
        this.creatingJoint.set(false);
        this.selectedParentId.set(null);
        this.selectedChildId.set(null);
        this.axisSuggestion.set(null);
        this.axisReady.set(false);
        this.newLimitMin = null;
        this.newLimitMax = null;
        this.reloadPartsAndJoints(mesh.id);
      },
      error: (err) => {
        this.creatingJoint.set(false);
        this.error.set(err?.error?.detail ?? "Échec de la création de la jointure.");
      },
    });
  }

  deleteJoint(joint: Joint): void {
    const mesh = this.latestMesh();
    if (!mesh) return;
    this.api.deleteJoint(joint.id).subscribe({ next: () => this.reloadPartsAndJoints(mesh.id) });
  }
}
