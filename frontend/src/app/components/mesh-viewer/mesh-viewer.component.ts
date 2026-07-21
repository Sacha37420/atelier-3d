import {
  Component, ElementRef, EventEmitter, Input, Output, ViewChild,
  AfterViewInit, OnDestroy, OnChanges, SimpleChanges, NgZone, signal,
} from '@angular/core';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

export interface CalibrationMeasurement {
  /** Distance entre les deux points cliqués, dans l'unité brute du maillage. */
  meshDistance: number;
}

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
  @Output() measured = new EventEmitter<CalibrationMeasurement>();

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
  }

  ngOnDestroy(): void {
    if (this.frameId) cancelAnimationFrame(this.frameId);
    this.resizeObserver?.disconnect();
    this.controls?.dispose();
    this.renderer?.dispose();
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

    this.resizeObserver = new ResizeObserver(() => this.onResize());
    this.resizeObserver.observe(host);
  }

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
        this.loading.set(false);
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
    if (!this.calibrationMode || !this.modelRoot || !this.camera || !this.renderer) return;
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
      const meshDistance = this.pickPoints[0].distanceTo(this.pickPoints[1]);
      this.measured.emit({ meshDistance });
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
}
