"""Install a temporary render hook in the CI container's KiBot script."""
import ast
import importlib.util
from pathlib import Path

spec = importlib.util.find_spec("kibot")
if spec is None or spec.origin is None:
    raise SystemExit("Cannot locate the installed KiBot package")
script = Path(spec.origin).parent / "blender_scripts" / "blender_export.py"
helper = Path(__file__).with_name("tune_blender_materials.py").resolve()
if not helper.is_file():
    raise SystemExit(f"Missing material helper: {helper}")

marker = "    if args.no_denoiser:\n"
tag = "    # Project solder-mask material hook\n"
source = script.read_text()
if tag in source:
    print("Blender material hook already installed")
elif source.count(marker) != 1:
    raise SystemExit("Unexpected KiBot Blender script; hook was not installed")
else:
    hook = (tag + "    import runpy\n"
            + f"    runpy.run_path({str(helper)!r})\n")
    source = source.replace(marker, hook + marker, 1)
    ast.parse(source)
    script.write_text(source)
    print(f"Installed Blender material hook in {script}")
