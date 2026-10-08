"""Install KiBot's render hook and apply the project's Blender adjustments.

Normal Python execution installs the hook in the current CI container.
Blender executes this same file with a special run name after scene creation.
The repository's workflow can continue calling this filename unchanged.
"""
import ast
import importlib.util
from pathlib import Path
import re


BLENDER_RUN_NAME = "__kicad_blender_tune__"


def patch_script(script, helper):
    marker = "    c_formats = len(args.format)\n"
    tag = "    # Project solder-mask material hook\n"
    source = script.read_text()
    if source.count(marker) != 1:
        raise RuntimeError("Unexpected KiBot Blender script; hook was not installed")
    hook = (
        tag + "    import runpy\n"
        + f"    runpy.run_path({str(helper)!r}, run_name={BLENDER_RUN_NAME!r})\n"
    )
    if hook in source:
        print("Blender render hook already installed")
        return
    if tag in source:
        # Replace the previous two-file hook when it is already installed.
        pattern = re.escape(tag) + r"    import runpy\n    runpy\.run_path\([^\n]*\)\n"
        source, count = re.subn(pattern, lambda match: hook, source)
        if count != 1:
            raise RuntimeError("Unexpected existing Blender hook; refusing to overwrite it")
        if hook + marker not in source:
            raise RuntimeError("Existing hook is not after scene creation")
    else:
        source = source.replace(marker, hook + marker, 1)
    ast.parse(source)
    script.write_text(source)
    print(f"Installed Blender render hook in {script}")


def install_hook():
    spec = importlib.util.find_spec("kibot")
    if spec is None or spec.origin is None:
        raise SystemExit("Cannot locate the installed KiBot package")
    script = Path(spec.origin).parent / "blender_scripts" / "blender_export.py"
    patch_script(script, Path(__file__).resolve())


def tune_scene():
    import bpy

    view = bpy.context.scene.view_settings
    print(f"Original colour transform: {view.view_transform}", flush=True)
    view.view_transform = 'Khronos PBR Neutral' # 'Standard' # 'AgX'
    view.look = 'None'
    view.exposure = 0.2
    view.gamma = 1.0

    # Start with zero to test whether the solder-mask texture causes the clouds.
    # Try 0.05 later if a little surface texture is desired.
    MASK_TEXTURE_STRENGTH = 0.2

    seen = set()
    changed = 0


    def tune_tree(tree):
        nonlocal changed
        if tree is None or tree.as_pointer() in seen:
            return
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if (node.bl_idname == "ShaderNodeBsdfPcbSolderMask"
                    or getattr(node, "bl_label", "") == "Solder Mask BSDF"):
                socket = node.inputs.get("Texture Strength")
                if socket is None:
                    raise RuntimeError("Solder-mask shader has no Texture Strength input")
                if socket.is_linked:
                    raise RuntimeError("Solder-mask texture input is linked; refusing to overwrite it")
                socket.default_value = MASK_TEXTURE_STRENGTH
                changed += 1
            tune_tree(getattr(node, "node_tree", None))


    for material in bpy.data.materials:
        tune_tree(material.node_tree)

    print(f"Solder-mask texture adjustment: {changed} shader node(s), strength={MASK_TEXTURE_STRENGTH}", flush=True)
    if not changed:
        print(
            "WARNING: No enhanced solder-mask shader found; "
            "skipping mask texture adjustment.",
            flush=True,
        )

    from mathutils import Vector

    scene = bpy.context.scene
    bpy.context.view_layer.update()

    # Measure the rendered board and components, excluding cameras and lights.
    points = [
        obj.matrix_world @ Vector(corner)
        for obj in scene.objects
        if obj.type == 'MESH' and not obj.hide_render
        for corner in obj.bound_box
    ]

    if not points:
        raise RuntimeError("Cannot measure board geometry for lighting")

    minimum = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    maximum = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    centre = (minimum + maximum) / 2
    extent = maximum - minimum
    board_size = max(extent.x, extent.y, extent.z)

    if board_size <= 0:
        raise RuntimeError("Invalid board dimensions for lighting")

    # Positions relative to the board centre, in multiples of board size.
    positions = (
        (-4.0, -3.0, 5.0),
        ( 4.0, -1.0, 5.0),
        (-1.0,  4.0, 4.0),
        ( 3.0,  4.0, 6.0),
    )

    area_lights = sorted(
        (
            obj for obj in scene.objects
            if obj.type == 'LIGHT' and obj.data.type == 'AREA'
        ),
        key=lambda obj: obj.name,
    )

    if not area_lights:
        raise RuntimeError("No AREA lights found")

    for index, obj in enumerate(area_lights):
        offset = Vector(positions[index % len(positions)])
        obj.location = centre + offset * board_size

        # AREA lights emit along their local negative Z axis.
        direction = centre - obj.location
        obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

        # Rectangular softboxes, automatically sized for each board.
        obj.data.shape = 'RECTANGLE'
        obj.data.size = board_size * 4.0
        obj.data.size_y = board_size * 6.0
        obj.visible_glossy = True

        print(
            f"Softbox {obj.name}: "
            f"size={obj.data.size:.3f} x {obj.data.size_y:.3f}, "
            f"energy={obj.data.energy:.3f}, aimed at board centre",
            flush=True,
        )

    for obj in scene.objects:
        if obj.type == 'LIGHT' and obj.data.type == 'SUN':
            obj.visible_glossy = False

    bpy.context.view_layer.update()

    # Darken dark, nearly neutral component materials.
    DARK_COLOUR_FACTOR = 0.15

    adjusted = 0

    for material in bpy.data.materials:
        if material.node_tree is None:
            continue

        for node in material.node_tree.nodes:
            if node.name != "Mat4cad BSDF":
                continue

            colour = node.inputs.get("Color")
            if colour is None or colour.is_linked:
                continue

            original = tuple(colour.default_value)
            rgb = original[:3]

            # Select dark greys; exclude bright and strongly coloured materials.
            if max(rgb) > 0.20 or max(rgb) - min(rgb) > 0.03:
                continue

            colour.default_value = (
                *(channel * DARK_COLOUR_FACTOR for channel in rgb),
                original[3],
            )
            adjusted += 1

            print(
                f"DARK MATERIAL {material.name!r}: "
                f"{original} -> {tuple(colour.default_value)}",
                flush=True,
            )

    print(f"Darkened {adjusted} component material(s)", flush=True)

    # Correct housings in supported JST XH models.
    # These models have two materials; the housing dominates surface area.
    corrected_meshes = set()

    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH':
            continue

        mesh = obj.data
        if not mesh.name.startswith('JST_XH_'):
            continue
        if mesh.as_pointer() in corrected_meshes:
            continue
        corrected_meshes.add(mesh.as_pointer())

        if len(mesh.materials) != 2:
            print(
                f"MATERIAL_DIAG JST correction skipped: {mesh.name}; "
                "expected two materials",
                flush=True,
            )
            continue

        areas = [0.0, 0.0]
        for polygon in mesh.polygons:
            areas[polygon.material_index] += polygon.area

        housing_index = max(range(2), key=lambda i: areas[i])
        contact_index = 1 - housing_index

        # Leave unfamiliar geometry unchanged.
        if areas[housing_index] < 2.0 * areas[contact_index]:
            print(
                f"MATERIAL_DIAG JST correction skipped: {mesh.name}; "
                "housing identification ambiguous",
                flush=True,
            )
            continue

        original = mesh.materials[housing_index]
        if original is None:
            continue

        # Retain the imported housing colour.
        colour = tuple(original.diffuse_color)
        if original.node_tree:
            for node in original.node_tree.nodes:
                if node.name == 'Mat4cad BSDF':
                    socket = node.inputs.get('Color')
                    if socket is not None and not socket.is_linked:
                        colour = tuple(socket.default_value)
                        break

        plastic = bpy.data.materials.new(
            name=f'{mesh.name}_housing_plastic'
        )
        plastic.use_nodes = True
        plastic.node_tree.nodes.clear()

        shader = plastic.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
        shader.inputs['Base Color'].default_value = (
    *(channel * 0.70 for channel in colour[:3]),
    colour[3],
)
        shader.inputs['Metallic'].default_value = 0.0
        shader.inputs['Roughness'].default_value = 0.4
        shader.inputs['IOR'].default_value = 1.46
        shader.inputs['Alpha'].default_value = 1.0

        output = plastic.node_tree.nodes.new('ShaderNodeOutputMaterial')
        plastic.node_tree.links.new(
            shader.outputs['BSDF'], output.inputs['Surface']
        )

        # Replace only this mesh's housing material, preserving contacts.
        mesh.materials[housing_index] = plastic

        print(
            f"MATERIAL_DIAG JST housing corrected: {mesh.name}; "
            "contacts retained",
            flush=True,
        )

    # Reduce brightness of beige axial-resistor bodies.
    # Jumpers using these models receive the same correction.
    RESISTOR_BODY_FACTOR = 0.50
    adjusted_bodies = set()

    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH':
            continue
        if not obj.data.name.startswith('R_Axial_'):
            continue

        for material in obj.data.materials:
            if material is None or material.node_tree is None:
                continue
            if material.as_pointer() in adjusted_bodies:
                continue

            for node in material.node_tree.nodes:
                if node.name != 'Mat4cad BSDF':
                    continue

                colour = node.inputs.get('Color')
                if colour is None or colour.is_linked:
                    continue

                r, g, b, alpha = colour.default_value

                # Select the warm beige body, excluding silver leads
                # and dark colour bands.
                if r > 0.5 and r > b * 1.25 and g > b * 1.10:
                    colour.default_value = (
                        r * RESISTOR_BODY_FACTOR,
                        g * RESISTOR_BODY_FACTOR,
                        b * RESISTOR_BODY_FACTOR,
                        alpha,
                    )
                    adjusted_bodies.add(material.as_pointer())
                    print(
                        f"Resistor body darkened: {material.name}",
                        flush=True,
                    )

    # Reusable metal adjustment; no project-specific material names.
    METAL_ROUGHNESS_MIN = 0.25
    metal_trees_seen = set()
    metals_adjusted = 0

    def soften_metals(tree):
        nonlocal metals_adjusted

        if tree is None or tree.as_pointer() in metal_trees_seen:
            return
        metal_trees_seen.add(tree.as_pointer())

        for node in list(tree.nodes):
            if node.type == 'BSDF_PRINCIPLED':
                metallic = node.inputs.get('Metallic')
                roughness = node.inputs.get('Roughness')

                if (metallic is not None
                        and not metallic.is_linked
                        and metallic.default_value >= 0.5
                        and roughness is not None):

                    if roughness.is_linked:
                        # Preserve the texture, but enforce a minimum.
                        original = roughness.links[0].from_socket
                        floor = tree.nodes.new('ShaderNodeMath')
                        floor.operation = 'MAXIMUM'
                        floor.inputs[1].default_value = METAL_ROUGHNESS_MIN
                        tree.links.new(original, floor.inputs[0])
                        tree.links.new(floor.outputs[0], roughness)
                    else:
                        roughness.default_value = max(
                            roughness.default_value,
                            METAL_ROUGHNESS_MIN,
                        )

                    metals_adjusted += 1

            soften_metals(getattr(node, 'node_tree', None))

    for material in bpy.data.materials:
        soften_metals(material.node_tree)

    print(
        f"Metal roughness floor: {METAL_ROUGHNESS_MIN}; "
        f"adjusted {metals_adjusted} shader(s)",
        flush=True,
    )

    # # Temporary diagnostic: identify component materials and their settings.
    # for material in bpy.data.materials:
    #     if material.node_tree is None:
    #         continue
    #
    #     for node in material.node_tree.nodes:
    #         if node.name != "Mat4cad BSDF":
    #             continue
    #
    #         settings = {}
    #         for socket in node.inputs:
    #             if not hasattr(socket, "default_value"):
    #                 continue
    #             value = socket.default_value
    #             if not isinstance(value, (str, int, float, bool)):
    #                 try:
    #                     value = tuple(value)
    #                 except TypeError:
    #                     continue
    #             settings[socket.name] = {
    #                 "value": value,
    #                 "linked": socket.is_linked,
    #             }
    #
    #         print(
    #             f"MATERIAL_DIAG {material.name!r}: {settings}",
    #             flush=True,
    #         )

if __name__ == BLENDER_RUN_NAME:
    tune_scene()
elif __name__ == "__main__":
    install_hook()
