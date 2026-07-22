import {
  Component, ElementRef, Input, OnChanges, OnDestroy, SimpleChanges, ViewChild,
  AfterViewInit, NgZone, signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { Part, Joint } from '../../core/api.service';

interface JointNode {
  joint: Joint;
  group: THREE.Group;
  restPosition: THREE.Vector3;
  value: number;
}

const DEFAULT_ANGLE_LIMIT_DEG = 180;

/**
 * Arbre cinématique (Lot 3) : chaque `Part` devient un maillage séparé porté
 * par un noeud pivot (THREE.Group) positionné à l'origine de sa jointure
 * parente — pas de skinning, une hiérarchie rigide comme un assemblage CAO
 * (cf. to_do_3D.md). Un slider par jointure fait tourner/glisser le noeud, et
 * tout ce qui en dépend plus bas dans l'arbre suit automatiquement (propriété
 * standard d'une hiérarchie de scène three.js — l'axe d'une jointure enfant,
 * exprimé dans le repère local de son parent, tourne avec lui sans calcul
 * supplémentaire).
 */
@Component({
  selector: 'app-kinematic-preview',
  standalone: true,
  imports: [FormsModule, DecimalPipe],
  templateUrl: './kinematic-preview.component.html',
  styleUrl: './kinematic-preview.component.scss',
})
export class KinematicPreviewComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() meshUrl: string | null = null;
  @Input() parts: Part[] = [];
  @Input() joints: Joint[] = [];

  @ViewChild('canvasHost', { static: true }) private canvasHost!: ElementRef<HTMLDivElement>;

  readonly loading = signal(false);
  readonly loadError = signal<string | null>(null);
  readonly jointNodes = signal<JointNode[]>([]);

  private renderer?: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera?: THREE.PerspectiveCamera;
  private controls?: OrbitControls;
  private frameId?: number;
  private resizeObserver?: ResizeObserver;
  private root = new THREE.Group();
  private sourceGeometry?: THREE.BufferGeometry;
  private partGroups = new Map<number, THREE.Group>();
  private modelDiagonal = 1;

  constructor(private zone: NgZone) {}

  ngAfterViewInit(): void {
    this.initScene();
    this.zone.runOutsideAngular(() => this.animate());
    if (this.meshUrl) this.load();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['meshUrl'] && !changes['meshUrl'].firstChange && this.meshUrl) {
      this.load();
      return;
    }
    if ((changes['parts'] || changes['joints']) && this.sourceGeometry) {
      this.rebuildHierarchy();
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
    this.scene.add(this.root);

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

  private load(): void {
    if (!this.meshUrl) return;
    this.loading.set(true);
    this.loadError.set(null);
    new GLTFLoader().load(
      this.meshUrl,
      (gltf) => {
        let found: THREE.BufferGeometry | undefined;
        gltf.scene.traverse((obj) => {
          if (found) return;
          const mesh = obj as THREE.Mesh;
          if (mesh.isMesh) found = mesh.geometry;
        });
        if (!found) {
          this.loadError.set("Maillage introuvable dans le fichier glTF.");
          this.loading.set(false);
          return;
        }
        this.sourceGeometry = found.index ? found.toNonIndexed() : found;
        const box = new THREE.Box3().setFromBufferAttribute(
          this.sourceGeometry.getAttribute('position') as THREE.BufferAttribute,
        );
        this.modelDiagonal = box.getSize(new THREE.Vector3()).length() || 1;
        this.rebuildHierarchy();
        this.loading.set(false);
      },
      undefined,
      () => { this.loadError.set("Impossible de charger le maillage."); this.loading.set(false); },
    );
  }

  /** Point de pivot absolu (repère du maillage) autour duquel une partie tourne — [0,0,0] si racine. */
  private pivotPoint(partId: number, childToJoint: Map<number, Joint>): [number, number, number] {
    const joint = childToJoint.get(partId);
    return joint ? joint.axis_origin : [0, 0, 0];
  }

  private rebuildHierarchy(): void {
    if (!this.sourceGeometry) return;
    this.root.clear();
    this.partGroups.clear();

    const positions = this.sourceGeometry.getAttribute('position');
    const childToJoint = new Map<number, Joint>();
    for (const j of this.joints) childToJoint.set(j.child_part, j);

    // 1) un maillage par partie, sommets extraits de la géométrie source par
    // face_ids (mêmes indices que ceux peints dans le viewer principal).
    for (const part of this.parts) {
      const geom = new THREE.BufferGeometry();
      const verts = new Float32Array(part.face_ids.length * 3 * 3);
      let vi = 0;
      for (const f of part.face_ids) {
        for (let v = 0; v < 3; v++) {
          verts[vi++] = positions.getX(f * 3 + v);
          verts[vi++] = positions.getY(f * 3 + v);
          verts[vi++] = positions.getZ(f * 3 + v);
        }
      }
      geom.setAttribute('position', new THREE.BufferAttribute(verts, 3));
      geom.computeVertexNormals();
      const material = new THREE.MeshStandardMaterial({ color: new THREE.Color(part.color), side: THREE.DoubleSide });
      const mesh = new THREE.Mesh(geom, material);

      const p = this.pivotPoint(part.id, childToJoint);
      mesh.position.set(-p[0], -p[1], -p[2]);

      const group = new THREE.Group();
      group.add(mesh);
      this.partGroups.set(part.id, group);
    }

    // 2) attache chaque groupe à son parent (root si aucune jointure entrante),
    // positionné relativement au point de pivot du PARENT — voir docstring de
    // la classe : le reste suit automatiquement grâce à la hiérarchie.
    for (const part of this.parts) {
      const group = this.partGroups.get(part.id);
      if (!group) continue;
      const joint = childToJoint.get(part.id);
      if (!joint) {
        this.root.add(group);
        continue;
      }
      const parentPivot = this.pivotPoint(joint.parent_part, childToJoint);
      group.position.set(
        joint.axis_origin[0] - parentPivot[0],
        joint.axis_origin[1] - parentPivot[1],
        joint.axis_origin[2] - parentPivot[2],
      );
      const parentGroup = this.partGroups.get(joint.parent_part);
      (parentGroup ?? this.root).add(group);
    }

    this.frameCamera();

    this.jointNodes.set(
      this.joints
        .filter((joint) => this.partGroups.has(joint.child_part))
        .map((joint) => ({
          joint,
          group: this.partGroups.get(joint.child_part)!,
          restPosition: this.partGroups.get(joint.child_part)!.position.clone(),
          value: 0,
        })),
    );
  }

  private frameCamera(): void {
    if (!this.camera || !this.controls) return;
    const box = new THREE.Box3().setFromObject(this.root);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const distance = (maxDim / (2 * Math.tan((Math.PI * this.camera.fov) / 360))) * 1.6;

    this.camera.near = maxDim / 1000;
    this.camera.far = maxDim * 100;
    this.camera.updateProjectionMatrix();
    this.camera.position.set(center.x + distance, center.y + distance * 0.6, center.z + distance);
    this.controls.target.copy(center);
    this.controls.update();
  }

  sliderMin(joint: Joint): number {
    if (joint.limit_min != null) return joint.limit_min;
    return joint.joint_type === 'revolute' ? -DEFAULT_ANGLE_LIMIT_DEG : -0.1 * this.modelDiagonal;
  }

  sliderMax(joint: Joint): number {
    if (joint.limit_max != null) return joint.limit_max;
    return joint.joint_type === 'revolute' ? DEFAULT_ANGLE_LIMIT_DEG : 0.1 * this.modelDiagonal;
  }

  setJointValue(node: JointNode, value: number): void {
    node.value = value;
    const axis = new THREE.Vector3(...node.joint.axis_direction);
    if (axis.lengthSq() === 0) return;
    axis.normalize();
    if (node.joint.joint_type === 'revolute') {
      node.group.quaternion.setFromAxisAngle(axis, THREE.MathUtils.degToRad(value));
    } else if (node.joint.joint_type === 'prismatic') {
      node.group.position.copy(node.restPosition).addScaledVector(axis, value);
    }
  }

  resetJoint(node: JointNode): void {
    this.setJointValue(node, 0);
  }
}
