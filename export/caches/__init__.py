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


IGNORE_PROPS = {
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
}


def _hash_reachable_nodes(mat):
    # LuxCore uses its own node tree, not mat.node_tree
    tree = mat.luxcore.node_tree if hasattr(mat, 'luxcore') and mat.luxcore.node_tree else mat.node_tree
    if not tree:
        return None

    # LuxCore output node is LuxCoreNodeMatOutput, Cycles uses OUTPUT_MATERIAL
    output = next(
        (n for n in tree.nodes if n.bl_idname in ('LuxCoreNodeMatOutput', 'OUTPUT_MATERIAL')),
        None
    )
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
                try:
                    val = tuple(inp.default_value)
                except TypeError:
                    val = inp.default_value
                data.append((inp.name, str(val)))
            for link in inp.links:
                traverse(link.from_node)
        for prop in node.bl_rna.properties:
            if prop.identifier in IGNORE_PROPS:
                continue
            # Only hash simple value types, skip structs/pointers/collections
            # that may come from third party addons and have unstable representations
            if prop.type not in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
                continue
            try:
                data.append((prop.identifier, str(getattr(node, prop.identifier))))
            except Exception:
                pass

    traverse(output)
    return data


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
                lux_tree = mat.luxcore.node_tree if hasattr(mat, 'luxcore') and mat.luxcore.node_tree else None
                if lux_tree or mat.node_tree:
                    new_data = _hash_reachable_nodes(mat)
                    new_hash = hash(tuple(new_data)) if new_data else None
                    old_hash = self.material_hashes.get(mat.name + '_hash')
                    self.material_hashes[mat.name + '_hash'] = new_hash
                    self.material_hashes[mat.name + '_data'] = new_data
                    if old_hash == new_hash:
                        continue
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


def _hash_world(world):
    data = []
    lux = world.luxcore
    for prop in lux.bl_rna.properties:
        if prop.type not in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
            continue
        try:
            val = getattr(lux, prop.identifier)
            try:
                val = tuple(val)
            except TypeError:
                pass
            data.append((prop.identifier, str(val)))
        except Exception:
            pass
    return hash(tuple(data))


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

            if world.luxcore.light == "sky2" and depsgraph.id_type_updated("OBJECT"):
                for dg_update in depsgraph.updates:
                    if dg_update.id == world.luxcore.sun:
                        world_updated = True
                        break
        elif self.world_name:
            world_updated = True

        self.world_name = world.name_full if world else None
        return world_updated
