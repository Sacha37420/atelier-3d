import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { KeycloakService } from './keycloak.service';

interface EnvWindow {
  __env?: { apiUrl?: string };
}

export type ProjectType = 'objet' | 'batiment' | 'assembly';
export type AssemblyStatus = 'DRAFT' | 'SOLVED' | 'ERROR';

export interface Project {
  id: number;
  name: string;
  description: string;
  project_type: ProjectType;
  scale_meters_per_unit: number | null;
  assembly_status: AssemblyStatus | null;
  parent_project: number | null;
  sub_parts_count: number;
  has_scale: boolean;
  has_active_job: boolean;
  has_mesh: boolean;
  has_resolved_poses: boolean;
  photo_count: number;
  owner_email: string;
  created_at: string;
  updated_at: string;
}

export interface Photo {
  id: number;
  file: string;
  order: number;
  camera_pose: Record<string, unknown> | null;
  pose_resolved: boolean;
  region_overlay: string | null;
  region_count: number | null;
  created_at: string;
}

export interface TopologyMeasures {
  boundary_edges: number;
  number_holes: number;
  non_two_manifold_edges: number;
  non_two_manifold_vertices: number;
  faces_number: number;
  vertices_number: number;
  is_watertight: boolean;
}

export interface RepairReport {
  method: 'repair' | 'poisson';
  before: TopologyMeasures;
  after: TopologyMeasures;
  target_faces: number | null;
}

export interface Mesh {
  id: number;
  project: number;
  job: number | null;
  file: string;
  gltf_file: string | null;
  version: number;
  vertex_count: number | null;
  face_count: number | null;
  is_watertight: boolean;
  repair_report: RepairReport | null;
  step_file: string | null;
  linear_deflection: number | null;
  angular_deflection: number | null;
  created_at: string;
}

export type JobStatus = 'PENDING' | 'RUNNING' | 'DONE' | 'ERROR';
export type JobKind =
  | 'RECONSTRUCTION' | 'REPAIR' | 'SEGMENTATION_PARTS' | 'SEGMENTATION_FACADE'
  | 'CAD_BUILD' | 'CAD_ASSEMBLE';

export type PrintQuaternion = [number, number, number, number];

export interface AutoOrientSuggestion {
  quaternion: PrintQuaternion;
  overhang_ratio: number;
}

export type ExportFormat = 'stl' | '3mf';

export interface Job {
  id: number;
  project: number;
  kind: JobKind;
  status: JobStatus;
  progress: number;
  message: string;
  params: Record<string, unknown>;
  duration_seconds: number | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail extends Project {
  photos: Photo[];
  jobs: Job[];
  meshes: Mesh[];
}

// ── Atelier 3D — Lot 3 (Mouvements) ─────────────────────────────────────────
export type Vec3 = [number, number, number];
export type PrimitiveType = 'plane' | 'cylinder' | 'sphere' | '';

export interface PrimitiveParams {
  normal?: Vec3;
  d?: number;
  center?: Vec3;
  axis?: Vec3;
  radius?: number;
}

export interface Part {
  id: number;
  mesh: number;
  name: string;
  face_ids: number[];
  color: string;
  suggested: boolean;
  primitive_type: PrimitiveType;
  primitive_params: PrimitiveParams | null;
  face_count: number;
  created_at: string;
  updated_at: string;
}

export type JointType = 'revolute' | 'prismatic' | 'fixed';

export interface Joint {
  id: number;
  parent_part: number;
  child_part: number;
  joint_type: JointType;
  axis_origin: Vec3;
  axis_direction: Vec3;
  limit_min: number | null;
  limit_max: number | null;
  created_at: string;
}

export interface JointAxisSuggestion {
  type: 'revolute' | 'prismatic';
  origin: Vec3;
  direction: Vec3;
}

export type Preset = 'rapide' | 'equilibre' | 'precis';

export interface ReconstructionEstimate {
  preset: Preset;
  n_photos: number;
  estimated_seconds: number;
  warning_threshold_exceeded: boolean;
}

// ── Atelier 3D — Lot 4 (Bâtiments) ──────────────────────────────────────────
export type SemanticClassName = 'mur' | 'fenetre' | 'porte' | 'toit';

export const SEMANTIC_CLASS_LABELS: Record<SemanticClassName, string> = {
  mur: 'Mur', fenetre: 'Fenêtre', porte: 'Porte', toit: 'Toit',
};

export interface PhotoLabel {
  id: number;
  photo: number;
  semantic_class: SemanticClassName;
  region_index: number;
  created_at: string;
}

export interface SemanticClass {
  id: number;
  mesh: number;
  name: string;
  color: string;
  face_ids: number[];
  face_count: number;
  created_at: string;
}

export interface FacadeEstimate {
  n_photos: number;
  estimated_seconds: number;
  warning_threshold_exceeded: boolean;
}

// ── Atelier 3D — Lot 5 (Conception CAO manuelle) ────────────────────────────
// V1 = formulaires numériques, pas de canvas interactif (cf. to_do_3D.md).
export type CadPlane = 'XY' | 'XZ' | 'YZ';

export interface CadSegment { type: 'segment'; start: [number, number]; end: [number, number]; }
export interface CadPolyline { type: 'polyline'; points: [number, number][]; closed: boolean; }
export interface CadCircle { type: 'circle'; center: [number, number]; radius: number; }
export interface CadArc { type: 'arc'; start: [number, number]; mid: [number, number]; end: [number, number]; }
export interface CadBSpline { type: 'bspline'; points: [number, number][]; thickness?: number; }
export type CadEntity = CadSegment | CadPolyline | CadCircle | CadArc | CadBSpline;
export type CadEntityType = CadEntity['type'];

export interface CadSketch {
  id: number;
  project: number;
  name: string;
  plane: CadPlane;
  entities: CadEntity[];
  created_at: string;
  updated_at: string;
}

export type CadOperationType =
  | 'PRIMITIVE_BOX' | 'PRIMITIVE_SPHERE' | 'PRIMITIVE_CYLINDER' | 'PRIMITIVE_CONE' | 'PRIMITIVE_TORUS'
  | 'EXTRUDE' | 'REVOLVE' | 'SURFACE_FROM_WIRE'
  | 'BOOLEAN_UNION' | 'BOOLEAN_CUT' | 'BOOLEAN_INTERSECT'
  | 'PATTERN_CIRCULAR' | 'PATTERN_LINEAR' | 'GEAR_TEETH';

export const CAD_OPERATION_LABELS: Record<CadOperationType, string> = {
  PRIMITIVE_BOX: 'Primitive : boîte',
  PRIMITIVE_SPHERE: 'Primitive : sphère',
  PRIMITIVE_CYLINDER: 'Primitive : cylindre',
  PRIMITIVE_CONE: 'Primitive : cône',
  PRIMITIVE_TORUS: 'Primitive : tore',
  EXTRUDE: 'Extrusion',
  REVOLVE: 'Révolution',
  SURFACE_FROM_WIRE: 'Surface depuis un contour',
  BOOLEAN_UNION: 'Booléen : union',
  BOOLEAN_CUT: 'Booléen : différence',
  BOOLEAN_INTERSECT: 'Booléen : intersection',
  PATTERN_CIRCULAR: 'Répétition circulaire',
  PATTERN_LINEAR: 'Répétition linéaire',
  GEAR_TEETH: 'Denture (engrenage)',
};

export interface CadOperation {
  id: number;
  project: number;
  order: number;
  operation_type: CadOperationType;
  params: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

// ── Atelier 3D — Lot 5.2 (Assemblage) ───────────────────────────────────────
// Un Project(ASSEMBLY) réunit des sous-parties (Project.parent_project) —
// chacune garde son propre historique CadSketch/CadOperation (Lot 5.1),
// simplement rattachée à ce parent au lieu d'être un projet indépendant.
export type ConstraintType =
  | 'COINCIDENT' | 'CONTACT' | 'PARALLEL' | 'CONCENTRIC' | 'DISTANCE' | 'ANGLE' | 'FIXED' | 'GEAR_MESH';

export const CONSTRAINT_TYPE_LABELS: Record<ConstraintType, string> = {
  COINCIDENT: 'Coïncidence',
  CONTACT: 'Contact / coplanaire',
  PARALLEL: 'Parallélisme',
  CONCENTRIC: 'Concentricité (axes)',
  DISTANCE: 'Distance',
  ANGLE: 'Angle',
  FIXED: 'Fixation au monde',
  GEAR_MESH: 'Engrènement (2 roues dentées)',
};

export interface CadReference {
  kind: 'face' | 'edge' | 'vertex';
  index: number;
}

export interface CadAssemblyPlacement {
  position: [number, number, number];
  quaternion_xyzw: [number, number, number, number];
}

export interface CadAssemblyInstance {
  id: number;
  assembly_project: number;
  source_project: number;
  source_mesh: number;
  label: string;
  placement: CadAssemblyPlacement | null;
  created_at: string;
}

export interface CadAssemblyConstraint {
  id: number;
  assembly_project: number;
  constraint_type: ConstraintType;
  instance_a: number;
  reference_a: CadReference;
  instance_b: number | null;
  reference_b: CadReference | null;
  params: Record<string, unknown>;
  created_at: string;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);
  private kc = inject(KeycloakService);

  get base(): string {
    return (window as unknown as EnvWindow).__env?.apiUrl
      ?? 'http://localhost:8092';
  }

  /**
   * URL absolue d'un fichier média (photo, maillage), utilisable telle quelle
   * dans un <img> ou passée à GLTFLoader. Le JWT part en paramètre d'URL : ni
   * une balise <img> ni le fetch() interne de GLTFLoader ne peuvent poser
   * d'en-tête Authorization (cf. MediaView côté backend, api/views.py).
   */
  mediaUrl(path: string | null): string {
    if (!path) return '';
    if (/^https?:\/\//.test(path)) return path;
    const token = this.kc.getToken();
    return `${this.base}${path}${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  }

  getMe(): Observable<unknown> {
    return this.http.get(`${this.base}/api/me/`);
  }

  getDepartments(): Observable<unknown[]> {
    return this.http.get<unknown[]>(`${this.base}/api/departments/`);
  }

  getUsers(): Observable<unknown[]> {
    return this.http.get<unknown[]>(`${this.base}/api/users/`);
  }

  // ── Atelier 3D — Lot 1 ────────────────────────────────────────────────────
  getProjects(): Observable<Project[]> {
    return this.http.get<Project[]>(`${this.base}/api/projects/`);
  }

  createProject(data: { name: string; description?: string; project_type?: ProjectType }): Observable<Project> {
    return this.http.post<Project>(`${this.base}/api/projects/`, data);
  }

  getProject(id: number): Observable<ProjectDetail> {
    return this.http.get<ProjectDetail>(`${this.base}/api/projects/${id}/`);
  }

  updateProject(id: number, data: Partial<Pick<Project, 'name' | 'description' | 'scale_meters_per_unit'>>): Observable<Project> {
    return this.http.patch<Project>(`${this.base}/api/projects/${id}/`, data);
  }

  uploadPhotos(projectId: number, files: File[]): Observable<Photo[]> {
    const form = new FormData();
    for (const f of files) form.append('files', f);
    return this.http.post<Photo[]>(`${this.base}/api/projects/${projectId}/photos/`, form);
  }

  deletePhoto(projectId: number, photoId: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/projects/${projectId}/photos/${photoId}/`);
  }

  uploadVideo(projectId: number, file: File, fps: number): Observable<Photo[]> {
    const form = new FormData();
    form.append('file', file);
    form.append('fps', String(fps));
    return this.http.post<Photo[]>(`${this.base}/api/projects/${projectId}/video/`, form);
  }

  getReconstructionEstimate(projectId: number, preset: Preset): Observable<ReconstructionEstimate> {
    return this.http.get<ReconstructionEstimate>(
      `${this.base}/api/projects/${projectId}/reconstruct/estimate/`, { params: { preset } },
    );
  }

  launchReconstruction(projectId: number, preset: Preset): Observable<Job> {
    return this.http.post<Job>(`${this.base}/api/projects/${projectId}/reconstruct/`, { preset });
  }

  getJob(id: number): Observable<Job> {
    return this.http.get<Job>(`${this.base}/api/jobs/${id}/`);
  }

  getJobs(projectId?: number): Observable<Job[]> {
    const params: Record<string, string> = {};
    if (projectId) params['project'] = String(projectId);
    return this.http.get<Job[]>(`${this.base}/api/jobs/`, { params });
  }

  // ── Atelier 3D — Lot 2 (Impression 3D) ───────────────────────────────────
  launchRepair(projectId: number, opts: { target_triangles?: number; target_size_mb?: number }): Observable<Job> {
    return this.http.post<Job>(`${this.base}/api/projects/${projectId}/repair/`, opts);
  }

  getAutoOrient(meshId: number): Observable<AutoOrientSuggestion> {
    return this.http.get<AutoOrientSuggestion>(`${this.base}/api/meshes/${meshId}/auto-orient/`);
  }

  /**
   * Le paramètre s'appelle `file_format`, pas `format` : DRF réserve `?format=`
   * pour sa négociation de contenu (sélection du renderer) — une valeur non
   * reconnue comme 'stl' y échoue en 404 avant même d'atteindre la vue Django
   * (cf. MeshExportView, backend/api/views.py).
   */
  exportMesh(meshId: number, format: ExportFormat, quaternion: PrintQuaternion): Observable<Blob> {
    const [qx, qy, qz, qw] = quaternion;
    return this.http.get(`${this.base}/api/meshes/${meshId}/export/`, {
      params: { file_format: format, qx: String(qx), qy: String(qy), qz: String(qz), qw: String(qw) },
      responseType: 'blob',
    });
  }

  // ── Atelier 3D — Lot 3 (Mouvements) ──────────────────────────────────────
  getParts(meshId: number): Observable<Part[]> {
    return this.http.get<Part[]>(`${this.base}/api/meshes/${meshId}/parts/`);
  }

  suggestParts(meshId: number): Observable<Part[]> {
    return this.http.post<Part[]>(`${this.base}/api/meshes/${meshId}/parts/suggest/`, {});
  }

  createPart(meshId: number, name: string, faceIds: number[]): Observable<Part> {
    return this.http.post<Part>(`${this.base}/api/meshes/${meshId}/parts/`, { name, face_ids: faceIds });
  }

  updatePart(id: number, data: { name?: string; face_ids?: number[] }): Observable<Part> {
    return this.http.patch<Part>(`${this.base}/api/parts/${id}/`, data);
  }

  deletePart(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/parts/${id}/`);
  }

  getJoints(meshId: number): Observable<Joint[]> {
    return this.http.get<Joint[]>(`${this.base}/api/meshes/${meshId}/joints/`);
  }

  createJoint(meshId: number, data: {
    parent_part: number; child_part: number; joint_type: JointType;
    axis_origin: Vec3; axis_direction: Vec3; limit_min?: number | null; limit_max?: number | null;
  }): Observable<Joint> {
    return this.http.post<Joint>(`${this.base}/api/meshes/${meshId}/joints/`, data);
  }

  updateJoint(
    id: number,
    data: Partial<Pick<Joint, 'joint_type' | 'axis_origin' | 'axis_direction' | 'limit_min' | 'limit_max'>>,
  ): Observable<Joint> {
    return this.http.patch<Joint>(`${this.base}/api/joints/${id}/`, data);
  }

  deleteJoint(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/joints/${id}/`);
  }

  suggestJointAxis(partId: number, otherId: number): Observable<{ suggestion: JointAxisSuggestion | null }> {
    return this.http.get<{ suggestion: JointAxisSuggestion | null }>(
      `${this.base}/api/parts/${partId}/suggest-axis/`, { params: { other: String(otherId) } },
    );
  }

  // ── Atelier 3D — Lot 4 (Bâtiments) ───────────────────────────────────────
  /** Calcule (ou réutilise le cache) la segmentation 2D zero-shot d'une photo — jusqu'à ~15-20s au premier appel. */
  getPhotoRegions(photoId: number): Observable<Photo> {
    return this.http.get<Photo>(`${this.base}/api/photos/${photoId}/regions/`);
  }

  getPhotoLabels(photoId: number): Observable<PhotoLabel[]> {
    return this.http.get<PhotoLabel[]>(`${this.base}/api/photos/${photoId}/labels/`);
  }

  createPhotoLabel(photoId: number, x: number, y: number, semanticClass: SemanticClassName): Observable<PhotoLabel> {
    return this.http.post<PhotoLabel>(`${this.base}/api/photos/${photoId}/labels/`, { x, y, semantic_class: semanticClass });
  }

  deletePhotoLabel(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/photo-labels/${id}/`);
  }

  getFacadeEstimate(projectId: number): Observable<FacadeEstimate> {
    return this.http.get<FacadeEstimate>(`${this.base}/api/projects/${projectId}/facade/estimate/`);
  }

  launchFacadeSegmentation(projectId: number): Observable<Job> {
    return this.http.post<Job>(`${this.base}/api/projects/${projectId}/facade/`, {});
  }

  getSemanticClasses(meshId: number): Observable<SemanticClass[]> {
    return this.http.get<SemanticClass[]>(`${this.base}/api/meshes/${meshId}/semantic-classes/`);
  }

  // ── Atelier 3D — Lot 5 (Conception CAO) ──────────────────────────────────
  getCadSketches(projectId: number): Observable<CadSketch[]> {
    return this.http.get<CadSketch[]>(`${this.base}/api/projects/${projectId}/cad-sketches/`);
  }

  createCadSketch(projectId: number, data: { name: string; plane: CadPlane; entities: CadEntity[] }): Observable<CadSketch> {
    return this.http.post<CadSketch>(`${this.base}/api/projects/${projectId}/cad-sketches/`, data);
  }

  deleteCadSketch(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/cad-sketches/${id}/`);
  }

  getCadOperations(projectId: number): Observable<CadOperation[]> {
    return this.http.get<CadOperation[]>(`${this.base}/api/projects/${projectId}/cad-operations/`);
  }

  createCadOperation(
    projectId: number, data: { operation_type: CadOperationType; params: Record<string, unknown>; order?: number },
  ): Observable<CadOperation> {
    return this.http.post<CadOperation>(`${this.base}/api/projects/${projectId}/cad-operations/`, data);
  }

  deleteCadOperation(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/cad-operations/${id}/`);
  }

  launchCadBuild(projectId: number, opts: { linear_deflection?: number; angular_deflection?: number } = {}): Observable<Job> {
    return this.http.post<Job>(`${this.base}/api/projects/${projectId}/cad-build/`, opts);
  }

  // ── Atelier 3D — Lot 5.2 (Assemblage) ────────────────────────────────────
  getSubParts(projectId: number): Observable<Project[]> {
    return this.http.get<Project[]>(`${this.base}/api/projects/${projectId}/sub-parts/`);
  }

  createSubPart(projectId: number, data: { name: string; project_type?: ProjectType }): Observable<Project> {
    return this.http.post<Project>(`${this.base}/api/projects/${projectId}/sub-parts/`, data);
  }

  getCadInstances(projectId: number): Observable<CadAssemblyInstance[]> {
    return this.http.get<CadAssemblyInstance[]>(`${this.base}/api/projects/${projectId}/cad-instances/`);
  }

  createCadInstance(
    projectId: number, data: { source_project: number; source_mesh?: number; label?: string },
  ): Observable<CadAssemblyInstance> {
    return this.http.post<CadAssemblyInstance>(`${this.base}/api/projects/${projectId}/cad-instances/`, data);
  }

  deleteCadInstance(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/cad-instances/${id}/`);
  }

  getCadConstraints(projectId: number): Observable<CadAssemblyConstraint[]> {
    return this.http.get<CadAssemblyConstraint[]>(`${this.base}/api/projects/${projectId}/cad-constraints/`);
  }

  createCadConstraint(projectId: number, data: {
    constraint_type: ConstraintType; instance_a: number; reference_a: CadReference;
    instance_b?: number | null; reference_b?: CadReference | null; params?: Record<string, unknown>;
  }): Observable<CadAssemblyConstraint> {
    return this.http.post<CadAssemblyConstraint>(`${this.base}/api/projects/${projectId}/cad-constraints/`, data);
  }

  deleteCadConstraint(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/api/cad-constraints/${id}/`);
  }

  launchCadAssemble(projectId: number): Observable<Job> {
    return this.http.post<Job>(`${this.base}/api/projects/${projectId}/cad-assemble/`, {});
  }
}
