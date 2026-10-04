"""Run inside Blender after the PCB import and before rendering."""
import bpy

view = bpy.context.scene.view_settings
print(f"Original colour transform: {view.view_transform}", flush=True)
view.view_transform = 'Standard' # 'AgX'
view.look = 'None'
view.exposure = 0.3
view.gamma = 1.0

# Start with zero to test whether the solder-mask texture causes the clouds.
# Try 0.05 later if a little surface texture is desired.
MASK_TEXTURE_STRENGTH = 0.2

seen = set()
changed = 0


def tune_tree(tree):
    global changed
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
