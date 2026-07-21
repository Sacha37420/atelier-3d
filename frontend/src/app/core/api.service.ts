import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { KeycloakService } from './keycloak.service';

interface EnvWindow {
  __env?: { apiUrl?: string };
}

export type ProjectType = 'objet' | 'batiment';

export interface Project {
  id: number;
  name: string;
  description: string;
  project_type: ProjectType;
  scale_meters_per_unit: number | null;
  has_scale: boolean;
  has_active_job: boolean;
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
  created_at: string;
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
  created_at: string;
}

export type JobStatus = 'PENDING' | 'RUNNING' | 'DONE' | 'ERROR';
export type JobKind = 'RECONSTRUCTION' | 'REPAIR' | 'SEGMENTATION_PARTS' | 'SEGMENTATION_FACADE';

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

export type Preset = 'rapide' | 'equilibre' | 'precis';

export interface ReconstructionEstimate {
  preset: Preset;
  n_photos: number;
  estimated_seconds: number;
  warning_threshold_exceeded: boolean;
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
}
