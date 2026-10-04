"""Run inside Blender after the PCB import and before rendering."""
import bpy

view = bpy.context.scene.view_settings
print(f"Original colour transform: {view.view_transform}", flush=True)
view.view_transform = 'Standard'
view.look = 'None'
view.exposure = 0.0
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
    raise RuntimeError("No supported solder-mask shader found; material adjustment was not applied")

for obj in bpy.context.scene.objects:
    if obj.type != 'LIGHT':
        continue

    if obj.data.type == 'AREA':
        obj.visible_glossy = True
    elif obj.data.type == 'SUN':
        obj.visible_glossy = False

    print(
        f"Light {obj.name}: glossy visibility={obj.visible_glossy}",
        flush=True,
    )
