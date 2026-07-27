"""
Worker autonome exécuté par `freecadcmd` (PAS par le python système/Django,
cf. docstring de cad_assemble.py — deux interpréteurs Python distincts).
Usage : `freecadcmd cad_assemble_freecad_worker.py <input> <output> (sans extension)`.

Entrée (JSON) : {instances: [{id, step_path}], constraints: [{id, type,
instance_a, instance_b, reference_a, reference_b, params, gear_radius_a?,
gear_radius_b?}], output_step_path}.
Sortie (JSON) : {placements: {instance_id (str): {position, quaternion_xyzw}}}
en cas de succès, ou {error: "..."} sinon (jamais d'exception non capturée :
le process appelant lit toujours un JSON, cf. cad_assemble.py).

Mécanique des Joints (vérifiée en réel sur les pièces push-machine avant
d'écrire ce fichier, cf. mémoire atelier_3d_lot5_2_assembly_findings) :
- reference {kind, index} (index 0-based, cohérent avec CadOperation
  (GEAR_TEETH).face_index du Lot 5.1) -> nom d'élément FreeCAD ("Face{i+1}" /
  "Edge{i+1}" / "Vertex{i+1}"), DUPLIQUÉ deux fois dans setJointConnectors
  ([nom, nom]) : déclenche le calcul du connecteur au CENTRE de la face (ou
  centre du cercle si edge circulaire) plutôt qu'à l'identité — un seul nom
  laisse Placement1/2 à l'identité (bug réel trouvé en spike : le 2e élément
  de la paire doit être du même type que le 1er pour que
  UtilsAssembly.findPlacement prenne le chemin « centre »).
- FIXED (reference_b nul) -> JointObject.GroundedJoint (ancre au monde), PAS
  un JointObject de type "Fixed" (qui lie deux pièces entre elles, pas au
  monde — vérifié dans JointObject.py).
- GEAR_MESH -> joint "Gears", Distance/Distance2 = gear_radius_a/b (précalculés
  côté Django depuis CadOperation(GEAR_TEETH), jamais ici).
"""
import json
import sys

sys.path.append('/opt/freecad/usr/Mod/Assembly')

import FreeCAD as App  # noqa: E402
import Part  # noqa: E402
import JointObject  # noqa: E402

CONSTRAINT_TO_JOINT = {
    'COINCIDENT': 'Ball',
    'CONTACT': 'Slider',
    'PARALLEL': 'Parallel',
    'CONCENTRIC': 'Revolute',
    'DISTANCE': 'Distance',
    'ANGLE': 'Angle',
}


def element_name(reference):
    prefix = {'face': 'Face', 'edge': 'Edge', 'vertex': 'Vertex'}[reference['kind']]
    return f"{prefix}{reference['index'] + 1}"


def main(in_path, out_path):
    # encoding explicite : le python embarque de freecadcmd ne peuple pas
    # forcement un environnement UTF-8 par defaut (locale minimale du
    # conteneur) - open() sans encoding= peut alors retomber sur 'ascii' et
    # planter (verifie en reel : 'ascii' codec can't decode byte 0xc3...),
    # avant meme d'atteindre le try/except plus bas.
    try:
        with open(in_path, encoding='utf-8') as fh:
            payload = json.loads(fh.read())
    except Exception as exc:
        with open(out_path, 'w', encoding='utf-8') as fh:
            json.dump({'error': f'Lecture entree impossible : {exc}'}, fh)
        return

    doc = App.newDocument('cad_assemble')
    try:
        assembly = doc.addObject('Assembly::AssemblyObject', 'Assembly')
        joint_group = assembly.newObject('Assembly::JointGroup', 'Joints')

        parts = {}
        for inst in payload['instances']:
            shape = Part.Shape()
            shape.read(inst['step_path'])
            part = assembly.newObject('Part::Feature', f"Part{inst['id']}")
            part.Shape = shape
            parts[inst['id']] = part

        for c in payload['constraints']:
            part_a = parts[c['instance_a']]

            if c['type'] == 'FIXED':
                ground = joint_group.newObject('App::FeaturePython', f"Ground{c['id']}")
                JointObject.GroundedJoint(ground, part_a)
                continue

            part_b = parts[c['instance_b']]
            name_a = element_name(c['reference_a'])
            name_b = element_name(c['reference_b'])
            joint_type = 'Gears' if c['type'] == 'GEAR_MESH' else CONSTRAINT_TO_JOINT[c['type']]

            joint = joint_group.newObject('App::FeaturePython', f"Joint{c['id']}")
            JointObject.Joint(joint, JointObject.JointTypes.index(joint_type))
            joint.Proxy.setJointConnectors(joint, [
                [part_a, [name_a, name_a]],
                [part_b, [name_b, name_b]],
            ])

            params = c.get('params') or {}
            if c['type'] == 'DISTANCE':
                joint.Distance = params['distance_mm']
            elif c['type'] == 'ANGLE':
                joint.Angle = params['angle_deg']
            elif c['type'] == 'GEAR_MESH':
                joint.Distance = c['gear_radius_a']
                joint.Distance2 = c['gear_radius_b']

        App.setActiveTransaction('Solve assembly')
        try:
            assembly.recompute(True)
        finally:
            App.closeActiveTransaction()

        state = assembly.State or []
        if any('Error' in s for s in state):
            raise RuntimeError(f"Échec du solveur FreeCAD : {', '.join(state)}")

        # Normalisation par composante connexe : le solveur ne garantit PAS
        # qu'une instance FIXED reste a l'identite apres resolution (verifie
        # en reel : avec plusieurs joints/branches rattaches, le corps
        # "grounde" se retrouve deplace/tourne de facon arbitraire mais
        # cohérente avec le reste — cf. mémoire atelier_3d_lot5_2_assembly_findings).
        # On recadre donc chaque composante connexe (reliee par des
        # contraintes non-FIXED) sur son ancre FIXED, qu'on force a
        # l'identite : correction = inverse(placement resolu de l'ancre),
        # appliquee a toutes les instances de la meme composante.
        parent = {inst['id']: inst['id'] for inst in payload['instances']}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for c in payload['constraints']:
            if c['type'] != 'FIXED' and c.get('instance_b') is not None:
                union(c['instance_a'], c['instance_b'])

        anchor_of_component = {}
        for c in payload['constraints']:
            if c['type'] == 'FIXED':
                anchor_of_component[find(c['instance_a'])] = c['instance_a']

        correction_of_component = {
            root: parts[anchor_id].Placement.inverse()
            for root, anchor_id in anchor_of_component.items()
        }

        for inst in payload['instances']:
            root = find(inst['id'])
            if root in correction_of_component:
                part = parts[inst['id']]
                part.Placement = correction_of_component[root] * part.Placement

        placements = {}
        shapes = []
        for inst in payload['instances']:
            part = parts[inst['id']]
            plc = part.Placement
            placements[str(inst['id'])] = {
                'position': [plc.Base.x, plc.Base.y, plc.Base.z],
                'quaternion_xyzw': [plc.Rotation.Q[0], plc.Rotation.Q[1], plc.Rotation.Q[2], plc.Rotation.Q[3]],
            }
            shapes.append(part.Shape.copy())

        combined = Part.makeCompound(shapes)
        combined.exportStep(payload['output_step_path'])

        with open(out_path, 'w', encoding='utf-8') as fh:
            json.dump({'placements': placements}, fh)
    except Exception as exc:
        with open(out_path, 'w', encoding='utf-8') as fh:
            json.dump({'error': str(exc)}, fh)
    finally:
        App.closeDocument(doc.Name)


# `freecadcmd` exécute ce script avec __name__ = nom du module (ex: le nom du
# fichier sans extension), JAMAIS '__main__' (vérifié en réel : le classique
# `if __name__ == '__main__':` ne se déclenche jamais ici, le script se termine
# silencieusement sans rien faire) — donc appel inconditionnel. ET surtout :
# `sys.argv[0]` reste `'freecadcmd'` et `sys.argv[1]` est CE SCRIPT lui-même
# (contrairement à l'invocation `python script.py arg1 arg2` normale où
# argv[0] est le script) — les vrais arguments sont donc en [2]/[3], pas
# [1]/[2] (bug réel trouvé : avec l'ancien indexage, `in_path` valait le
# chemin de CE FICHIER .py, lu comme "entrée", et `out_path` écrasait le
# fichier d'entrée réel avec un message d'erreur).
main(sys.argv[2], sys.argv[3])
