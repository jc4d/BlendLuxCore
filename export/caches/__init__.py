import bpy
from ... import utils
from ...utils import EXPORTABLE_OBJECTS
from .. import camera, material

from .object_cache import ObjectCache2, supports_live_transform


class StringCache:
    def __init__(self):
        self.props = None

    def diff(self, new_props):
        props_str = str(self.props)
        new_props_str = str(new_props)

        if self.props is None:
            # Not initialized yet
            self.props = new_props
            return True

        has_changes = props_str != new_props_str
        self.props = new_props
        return has_changes


class CameraCache:
    def __init__(self):
        self.string_cache = StringCache()

    @property
    def props(self):
        return self.string_cache.props

    def diff(self, exporter, scene, depsgraph, context):
        # String cache
        camera_props = camera.convert(exporter, scene, depsgraph, context)
        has_changes = self.string_cache.diff(camera_props)

        # Check camera object and data for changes
        # Needed in case the volume node tree was relinked/unlinked
        # TODO 2.8
        # if scene.camera and (scene.camera.is_updated or scene.camera.is_updated_data):
        #     return True

        return has_changes


# UI-only node properties that don't affect rendering.
# These are standard Blender node properties (bpy.types.Node) that represent
# display state rather than render-relevant values, so we exclude them from
# the hash to avoid spurious render restarts when moving or selecting nodes.
# Full list of bpy.types.Node properties:
# https://docs.blender.org/api/current/bpy.types.Node.html
IGNORE_PROPS = {
    # UI/display properties
    'location',
    'location_absolute',
    'width',
    'height',
    'select',
    'hide',
    'show_options',
    'show_preview',
    'show_texture',
    'use_custom_color',
    'bl_width_default',
    'bl_width_min',
    'bl_width_max',
    'bl_height_default',
    'bl_height_min',
    'bl_height_max',
    # Node metadata - not render relevant
    'rna_type',
    'name',
    'label',
    'bl_label',
    'bl_description',
    'bl_icon',
    'bl_static_type',
    'bl_idname',
    'color',
    'color_tag',
    'warning_propagation',
    'dimensions',
    'inputs',
    'outputs',
    'internal_links',
    'parent',
}


def _hash_node_tree(tree, output_idnames):
    """Hash all render-relevant properties of nodes reachable from the output node.
    Only nodes connected to the output are traversed, so disconnected nodes
    do not affect the hash and will not trigger unnecessary render restarts."""
    output = None
    for idname in output_idnames:
        output = next(
            (n for n in tree.nodes if n.bl_idname == idname and getattr(n, 'is_active_output', False)),
            None
        ) or next(
            (n for n in tree.nodes if n.bl_idname == idname),
            None
        )
        if output:
            break
    if not output:
        return None

    visited = set()
    data = []

    def traverse(node):
        if node in visited:
            return
        visited.add(node)
        data.append(node.bl_idname)
        for inp in node.inputs:
            if not inp.is_linked and hasattr(inp, 'default_value'):
                val = inp.default_value
                if hasattr(val, '__iter__') and not isinstance(val, str):
                    val = tuple(val)
                data.append((inp.name, str(val)))
            for link in inp.links:
                traverse(link.from_node)
        # Hash output socket values to capture source nodes like RGB and Value
        # whose value is stored in outputs rather than inputs
        for out in node.outputs:
            if hasattr(out, 'default_value'):
                val = out.default_value
                if hasattr(val, '__iter__') and not isinstance(val, str):
                    val = tuple(val)
                data.append(('out_' + out.name, str(val)))
        for prop in node.bl_rna.properties:
            if prop.identifier in IGNORE_PROPS:
                continue
            if prop.type in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
                data.append((prop.identifier, str(getattr(node, prop.identifier))))
            elif prop.type == 'POINTER':
                # Use the name of the pointed-to object as a stable identifier
                # e.g. for image textures, this captures which image is assigned
                val = getattr(node, prop.identifier, None)
                if val is not None and hasattr(val, 'name'):
                    data.append((prop.identifier, val.name))

    traverse(output)
    return hash(tuple(data))


def _get_material_hash(mat):
    # Guard against materials with no LuxCore properties,
    # e.g. after a material slot is removed
    if not hasattr(mat, 'luxcore'):
        return None
    if mat.luxcore.node_tree and not mat.luxcore.use_cycles_nodes:
        tree = mat.luxcore.node_tree
        output_idnames = ['LuxCoreNodeMatOutput']
    else:
        tree = mat.node_tree
        output_idnames = ['ShaderNodeOutputMaterial']
    if not tree:
        return None
    return _hash_node_tree(tree, output_idnames)


def _hash_world(world):
    lux = world.luxcore

    # When using Cycles settings, hash the Cycles world node tree
    if lux.use_cycles_settings and world.node_tree:
        return _hash_node_tree(world.node_tree, ['ShaderNodeOutputWorld'])

    # LuxCore native world settings: hash all render-relevant properties
    data = []
    for prop in lux.bl_rna.properties:
        if prop.type in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
            val = getattr(lux, prop.identifier)
            if hasattr(val, '__iter__') and not isinstance(val, str):
                val = tuple(val)
            data.append((prop.identifier, str(val)))
        elif prop.type == 'POINTER':
            # Use the name of the pointed-to object as a stable identifier
            val = getattr(lux, prop.identifier, None)
            if val is not None and hasattr(val, 'name'):
                data.append((prop.identifier, val.name))
    return hash(tuple(data))


class MaterialCache:
    def __init__(self):
        self.changed_materials = set()
        self.material_hashes = {}

    def diff(self, depsgraph):
        if depsgraph.id_type_updated("MATERIAL"):
            for dg_update in depsgraph.updates:
                if not isinstance(dg_update.id, bpy.types.Material):
                    continue
                mat = dg_update.id
                if not hasattr(mat, 'luxcore'):
                    self.changed_materials.add(mat)
                    continue
                if mat.luxcore.node_tree or mat.node_tree:
                    new_hash = _get_material_hash(mat)
                    old_hash = self.material_hashes.get(mat.name)
                    if old_hash == new_hash:
                        continue
                    self.material_hashes[mat.name] = new_hash
                self.changed_materials.add(mat)
        return self.changed_materials

    def update(self, exporter, depsgraph, is_viewport_render, props):
        for mat in self.changed_materials:
            lux_mat_name, mat_props = material.convert(exporter, depsgraph, mat, is_viewport_render)
            props.Set(mat_props)
        self.changed_materials.clear()


class VisibilityCache:
    def __init__(self):
        # sets containing keys
        self.last_visible_objects = None
        self.objects_to_remove = None
        self.has_new_objects = False

    def init(self, depsgraph, context):
        self.last_visible_objects = self._get_visible_objects(depsgraph, context)

    def diff(self, depsgraph, context):
        visible_objs = self._get_visible_objects(depsgraph, context)
        self.objects_to_remove = self.last_visible_objects - visible_objs
        self.has_new_objects = bool(visible_objs - self.last_visible_objects)
        self.last_visible_objects = visible_objs
        return bool(self.objects_to_remove) or self.has_new_objects

    def _get_visible_objects(self, depsgraph, context):
        keys = set()

        for dg_obj_instance in depsgraph.object_instances:
            if not supports_live_transform(dg_obj_instance.particle_system):
                continue

            if dg_obj_instance.show_self:
                # For duplis, check visibility of parent (emitter)
                obj = dg_obj_instance.parent if dg_obj_instance.parent else dg_obj_instance.object
                if obj.luxcore.exclude_from_render or not obj.visible_in_viewport_get(context.space_data):
                    continue
                keys.add(utils.make_key_from_instance(dg_obj_instance))
        return keys


class WorldCache:
    def __init__(self):
        self.world_name = None
        self.world_hash = None

    def diff(self, depsgraph):
        world = depsgraph.scene_eval.world
        world_updated = False

        if world:
            if depsgraph.id_type_updated("WORLD"):
                new_hash = _hash_world(world)
                if new_hash != self.world_hash:
                    world_updated = True
                self.world_hash = new_hash

            if self.world_name != world.name_full:
                world_updated = True

            # The sun influences the world, e.g. through direction and turbidity if sky2 is used
            if world.luxcore.light == "sky2" and depsgraph.id_type_updated("OBJECT"):
                for dg_update in depsgraph.updates:
                    if dg_update.id == world.luxcore.sun:
                        world_updated = True
                        break
        elif self.world_name:
            # We had a world, but it was deleted
            world_updated = True

        self.world_name = world.name_full if world else None
        return world_updated