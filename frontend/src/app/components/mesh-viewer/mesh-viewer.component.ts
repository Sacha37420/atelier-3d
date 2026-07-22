import {
  Component, ElementRef, EventEmitter, Input, Output, ViewChild,
  AfterViewInit, OnDestroy, OnChanges, SimpleChanges, NgZone, signal,
} from '@angular/core';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { computeBoundsTree, disposeBoundsTree, acceleratedRaycast } from 'three-mesh-bvh';

// Un raycast three.js par défaut teste chaque triangle un par un : sur un
// maillage de reconstruction dense (plusieurs centaines de milliers de
// faces, courant dès qu'une reconstruction utilise beaucoup de photos), un
// seul raycast peut prendre plusieurs centaines de ms — vérifié : un simple
// coup de pinceau devenait ingérable (~2s par déplacement de souris) sans
// cette accélération. three-mesh-bvh construit une fois une hiérarchie de
// volumes englobants et patch Raycaster pour l'utiliser automatiquement,
// pour tout raycast de ce composant (pinceau, calibration, pose d'axe).
THREE.BufferGeometry.prototype.computeBoundsTree = computeBoundsTree;
THREE.BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree;
THREE.Mesh.prototype.raycast = acceleratedRaycast;

export interface CalibrationMeasurement {
  /** Distance entre les deux points cliqués, dans l'unité brute du maillage. */
  meshDistance: number;
}

export interface PaintedPart {
  id: number;
  faceIds: number[];
  color: string;
}

const BRUSH_RADIUS_RATIOS = [0.01, 0.02, 0.035, 0.06, 0.1];
const PAINT_BASE_COLOR = new THREE.Color(0xb0b0b0);
const PAINT_HIGHLIGHT_COLOR = new THREE.Color(0xff8c1a);

/**
 * Viewer 3D three.js pour les maillages produits par le pipeline de reconstruction
 * (cf. to_do_3D.md — "Viewer 3D frontend : three.js (OrbitControls, GLTFLoader,
 * STLLoader)"). Affiche le glTF exporté par le backend (format pivot interne = PLY,
 * glTF pour ce viewer).
 *
 * Mode calibration : deux clics sur le maillage mesurent une distance en unité
 * brute du maillage (aucune mise à l'échelle d'affichage n'est appliquée au modèle
 * — seule la caméra est repositionnée — pour que cette distance corresponde
 * exactement à `scale_meters_per_unit` côté backend).
 */
@Component({
  selector: 'app-mesh-viewer',
  standalone: true,
  templateUrl: './mesh-viewer.component.html',
  styleUrl: './mesh-viewer.component.scss',
})
export class MeshViewerComponent implements AfterViewInit, OnDestroy, OnChanges {
  @Input() meshUrl: string | null = null;
  @Input() calibrationMode = false;
  @Input() orientationMode = false;
  @Output() measured = new EventEmitter<CalibrationMeasurement>();
  /** Quaternion [x, y, z, w] courant appliqué au maillage (orientation d'impression, Lot 2). */
  @Output() orientationChanged = new EventEmitter<[number, number, number, number]>();

  /**
   * Pose manuelle d'un axe de jointure (Lot 3) : mêmes 2 clics que la
   * calibration, mais expose les points bruts (origine + direction) plutôt
   * qu'une distance — repli quand la suggestion RANSAC de zone de contact ne
   * trouve rien (cf. to_do_3D.md : "sinon entièrement manuel via manipulateur
   * 3D dans le viewer").
   */
  @Input() axisMode = false;
  @Output() axisPicked = new EventEmitter<{ origin: [number, number, number]; direction: [number, number, number] }>();

  // ── Peinture de parties (Lot 3) ──────────────────────────────────────────
  @Input() paintMode = false;
  /** Autres parties déjà enregistrées, affichées en surbrillance atténuée pendant la peinture. */
  @Input()
  set existingParts(parts: PaintedPart[]) {
    this._existingParts = parts;
    this.rebuildPartColorMap();
    if (this.paintMode) this.repaintAllColors();
  }
  get existingParts(): PaintedPart[] { return this._existingParts; }
  private _existingParts: PaintedPart[] = [];

  @ViewChild('canvasHost', { static: true }) private canvasHost!: ElementRef<HTMLDivElement>;

  readonly wireframe = signal(false);
  readonly loading = signal(false);
  readonly loadError = signal<string | null>(null);
  readonly pickedCount = signal(0);

  private renderer?: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera?: THREE.PerspectiveCamera;
  private controls?: OrbitControls;
  private frameId?: number;
  private resizeObserver?: ResizeObserver;
  private modelRoot?: THREE.Object3D;
  private pickPoints: THREE.Vector3[] = [];
  private pickMarkers: THREE.Object3D[] = [];
  private raycaster = new THREE.Raycaster();
  // Orientation d'impression (Lot 2) : appliquée au modelRoot, indépendante de la
  // caméra/calibration. Remise à l'identité à chaque chargement de maillage (un
  // nouveau job REPAIR produit une nouvelle version, pas de raison de conserver
  // une rotation calculée sur l'ancienne géométrie).
  private printQuaternion = new THREE.Quaternion();

  // ── Peinture de parties (Lot 3) ──────────────────────────────────────────
  readonly paintedCount = signal(0);
  readonly brushLevel = signal(2); // index 1..5 dans BRUSH_RADIUS_RATIOS
  readonly eraseMode = signal(false);

  private paintMesh?: THREE.Mesh;
  private paintMaterial?: THREE.MeshStandardMaterial;
  private colorAttr?: THREE.BufferAttribute;
  private faceCentroidsLocal?: Float32Array;
  private modelDiagonal = 1;
  private currentSelection = new Set<number>();
  private partColorByFace = new Map<number, THREE.Color>();
  private spatialGrid = new Map<string, number[]>();
  private gridCellSize = 1;
  private painting = false;

  constructor(private zone: NgZone) {}

  ngAfterViewInit(): void {
    this.initScene();
    this.zone.runOutsideAngular(() => this.animate());
    if (this.meshUrl) this.loadMesh(this.meshUrl);
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['meshUrl'] && !changes['meshUrl'].firstChange && this.meshUrl) {
      this.loadMesh(this.meshUrl);
    }
    if (changes['calibrationMode'] && changes['calibrationMode'].currentValue === false) {
      this.clearPicks();
    }
    if (changes['axisMode'] && changes['axisMode'].currentValue === false) {
      this.clearPicks();
    }
    if (changes['paintMode']) {
      if (this.paintMode) {
        this.ensurePaintSetup();
        // Le pinceau peint au clic-glissé — le même geste que la rotation par
        // défaut d'OrbitControls (bouton gauche). Sans la désactiver ici, la
        // caméra tourne sous le curseur pendant le coup de pinceau, ce qui
        // peint des faces imprévisibles (vérifié : un traît de pinceau sur du
        // vide, hors du maillage, faisait quand même pivoter le modèle).
        if (this.controls) this.controls.enabled = false;
      } else {
        this.clearPaintSelection();
        if (this.paintMaterial) {
          this.paintMaterial.vertexColors = false;
          this.paintMaterial.needsUpdate = true;
        }
        if (this.controls) this.controls.enabled = true;
      }
    }
  }

  ngOnDestroy(): void {
    if (this.frameId) cancelAnimationFrame(this.frameId);
    this.resizeObserver?.disconnect();
    this.controls?.dispose();
    this.renderer?.dispose();
    window.removeEventListener('pointerup', this.onWindowPointerUp);
  }

  private initScene(): void {
    const host = this.canvasHost.nativeElement;
    this.scene.background = new THREE.Color(0x14171f);

    // Filet de sécurité : si la mise en page n'est pas encore résolue au moment
    // exact où ngAfterViewInit tourne, clientWidth/Height peuvent lire 0 — un
    // ratio d'aspect infini ou un canvas de hauteur nulle ne rendent plus rien
    // tant que le premier ResizeObserver (onResize) ne corrige pas la taille.
    const w = Math.max(1, host.clientWidth);
    const h = Math.max(1, host.clientHeight);

    this.camera = new THREE.PerspectiveCamera(50, w / h, 0.001, 10000);
    this.camera.position.set(1, 1, 1);

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h);
    host.appendChild(this.renderer.domElement);

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const dir = new THREE.DirectionalLight(0xffffff, 1.2);
    dir.position.set(5, 10, 7);
    this.scene.add(dir);
    const dir2 = new THREE.DirectionalLight(0xffffff, 0.4);
    dir2.position.set(-5, -3, -7);
    this.scene.add(dir2);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;

    this.renderer.domElement.addEventListener('click', (ev) => this.onCanvasClick(ev));
    this.renderer.domElement.addEventListener('pointerdown', (ev) => this.onPaintPointerDown(ev));
    this.renderer.domElement.addEventListener('pointermove', (ev) => this.onPaintPointerMove(ev));
    window.addEventListener('pointerup', this.onWindowPointerUp);

    this.resizeObserver = new ResizeObserver(() => this.onResize());
    this.resizeObserver.observe(host);
  }

  private onWindowPointerUp = (): void => { this.painting = false; };

  private onResize(): void {
    if (!this.renderer || !this.camera) return;
    const host = this.canvasHost.nativeElement;
    const w = host.clientWidth, h = host.clientHeight;
    if (w === 0 || h === 0) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  private animate = (): void => {
    this.frameId = requestAnimationFrame(this.animate);
    this.controls?.update();
    if (this.renderer && this.camera) this.renderer.render(this.scene, this.camera);
  };

  private loadMesh(url: string): void {
    this.loading.set(true);
    this.loadError.set(null);
    this.clearPicks();
    if (this.modelRoot) {
      this.scene.remove(this.modelRoot);
      this.modelRoot = undefined;
    }
    // Une nouvelle géométrie invalide tout ce qui était calculé pour la
    // peinture de faces (centroïdes, grille spatiale, sélection courante —
    // les indices de faces n'ont plus de sens sur un autre maillage).
    this.paintMesh = undefined;
    this.paintMaterial = undefined;
    this.colorAttr = undefined;
    this.faceCentroidsLocal = undefined;
    this.spatialGrid.clear();
    this.currentSelection.clear();
    this.paintedCount.set(0);

    new GLTFLoader().load(
      url,
      (gltf) => {
        this.modelRoot = gltf.scene;
        // Rendu double-face en sécurité : un maillage issu de reconstruction
        // photogrammétrique peut avoir des normales/un sens de bouclage
        // incohérents par endroits — le culling par défaut (FrontSide) rendrait
        // alors certaines faces invisibles selon l'angle de caméra.
        this.modelRoot.traverse((obj) => {
          const mesh = obj as THREE.Mesh;
          if (!mesh.isMesh) return;
          for (const m of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
            (m as THREE.Material).side = THREE.DoubleSide;
          }
        });
        this.applyWireframe(this.wireframe());
        this.scene.add(this.modelRoot);
        this.frameCamera(this.modelRoot);
        this.printQuaternion.identity();
        this.modelRoot.quaternion.copy(this.printQuaternion);
        this.orientationChanged.emit([0, 0, 0, 1]);
        this.loading.set(false);
        if (this.paintMode) this.ensurePaintSetup();
      },
      undefined,
      (err) => {
        console.error('Échec du chargement du maillage', err);
        this.loadError.set("Impossible de charger le maillage (export glTF manquant ou invalide).");
        this.loading.set(false);
      },
    );
  }

  private frameCamera(object: THREE.Object3D): void {
    if (!this.camera || !this.controls) return;
    const box = new THREE.Box3().setFromObject(object);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const distance = maxDim / (2 * Math.tan((Math.PI * this.camera.fov) / 360)) * 1.6;

    this.camera.near = maxDim / 1000;
    this.camera.far = maxDim * 100;
    this.camera.updateProjectionMatrix();
    this.camera.position.set(center.x + distance, center.y + distance * 0.6, center.z + distance);
    this.controls.target.copy(center);
    this.controls.update();
  }

  toggleWireframe(): void {
    this.wireframe.update((v) => !v);
    this.applyWireframe(this.wireframe());
  }

  private applyWireframe(enabled: boolean): void {
    this.modelRoot?.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const m of materials) {
        (m as THREE.MeshStandardMaterial).wireframe = enabled;
      }
    });
  }

  private onCanvasClick(ev: MouseEvent): void {
    if ((!this.calibrationMode && !this.axisMode) || !this.modelRoot || !this.camera || !this.renderer) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((ev.clientX - rect.left) / rect.width) * 2 - 1,
      -((ev.clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.raycaster.setFromCamera(ndc, this.camera);
    const hits = this.raycaster.intersectObject(this.modelRoot, true);
    if (hits.length === 0) return;

    const point = hits[0].point.clone();
    this.addPickMarker(point);
    this.pickPoints.push(point);
    this.pickedCount.set(this.pickPoints.length);

    if (this.pickPoints.length === 2) {
      if (this.calibrationMode) {
        const meshDistance = this.pickPoints[0].distanceTo(this.pickPoints[1]);
        this.measured.emit({ meshDistance });
      } else if (this.axisMode) {
        const origin = this.pickPoints[0];
        const direction = this.pickPoints[1].clone().sub(origin).normalize();
        this.axisPicked.emit({
          origin: [origin.x, origin.y, origin.z],
          direction: [direction.x, direction.y, direction.z],
        });
      }
    } else if (this.pickPoints.length > 2) {
      this.clearPicks();
    }
  }

  private addPickMarker(point: THREE.Vector3): void {
    const box = new THREE.Box3().setFromObject(this.modelRoot!);
    const scale = box.getSize(new THREE.Vector3()).length() * 0.006 || 0.01;
    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(scale, 12, 12),
      new THREE.MeshBasicMaterial({ color: 0xff5533 }),
    );
    marker.position.copy(point);
    this.scene.add(marker);
    this.pickMarkers.push(marker);

    if (this.pickPoints.length === 1) {
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([this.pickPoints[0], point]),
        new THREE.LineBasicMaterial({ color: 0xff5533 }),
      );
      this.scene.add(line);
      this.pickMarkers.push(line);
    }
  }

  clearPicks(): void {
    for (const m of this.pickMarkers) this.scene.remove(m);
    this.pickMarkers = [];
    this.pickPoints = [];
    this.pickedCount.set(0);
  }

  // ── Orientation d'impression (Lot 2) ────────────────────────────────────────
  /** Applique la suggestion renvoyée par GET /api/meshes/<id>/auto-orient/. */
  applySuggestedOrientation(quaternion: [number, number, number, number]): void {
    this.printQuaternion.set(quaternion[0], quaternion[1], quaternion[2], quaternion[3]);
    this.updateModelOrientation();
  }

  /** Ajustement manuel : rotation incrémentale autour d'un axe du maillage. */
  rotateStep(axis: 'x' | 'y' | 'z', degrees: number): void {
    const vec = axis === 'x' ? new THREE.Vector3(1, 0, 0)
      : axis === 'y' ? new THREE.Vector3(0, 1, 0)
      : new THREE.Vector3(0, 0, 1);
    const step = new THREE.Quaternion().setFromAxisAngle(vec, THREE.MathUtils.degToRad(degrees));
    this.printQuaternion.premultiply(step);
    this.updateModelOrientation();
  }

  resetOrientation(): void {
    this.printQuaternion.identity();
    this.updateModelOrientation();
  }

  private updateModelOrientation(): void {
    this.modelRoot?.quaternion.copy(this.printQuaternion);
    this.orientationChanged.emit([
      this.printQuaternion.x, this.printQuaternion.y, this.printQuaternion.z, this.printQuaternion.w,
    ]);
  }

  // ── Peinture de parties (Lot 3) ──────────────────────────────────────────
  /**
   * Prépare la géométrie pour la peinture : la première fois qu'on entre en
   * mode peinture pour ce maillage, convertit en non-indexé (chaque face a ses
   * 3 sommets propres, indispensable pour une couleur par face sans qu'elle ne
   * déborde sur les faces voisines qui partageraient autrement ces sommets),
   * ajoute un attribut de couleur, calcule les centroïdes de faces (repère
   * local, indépendant de toute rotation d'orientation du Lot 2) et construit
   * la grille spatiale utilisée par le pinceau.
   */
  private ensurePaintSetup(): void {
    if (!this.modelRoot) return;
    if (!this.paintMesh) {
      this.modelRoot.traverse((obj) => {
        if (this.paintMesh) return;
        const mesh = obj as THREE.Mesh;
        if (mesh.isMesh) this.paintMesh = mesh;
      });
    }
    if (!this.paintMesh) return;

    if (!this.colorAttr) {
      let geometry = this.paintMesh.geometry;
      if (geometry.index) {
        geometry = geometry.toNonIndexed();
        this.paintMesh.geometry = geometry;
      }
      const positions = geometry.getAttribute('position');
      const colors = new Float32Array(positions.count * 3);
      this.colorAttr = new THREE.BufferAttribute(colors, 3);
      geometry.setAttribute('color', this.colorAttr);

      const faceCount = positions.count / 3;
      this.faceCentroidsLocal = new Float32Array(faceCount * 3);
      for (let f = 0; f < faceCount; f++) {
        let x = 0, y = 0, z = 0;
        for (let v = 0; v < 3; v++) {
          x += positions.getX(f * 3 + v);
          y += positions.getY(f * 3 + v);
          z += positions.getZ(f * 3 + v);
        }
        this.faceCentroidsLocal[f * 3] = x / 3;
        this.faceCentroidsLocal[f * 3 + 1] = y / 3;
        this.faceCentroidsLocal[f * 3 + 2] = z / 3;
      }

      const box = new THREE.Box3().setFromBufferAttribute(positions as THREE.BufferAttribute);
      this.modelDiagonal = box.getSize(new THREE.Vector3()).length() || 1;
      this.rebuildSpatialGrid();
      this.repaintAllColors();
      // Accélère paintAt() (raycast à chaque déplacement de souris pendant un
      // coup de pinceau) — voir le commentaire en tête de fichier.
      geometry.computeBoundsTree();
    }

    const materials = Array.isArray(this.paintMesh.material) ? this.paintMesh.material : [this.paintMesh.material];
    this.paintMaterial = materials[0] as THREE.MeshStandardMaterial;
    this.paintMaterial.vertexColors = true;
    this.paintMaterial.needsUpdate = true;
  }

  private rebuildSpatialGrid(): void {
    this.gridCellSize = BRUSH_RADIUS_RATIOS[this.brushLevel() - 1] * this.modelDiagonal;
    this.spatialGrid.clear();
    if (!this.faceCentroidsLocal) return;
    const n = this.faceCentroidsLocal.length / 3;
    for (let f = 0; f < n; f++) {
      const key = this.cellKeyForFace(f);
      let bucket = this.spatialGrid.get(key);
      if (!bucket) { bucket = []; this.spatialGrid.set(key, bucket); }
      bucket.push(f);
    }
  }

  private cellKeyForFace(f: number): string {
    const c = this.faceCentroidsLocal!;
    const s = this.gridCellSize;
    return `${Math.floor(c[f * 3] / s)}_${Math.floor(c[f * 3 + 1] / s)}_${Math.floor(c[f * 3 + 2] / s)}`;
  }

  private queryRadius(point: THREE.Vector3, radius: number): number[] {
    if (!this.faceCentroidsLocal) return [];
    const s = this.gridCellSize;
    const cx = Math.floor(point.x / s), cy = Math.floor(point.y / s), cz = Math.floor(point.z / s);
    const span = Math.max(1, Math.ceil(radius / s));
    const r2 = radius * radius;
    const c = this.faceCentroidsLocal;
    const result: number[] = [];
    for (let dx = -span; dx <= span; dx++) {
      for (let dy = -span; dy <= span; dy++) {
        for (let dz = -span; dz <= span; dz++) {
          const bucket = this.spatialGrid.get(`${cx + dx}_${cy + dy}_${cz + dz}`);
          if (!bucket) continue;
          for (const f of bucket) {
            const ddx = c[f * 3] - point.x, ddy = c[f * 3 + 1] - point.y, ddz = c[f * 3 + 2] - point.z;
            if (ddx * ddx + ddy * ddy + ddz * ddz <= r2) result.push(f);
          }
        }
      }
    }
    return result;
  }

  private rebuildPartColorMap(): void {
    this.partColorByFace.clear();
    for (const part of this._existingParts) {
      const color = new THREE.Color(part.color);
      for (const f of part.faceIds) this.partColorByFace.set(f, color);
    }
  }

  private paintFaceColor(f: number): void {
    if (!this.colorAttr) return;
    const color = this.currentSelection.has(f)
      ? PAINT_HIGHLIGHT_COLOR
      : (this.partColorByFace.get(f) ?? PAINT_BASE_COLOR);
    for (let v = 0; v < 3; v++) this.colorAttr.setXYZ(f * 3 + v, color.r, color.g, color.b);
  }

  private repaintAllColors(): void {
    if (!this.colorAttr || !this.faceCentroidsLocal) return;
    const n = this.faceCentroidsLocal.length / 3;
    for (let f = 0; f < n; f++) this.paintFaceColor(f);
    this.colorAttr.needsUpdate = true;
  }

  private onPaintPointerDown(ev: PointerEvent): void {
    if (!this.paintMode) return;
    this.painting = true;
    this.paintAt(ev);
  }

  private onPaintPointerMove(ev: PointerEvent): void {
    if (!this.paintMode || !this.painting) return;
    this.paintAt(ev);
  }

  private paintAt(ev: PointerEvent): void {
    if (!this.paintMesh || !this.camera || !this.renderer || !this.colorAttr) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((ev.clientX - rect.left) / rect.width) * 2 - 1,
      -((ev.clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.raycaster.setFromCamera(ndc, this.camera);
    const hits = this.raycaster.intersectObject(this.paintMesh, false);
    if (hits.length === 0) return;

    const localPoint = this.paintMesh.worldToLocal(hits[0].point.clone());
    const radius = BRUSH_RADIUS_RATIOS[this.brushLevel() - 1] * this.modelDiagonal;
    const faces = this.queryRadius(localPoint, radius);
    let changed = false;
    for (const f of faces) {
      if (this.eraseMode()) {
        if (this.currentSelection.delete(f)) { this.paintFaceColor(f); changed = true; }
      } else if (!this.currentSelection.has(f)) {
        this.currentSelection.add(f);
        this.paintFaceColor(f);
        changed = true;
      }
    }
    if (changed) {
      this.colorAttr.needsUpdate = true;
      this.paintedCount.set(this.currentSelection.size);
    }
  }

  increaseBrush(): void {
    this.brushLevel.update((v) => Math.min(BRUSH_RADIUS_RATIOS.length, v + 1));
    this.rebuildSpatialGrid();
  }

  decreaseBrush(): void {
    this.brushLevel.update((v) => Math.max(1, v - 1));
    this.rebuildSpatialGrid();
  }

  toggleErase(): void {
    this.eraseMode.update((v) => !v);
  }

  /**
   * Charge une sélection existante dans le tampon de peinture (édition d'une
   * `Part`). Appelle `ensurePaintSetup()` elle-même : le composant parent peut
   * l'appeler juste après avoir positionné `paintMode` à `true` sans dépendre
   * de l'ordre d'exécution de `ngOnChanges` entre les deux composants.
   */
  loadPaintSelection(faceIds: number[]): void {
    this.ensurePaintSetup();
    this.currentSelection = new Set(faceIds);
    this.paintedCount.set(this.currentSelection.size);
    this.repaintAllColors();
  }

  clearPaintSelection(): void {
    this.currentSelection.clear();
    this.paintedCount.set(0);
    this.repaintAllColors();
  }

  getPaintedFaceIds(): number[] {
    return Array.from(this.currentSelection);
  }
}
