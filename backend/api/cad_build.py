"""
Job CAD_BUILD (Lot 5.1, module Conception CAO) : évalue l'historique
CadOperation d'un Project en un nouveau Mesh (STEP + glTF), via build123d
(noyau OCCT, mode « algébrique ») et cq_gears (denture) — cf. to_do_3D.md,
spike Lot 5.0 (mémoire atelier_3d_lot5_spike_findings). Réévalué en entier à
chaque édition, pas de cache incrémental (limite topologique assumée :
avertir si le nombre de faces change après réévaluation plutôt que
d'appliquer aveuglément les opérations suivantes sur de mauvais indices, cf.
to_do_3D.md) — cette détection vit dans la vue de lancement, pas ici.

Modèle d'évaluation : chaque CadOperation référence les opérations dont elle
dépend par leur id (pas par index de liste), et produit sa PROPRE forme,
stockée sous son id dans `results`. Rien n'est implicitement cumulé d'une
opération à l'autre — PRIMITIVE_*/SURFACE_FROM_WIRE sont toujours autonomes,
EXTRUDE/REVOLVE ne combinent avec une forme antérieure que si `target` est
fourni dans params, BOOLEAN_*/PATTERN_*/GEAR_TEETH référencent explicitement
la ou les opérations dont ils dépendent. Le résultat final du `Project` est
la forme de la DERNIÈRE opération de la liste (triée par `order`).

Schéma des `params` par type d'opération (cf. CadOperation.TYPE_CHOICES) :
  PRIMITIVE_BOX        {length, width, height, placement?}
  PRIMITIVE_SPHERE     {radius, placement?}
  PRIMITIVE_CYLINDER   {radius, height, placement?}
  PRIMITIVE_CONE       {bottom_radius, top_radius, height, placement?}
  PRIMITIVE_TORUS      {major_radius, minor_radius, placement?}
    placement (optionnel, sur toute primitive) : {position: [x,y,z],
    rotation_deg: [rx,ry,rz]}
  EXTRUDE / REVOLVE    {sketch_id, mode: ADD|CUT|INTERSECT, target?: <id>,
                        amount, both? (EXTRUDE), axis_origin?/axis_direction?
                        /angle_deg? (REVOLVE, défaut Z / 360°)}
    `target` optionnel : si absent, le résultat de la sketch est une forme
    autonome (mode alors ignoré) — sert à démarrer un nouveau corps. Si
    présent, combiné par `mode` avec la forme de l'opération `target`.
  SURFACE_FROM_WIRE    {sketch_id} — surface (Face) autonome, non solide.
  BOOLEAN_UNION/CUT/INTERSECT  {source_a: <id>, source_b: <id>}
  PATTERN_CIRCULAR     {source: <id>, count, radius, center? ([x,y,z]),
                        axis_direction? ([x,y,z], défaut Z), start_angle_deg?}
  PATTERN_LINEAR       {source: <id>, count, step: [dx,dy,dz]}
  GEAR_TEETH           {source: <id>, face_index, module, teeth_number,
                        pressure_angle? (défaut 20), width, internal? (bool,
                        défaut False)}
"""
import cadquery as cq
from cq_gears import SpurGear

import build123d as bd


class CadBuildError(Exception):
    """Erreur métier (paramètre manquant, référence cassée, face non
    cylindrique/conique...) — message affichable tel quel côté frontend."""


_PLANES = {'XY': bd.Plane.XY, 'XZ': bd.Plane.XZ, 'YZ': bd.Plane.YZ}
_BOOL_OPS = {
    'ADD': lambda a, b: a + b,
    'CUT': lambda a, b: a - b,
    'INTERSECT': lambda a, b: a & b,
}


def _require(params: dict, *keys):
    missing = [k for k in keys if k not in params or params[k] is None]
    if missing:
        raise CadBuildError(f"Paramètre(s) manquant(s) : {', '.join(missing)}")


def _vec(value, default=(0, 0, 0)):
    if value is None:
        return tuple(default)
    return tuple(value)


def _apply_placement(shape, placement):
    if not placement:
        return shape
    pos = _vec(placement.get('position'))
    rot = _vec(placement.get('rotation_deg'))
    return bd.Location(pos, rot) * shape


# ── Sketchs (CadSketch.entities → un unique Wire, ou Curve pour le cas ──────
# bspline ouverte + épaisseur) ───────────────────────────────────────────────
def _build_sketch_wire(entities: list):
    if not entities:
        raise CadBuildError("Sketch vide.")

    if len(entities) == 1 and entities[0]['type'] == 'bspline' and entities[0].get('thickness'):
        e = entities[0]
        _require(e, 'points', 'thickness')
        pts = [tuple(p) for p in e['points']]
        curve = bd.Spline(*pts)
        return bd.offset(curve, amount=e['thickness'])

    edges = None
    for e in entities:
        t = e['type']
        if t == 'segment':
            _require(e, 'start', 'end')
            edge = bd.Line(tuple(e['start']), tuple(e['end']))
        elif t == 'polyline':
            _require(e, 'points')
            pts = [tuple(p) for p in e['points']]
            edge = bd.Polyline(*pts, close=bool(e.get('closed', False)))
        elif t == 'circle':
            _require(e, 'center', 'radius')
            edge = bd.CenterArc(tuple(e['center']), radius=e['radius'], start_angle=0, arc_size=360)
        elif t == 'arc':
            _require(e, 'start', 'mid', 'end')
            edge = bd.ThreePointArc(tuple(e['start']), tuple(e['mid']), tuple(e['end']))
        elif t == 'bspline':
            _require(e, 'points')
            pts = [tuple(p) for p in e['points']]
            edge = bd.Spline(*pts)
        else:
            raise CadBuildError(f"Type d'entité de sketch inconnu : {t}")
        edges = edge if edges is None else edges + edge
    return edges


def _build_sketch_face(sketch):
    plane = _PLANES.get(sketch.plane, bd.Plane.XY)
    wire = _build_sketch_wire(sketch.entities)
    wire = plane * wire
    return bd.make_face(wire)


# ── Une opération ────────────────────────────────────────────────────────────
def _eval_primitive(op_type: str, params: dict):
    if op_type == 'PRIMITIVE_BOX':
        _require(params, 'length', 'width', 'height')
        shape = bd.Box(params['length'], params['width'], params['height'])
    elif op_type == 'PRIMITIVE_SPHERE':
        _require(params, 'radius')
        shape = bd.Sphere(params['radius'])
    elif op_type == 'PRIMITIVE_CYLINDER':
        _require(params, 'radius', 'height')
        shape = bd.Cylinder(params['radius'], params['height'])
    elif op_type == 'PRIMITIVE_CONE':
        _require(params, 'bottom_radius', 'top_radius', 'height')
        shape = bd.Cone(params['bottom_radius'], params['top_radius'], params['height'])
    elif op_type == 'PRIMITIVE_TORUS':
        _require(params, 'major_radius', 'minor_radius')
        shape = bd.Torus(params['major_radius'], params['minor_radius'])
    else:
        raise CadBuildError(f"Primitive inconnue : {op_type}")
    return _apply_placement(shape, params.get('placement'))


def _shape_kind(shape) -> str:
    return 'solide' if shape.solids() else ('surface' if shape.faces() else 'forme')


def _ensure_combinable(a, b) -> None:
    """
    OCCT échoue de façon opaque ("Null TopoDS_Shape object", sans autre
    contexte) quand on fusionne/soustrait une surface (SURFACE_FROM_WIRE) et
    un solide (primitive, EXTRUDE/REVOLVE...) — un booléen ne peut combiner
    que deux formes du même type. Vérifié en conditions réelles (Lot 5.1) :
    lever une erreur explicite ici plutôt que laisser remonter l'exception
    OCCT brute, incompréhensible pour l'utilisateur.
    """
    ka, kb = _shape_kind(a), _shape_kind(b)
    if ka != kb:
        raise CadBuildError(
            f"Impossible de combiner un élément de type {ka} avec un élément de type {kb} par un "
            "booléen — les deux opérations combinées doivent être du même type (deux solides, ou "
            "deux surfaces)."
        )


def _eval_extrude_revolve(op_type: str, params: dict, sketches_by_id: dict, results: dict):
    _require(params, 'sketch_id', 'mode')
    sketch = sketches_by_id.get(params['sketch_id'])
    if sketch is None:
        raise CadBuildError(f"CadSketch id={params['sketch_id']} introuvable pour ce projet.")
    face = _build_sketch_face(sketch)

    if op_type == 'EXTRUDE':
        _require(params, 'amount')
        shape = bd.extrude(face, amount=params['amount'], both=bool(params.get('both', False)))
    else:  # REVOLVE
        axis = bd.Axis(_vec(params.get('axis_origin')), _vec(params.get('axis_direction'), (0, 0, 1)))
        shape = bd.revolve(face, axis=axis, revolution_arc=params.get('angle_deg', 360))

    target_id = params.get('target')
    if target_id is None:
        return shape
    if target_id not in results:
        raise CadBuildError(f"Opération cible id={target_id} introuvable (ordre invalide ?).")
    op_fn = _BOOL_OPS.get(params['mode'])
    if op_fn is None:
        raise CadBuildError(f"Mode de combinaison inconnu : {params['mode']}")
    _ensure_combinable(results[target_id], shape)
    return op_fn(results[target_id], shape)


def _eval_boolean(op_type: str, params: dict, results: dict):
    _require(params, 'source_a', 'source_b')
    a = results.get(params['source_a'])
    b = results.get(params['source_b'])
    if a is None or b is None:
        raise CadBuildError("source_a/source_b doivent référencer des opérations antérieures de ce projet.")
    _ensure_combinable(a, b)
    mode = {'BOOLEAN_UNION': 'ADD', 'BOOLEAN_CUT': 'CUT', 'BOOLEAN_INTERSECT': 'INTERSECT'}[op_type]
    return _BOOL_OPS[mode](a, b)


def _eval_pattern(op_type: str, params: dict, results: dict):
    _require(params, 'source', 'count')
    source = results.get(params['source'])
    if source is None:
        raise CadBuildError(f"Opération source id={params['source']} introuvable.")
    count = int(params['count'])
    if count < 1:
        raise CadBuildError("'count' doit être >= 1.")

    if op_type == 'PATTERN_CIRCULAR':
        _require(params, 'radius')
        center = _vec(params.get('center'))
        axis_dir = _vec(params.get('axis_direction'), (0, 0, 1))
        start_angle = params.get('start_angle_deg', 0)
        # PolarLocations répartit `count` positions sur un cercle de rayon
        # `radius` dans le plan XY (axe Z) ; on ramène ensuite ce cercle sur
        # l'axe/centre demandés via Axis.location (position + orientation en
        # un seul transform, cf. spike : bien plus simple qu'une composition
        # manuelle rotation+translation).
        locs = bd.PolarLocations(radius=params['radius'], count=count, start_angle=start_angle)
        axis = bd.Axis(center, axis_dir)
        copies = [axis.location * loc * source for loc in locs]
    elif op_type == 'PATTERN_LINEAR':
        _require(params, 'step')
        step = _vec(params['step'])
        copies = [bd.Location((step[0] * i, step[1] * i, step[2] * i)) * source for i in range(count)]
    else:
        raise CadBuildError(f"Type de pattern inconnu : {op_type}")

    result = copies[0]
    for c in copies[1:]:
        result = result + c
    return result


def _eval_gear_teeth(params: dict, results: dict):
    _require(params, 'source', 'face_index', 'module', 'teeth_number', 'width')
    source = results.get(params['source'])
    if source is None:
        raise CadBuildError(f"Opération source id={params['source']} introuvable.")

    faces = source.faces()
    idx = params['face_index']
    if idx < 0 or idx >= len(faces):
        raise CadBuildError(f"face_index {idx} hors limites (0..{len(faces) - 1}).")
    face = faces[idx]
    if face.geom_type not in (bd.GeomType.CYLINDER, bd.GeomType.CONE):
        raise CadBuildError(
            f"La face désignée (index {idx}) n'est pas cylindrique/conique (type détecté : "
            f"{face.geom_type}) — la denture ne peut être posée que sur ce type de face."
        )

    gear = SpurGear(
        module=params['module'],
        teeth_number=params['teeth_number'],
        width=params['width'],
        pressure_angle=params.get('pressure_angle', 20),
    )
    gear_wp = cq.Workplane('XY').gear(gear)
    gear_shape = bd.Solid(gear_wp.val().wrapped)

    # cq_gears construit toujours l'engrenage avec le centre de sa face basse
    # à l'origine locale et son axe selon Z (vérifié au spike) — Axis.location
    # donne en un seul transform la position + l'orientation qui envoient
    # (origine, Z local) sur (axis.position, axis.direction), bien plus simple
    # qu'une composition manuelle rotation+translation.
    axis = face.axis_of_rotation
    gear_shape = axis.location * gear_shape

    mode = 'CUT' if params.get('internal') else 'ADD'
    return _BOOL_OPS[mode](source, gear_shape)


def evaluate_operations(operations, sketches_by_id: dict):
    """
    Évalue une liste de `CadOperation` (triée par `order`) et retourne la
    forme de la dernière — cf. docstring du module pour le schéma `params`
    attendu par type. Lève `CadBuildError` avec un message affichable tel
    quel en cas de paramètre manquant ou de référence cassée.
    """
    results = {}
    last = None
    for op in operations:
        op_type = op.operation_type
        params = op.params or {}
        try:
            if op_type.startswith('PRIMITIVE_'):
                shape = _eval_primitive(op_type, params)
            elif op_type in ('EXTRUDE', 'REVOLVE'):
                shape = _eval_extrude_revolve(op_type, params, sketches_by_id, results)
            elif op_type == 'SURFACE_FROM_WIRE':
                _require(params, 'sketch_id')
                sketch = sketches_by_id.get(params['sketch_id'])
                if sketch is None:
                    raise CadBuildError(f"CadSketch id={params['sketch_id']} introuvable.")
                shape = _build_sketch_face(sketch)
            elif op_type in ('BOOLEAN_UNION', 'BOOLEAN_CUT', 'BOOLEAN_INTERSECT'):
                shape = _eval_boolean(op_type, params, results)
            elif op_type in ('PATTERN_CIRCULAR', 'PATTERN_LINEAR'):
                shape = _eval_pattern(op_type, params, results)
            elif op_type == 'GEAR_TEETH':
                shape = _eval_gear_teeth(params, results)
            else:
                raise CadBuildError(f"Type d'opération inconnu : {op_type}")
        except Exception as exc:
            raise CadBuildError(f"Échec de l'opération #{op.order} ({op_type}) : {exc}") from exc
        results[op.id] = shape
        last = shape
    if last is None:
        raise CadBuildError("Aucune opération à évaluer.")
    return last
