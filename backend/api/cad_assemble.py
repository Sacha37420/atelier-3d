"""
Job CAD_ASSEMBLE (Lot 5.2, module Conception CAO) : résout un
`Project(project_type=ASSEMBLY)` — place chaque `CadAssemblyInstance`
(référence un `Project` sous-partie + son `Mesh` précis, cf. to_do_3D.md limite
topologique n°2) selon les `CadAssemblyConstraint` posées, via FreeCAD headless
(workbench Assembly, solveur OndselSolver) — cf. spike Lot 5.0 (mémoire
atelier_3d_lot5_spike_findings) et vérifications réelles avant écriture
(mémoire atelier_3d_lot5_2_assembly_findings).

Architecture en 2 process, PAS un import direct : `FreeCAD`/`Part`/
`JointObject` ne sont PAS des paquets pip du python système (celui de Django/
Celery) — ce sont les modules embarqués dans l'AppImage FreeCAD, uniquement
importables depuis l'interpréteur Python propre à `freecadcmd` (vérifié :
`import FreeCAD` échoue avec ModuleNotFoundError dans le worker Celery, cf.
PATH bug du spike Lot 5.0 — même cause : deux pythons distincts). Donc :

- Ce module (`cad_assemble.py`, importé par `tasks.py`) tourne côté Django/
  Celery : il sérialise le nécessaire (chemins STEP, contraintes, rayons de
  denture précalculés depuis l'ORM) dans un JSON, lance `freecadcmd
  cad_assemble_freecad_worker.py <in.txt> <out.txt>` en sous-processus (même
  pattern que COLMAP/OpenMVS dans reconstruction.py), lit le résultat.
- `cad_assemble_freecad_worker.py` (script autonome, stdlib + FreeCAD
  seulement, PAS de Django) fait le travail FreeCAD réel et écrit le JSON de
  sortie — cf. ce fichier pour la mécanique des Joints elle-même.

Piège réel trouvé en vérifiant ce pipeline : `freecadcmd script.py a.json
b.json` n'atteint PAS `sys.argv` sans effet de bord — `freecadcmd` inspecte
CHAQUE argument et, si son extension correspond à un importeur connu (`.json`
→ maillage FEM YAML/JSON, `.txt` → maillage FEM Z88 !), tente de l'OUVRIR comme
un document avant même d'exécuter le script — avec parfois une exception FATALE
qui interrompt tout (`importZ88Mesh.py` plante sur un `.txt` qui n'est pas un
vrai maillage Z88). Un nom de fichier SANS extension reste juste un
« File format not supported » (message, pas une exception) et arrive intact
dans `sys.argv` — d'où les fichiers d'échange sans extension ci-dessous.
"""
import json
import subprocess
from pathlib import Path

from . import storage_backend


class CadAssembleError(Exception):
    """Erreur métier (contrainte incomplète, référence cassée, solveur en
    échec...) — message affichable tel quel côté frontend."""


_WORKER_SCRIPT = Path(__file__).resolve().parent / 'cad_assemble_freecad_worker.py'


def _gear_pitch_radius(source_project) -> float:
    op = source_project.cad_operations.filter(operation_type='GEAR_TEETH').order_by('order').first()
    if op is None:
        raise CadAssembleError(
            f"Contrainte GEAR_MESH : le projet « {source_project.name} » ne contient "
            f"aucune opération GEAR_TEETH (denture) pour en dériver le rayon primitif."
        )
    params = op.params or {}
    module = params.get('module')
    teeth = params.get('teeth_number')
    if module is None or teeth is None:
        raise CadAssembleError(
            f"Opération GEAR_TEETH du projet « {source_project.name} » incomplète (module/teeth_number)."
        )
    return module * teeth / 2.0


def _build_payload(assembly_project, output_step_path: str, workdir: Path) -> dict:
    instances = list(assembly_project.cad_instances.select_related('source_project', 'source_mesh').all())
    if not instances:
        raise CadAssembleError("Aucune CadAssemblyInstance dans cet assemblage.")

    constraints = list(assembly_project.cad_constraints.select_related('instance_a', 'instance_b').all())
    if not constraints:
        raise CadAssembleError("Aucune CadAssemblyConstraint posée — rien à résoudre.")
    if not any(c.constraint_type == 'FIXED' for c in constraints):
        raise CadAssembleError(
            "Aucune contrainte FIXED : l'assemblage doit être ancré au monde par au moins une instance."
        )

    instances_payload = []
    for instance in instances:
        if not instance.source_mesh.step_file:
            raise CadAssembleError(
                f"L'instance « {instance.label or instance.source_project.name} » "
                f"référence un Mesh sans STEP (pas produit par CAD_BUILD)."
            )
        # freecadcmd tourne dans un process séparé, sans accès à storage (cf.
        # docstring de ce module) : chaque STEP doit exister en local avant de
        # lancer le sous-processus. Matérialisé dans workdir (pas via
        # local_copy) : plusieurs fichiers doivent survivre en même temps,
        # jusqu'à la fin de resolve_assembly.
        step_local = workdir / f'input_{instance.id}.step'
        storage_backend.download_to(instance.source_mesh.step_file, step_local)
        instances_payload.append({'id': instance.id, 'step_path': str(step_local)})

    constraints_payload = []
    for c in constraints:
        entry = {
            'id': c.id,
            'type': c.constraint_type,
            'instance_a': c.instance_a_id,
            'instance_b': c.instance_b_id,
            'reference_a': c.reference_a,
            'reference_b': c.reference_b,
            'params': c.params or {},
        }
        if c.constraint_type == 'GEAR_MESH':
            entry['gear_radius_a'] = _gear_pitch_radius(c.instance_a.source_project)
            entry['gear_radius_b'] = _gear_pitch_radius(c.instance_b.source_project)
        constraints_payload.append(entry)

    return {
        'instances': instances_payload,
        'constraints': constraints_payload,
        'output_step_path': output_step_path,
    }


def resolve_assembly(assembly_project, workdir: Path) -> dict:
    """
    Résout `assembly_project` (Project(ASSEMBLY)) en sous-processus freecadcmd.
    Écrit le STEP combiné dans `workdir/assembly_result.step` et retourne
    `{instance_id (str): {'position': [...], 'quaternion_xyzw': [...]}}`.
    Lève `CadAssembleError` (message affichable tel quel) en cas d'échec —
    contrainte incomplète, référence cassée, ou solveur FreeCAD en échec (le
    message du solveur est répercuté tel quel, jamais avalé, cf. to_do_3D.md).
    """
    output_step_path = str(workdir / 'assembly_result.step')
    payload = _build_payload(assembly_project, output_step_path, workdir)

    in_path = workdir / 'assemble_input'
    out_path = workdir / 'assemble_output'
    in_path.write_text(json.dumps(payload))

    proc = subprocess.run(
        ['freecadcmd', str(_WORKER_SCRIPT), str(in_path), str(out_path)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise CadAssembleError(
            f"Échec du solveur FreeCAD (code {proc.returncode}) : "
            f"{proc.stderr.strip()[-2000:] or proc.stdout.strip()[-2000:]}"
        )

    result = json.loads(out_path.read_text())
    if result.get('error'):
        raise CadAssembleError(result['error'])

    return result['placements']
