"""Add paginated BOM tables to assembly pages rendered with the PCB worksheet.

Uses rsvg-convert and KiBot's bundled PDF library in the existing pinned image.
No downloads, local Docker, or additional Python packages are required in CI.
"""
import argparse
import csv
import html
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import yaml

try:
    from kibot import PyPDF2 as pdf
except ImportError:
    import pypdf as pdf  # For verification outside the CI image.

HEADERS = ("References", "Qty", "Value", "Footprint", "MPN", "Status")
WEIGHTS = (0.22, 0.07, 0.14, 0.24, 0.23, 0.10)
FONT_MM = 3.0  # About 8.5 pt; kept constant when paper size changes.
LINE_MM = 4.2
MARGIN_MM = 18.0
# Reserve the lower strip for the custom worksheet title block. Adjust this
# if a future worksheet has a taller title block; values are millimetres.
TITLE_BLOCK_CLEARANCE_MM = 35.0
TABLE_TOP_MM = 28.0
TABLE_HEADER_MM = 8.0


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not set(HEADERS) <= set(reader.fieldnames):
            raise ValueError(f"{path}: expected CSV columns {HEADERS}")
        rows = []
        for row in reader:
            if None in row or any(row[h] is None for h in HEADERS):
                raise ValueError(f"{path}: malformed CSV row: {row}")
            if any(row[h].strip() for h in HEADERS):
                rows.append([row[h] for h in HEADERS])
        return rows



BOM_MODES = ("combined", "fitted", "separate", "no bom")


def validate_bom_mode(mode):
    if mode not in BOM_MODES:
        raise ValueError(f"ASSEMBLY_BOM_MODE must be one of {BOM_MODES}; got {mode!r}")


def validate_source_boms(config):
    # Filtering cannot recover rows already removed by the KiBot CSV outputs.
    for name in ("assembly_bom_top", "assembly_bom_bottom"):
        matches = [o for o in config.get("outputs", []) if o.get("name") == name]
        if len(matches) != 1:
            raise ValueError(f"Expected one source BOM output named {name}")
        options = matches[0].get("options", {})
        if options.get("ignore_dnf", True) is not False:
            raise ValueError(f"Set {name} options ignore_dnf to false")
        if options.get("group_not_fitted", False) is not False:
            raise ValueError(f"Set {name} options group_not_fitted to false")


def row_is_dnp(row):
    # KiBot emits e.g. '(DNF)' or '(DNF) (DNC)'. DNC alone is still fitted.
    return bool(re.search(r"\b(?:DNF|DNP)\b", row[HEADERS.index("Status")], re.I))


def split_rows(rows):
    fitted, dnp = [], []
    for row in rows:
        (dnp if row_is_dnp(row) else fitted).append(row)
    return fitted, dnp


def build_sections(rows_by_side, mode, widths, height):
    validate_bom_mode(mode)
    if mode == "no bom":
        return []
    sections = []
    split = {side: split_rows(rows_by_side[side]) for side in ("Top", "Bottom")}
    kinds = ("combined",) if mode == "combined" else ("fitted",)
    if mode == "separate":
        kinds = ("fitted", "dnp")
    for kind in kinds:
        for side in ("Top", "Bottom"):
            if kind == "combined":
                label, rows = f"{side} BOM - all", rows_by_side[side]
            elif kind == "fitted":
                label, rows = f"{side} BOM - fitted", split[side][0]
            else:
                label, rows = f"{side} BOM - DNP", split[side][1]
            sections.append((label, paginate(rows, widths, height)))
    return sections


def write_filtered_boms(output_dir, base, rows_by_side):
    # Export all four additional CSVs regardless of the selected PDF mode.
    directory = output_dir / "assembly" / "tables"
    directory.mkdir(parents=True, exist_ok=True)
    for side in ("Top", "Bottom"):
        for kind, rows in zip(("fitted", "dnp"), split_rows(rows_by_side[side])):
            path = directory / f"{base}-assembly-bom-{side.lower()}-{kind}.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(HEADERS)
                writer.writerows(rows)


def wrap_cell(value, width):
    # Monospaced table text makes line fitting predictable for long part numbers.
    chars = max(1, int((width - 4.0) / (FONT_MM * 0.62)))
    lines = []
    for paragraph in value.replace("\t", " ").splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, width=chars,
                                   break_long_words=True, break_on_hyphens=False) or [""])
    return lines


def paginate(rows, widths, height):
    available = height - TITLE_BLOCK_CLEARANCE_MM - TABLE_TOP_MM - TABLE_HEADER_MM - 3.0
    max_lines = int(available // LINE_MM)
    if max_lines < 2:
        raise ValueError("Paper is too small for the BOM layout")
    pages, page, remaining = [], [], max_lines
    for row in rows:
        columns = [wrap_cell(v, w) for v, w in zip(row, widths)]
        count = max(map(len, columns))
        # Move ordinary rows together. Split an exceptionally tall row only
        # when it cannot fit on a whole page, preserving every line of data.
        if count <= max_lines and count + 1 > remaining and page:
            pages.append(page)
            page, remaining = [], max_lines
        offset = 0
        while offset < count:
            if remaining < 2:
                pages.append(page)
                page, remaining = [], max_lines
            take = min(count - offset, remaining - 1)
            fragment = [(col + [""] * count)[offset:offset + take] for col in columns]
            page.append((fragment, take, offset > 0))
            remaining -= take + 1
            offset += take
            if offset < count:
                pages.append(page)
                page, remaining = [], max_lines
    if page or not pages:
        pages.append(page)
    return pages


def make_svg(width, height, widths, rows, side, project, section_page, section_total,
             document_page, document_total):
    # Transparent outside the table: the worksheet underneath stays visible.
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" '
             f'height="{height}mm" viewBox="0 0 {width} {height}">']

    def text(x, y, value, size=FONT_MM, bold=False, mono=False):
        font = "DejaVu Sans Mono" if mono else "DejaVu Sans"
        parts.append(f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" '
                     f'font-weight="{"bold" if bold else "normal"}">{html.escape(value)}</text>')

    def line(x1, y1, x2, y2):
        parts.append(f'<path d="M{x1},{y1} L{x2},{y2}" fill="none" stroke="#555" stroke-width="0.2"/>')

    y, x = TABLE_TOP_MM, MARGIN_MM
    parts.append(f'<rect x="{x}" y="{y}" width="{sum(widths)}" height="{TABLE_HEADER_MM}" fill="#eeeeee"/>')
    for heading, col_width in zip(HEADERS, widths):
        text(x + 2, y + 5.5, heading, bold=True)
        x += col_width
    line(MARGIN_MM, y, width - MARGIN_MM, y)
    y += TABLE_HEADER_MM
    if not rows:
        text(MARGIN_MM + 2, y + 7, f"No components in this section: {side}.")
        y += 12
    for columns, count, continued in rows:
        line(MARGIN_MM, y, width - MARGIN_MM, y)
        if continued:
            # A coloured rule identifies fragments of an unusually long row.
            parts.append(f'<path d="M{MARGIN_MM},{y} L{width-MARGIN_MM},{y}" '
                         'stroke="#888" stroke-width="0.5"/>')
        x = MARGIN_MM
        for column, col_width in zip(columns, widths):
            for i, value in enumerate(column):
                text(x + 2, y + 4 + i * LINE_MM, value, mono=True)
            x += col_width
        y += (count + 1) * LINE_MM
    line(MARGIN_MM, y, width - MARGIN_MM, y)
    x = MARGIN_MM
    for col_width in (*widths, 0):
        inner = MARGIN_MM < x < width - MARGIN_MM - 0.01
        line(x, TABLE_TOP_MM, x, TABLE_TOP_MM + TABLE_HEADER_MM if not rows and inner else y)
        x += col_width
    parts.append("</svg>")
    return "\n".join(parts)


def load_config(path, cli_defines, with_definitions=False):
    """Resolve the existing standalone project's KiBot definitions document."""
    docs = re.split(r"^\.\.\.\s*$", path.read_text(), flags=re.M)
    body = next((d for d in docs if re.search(r"^kibot:\s*$", d, re.M)), None)
    if body is None:
        raise ValueError("Configuration has no kibot section")
    definitions = {}
    for doc in docs:
        if re.search(r"^definitions:\s*$", doc, re.M):
            definitions.update(yaml.safe_load(doc)["definitions"])
    definitions.update(cli_defines)
    for _ in range(20):
        before = body
        for key, value in definitions.items():
            if isinstance(value, (dict, list)):
                raise ValueError(f"Definition {key} must be a scalar")
            if value is None:
                value = "null"
            elif isinstance(value, bool):
                value = str(value).lower()
            body = body.replace("@" + key + "@", str(value))
        if before == body:
            break
    config = yaml.safe_load(body)
    if config.get("import"):
        raise ValueError("This worksheet helper currently requires a standalone config without imports")
    return (config, definitions) if with_definitions else config


def add_bom_pages(config, sections, output_pdf):
    matches = [o for o in config["outputs"] if o.get("name") == "pcb_assembly_pdf"]
    if len(matches) != 1 or matches[0].get("type") != "pcb_print":
        raise ValueError("Expected one pcb_print output named pcb_assembly_pdf")
    options = matches[0].setdefault("options", {})
    if options.get("force_edge_cuts", False):
        raise ValueError("Set pcb_assembly_pdf options force_edge_cuts to false for blank BOM pages")
    options.update(output=str(output_pdf), format="PDF", plot_sheet_reference=True,
                   frame_plot_mechanism="internal")
    # Retain sheet_reference_layout: an empty value uses the project setting.
    for side, pages in sections:
        for number in range(1, len(pages) + 1):
            label = f"{side} ({number} of {len(pages)})"
            options["pages"].append({"sheet": label, "layer_var": label,
                                     "sheet_reference_color": "#000000", "layers": []})
    return config


def add_ibom_link(writer, config, pcb_path, assembly_path, output_dir):
    """Restyle the text in assembly_ibom_link and make its area clickable.

    This mapping is deliberately restricted to the current 1:1, unmirrored
    pcb_print overview, with auxiliary origin disabled by KiBot's plotter.
    """
    output = next(o for o in config["outputs"] if o.get("name") == "pcb_assembly_pdf")
    options = output["options"]
    first = options["pages"][0]
    layers = first.get("layers", [])
    names = [layer.get("layer") if isinstance(layer, dict) else layer for layer in layers]
    if "AssemblyOverview" not in names:
        print("iBOM link omitted: AssemblyOverview is not on the first page")
        return
    if first.get("mirror", False) or float(first.get("scaling", options.get("scaling", 0))) != 1.0:
        raise ValueError("iBOM group link requires an unmirrored first page at scaling 1.0")

    import pcbnew
    board = pcbnew.LoadBoard(str(pcb_path))
    groups = [group for group in board.Groups() if group.GetName() == "assembly_ibom_link"]
    if len(groups) != 1:
        print("WARNING: iBOM link omitted: expected one group named assembly_ibom_link")
        return
    texts = [item for item in groups[0].GetItems()
             if isinstance(item, pcbnew.PCB_TEXT)
             and board.GetLayerName(item.GetLayer()) == "AssemblyOverview"]
    if len(texts) != 1:
        raise ValueError("assembly_ibom_link must contain exactly one PCB text item on AssemblyOverview")
    item = texts[0]
    if abs(item.GetTextAngle().AsDegrees() % 360) > 0.01 or item.IsMirrored():
        raise ValueError("Use horizontal, unmirrored PCB text for assembly_ibom_link")
    label = item.GetShownText(False).strip()
    if not label or "\n" in label:
        raise ValueError("Use a nonempty, single-line label for assembly_ibom_link")
    bounds = item.GetBoundingBox()
    left, top = pcbnew.ToMM(bounds.GetX()), pcbnew.ToMM(bounds.GetY())
    right = left + pcbnew.ToMM(bounds.GetWidth())
    bottom = top + pcbnew.ToMM(bounds.GetHeight())
    if right <= left or bottom <= top:
        raise ValueError("iBOM link text has no area")

    ibom = output_dir / "assembly" / "bom" / f"{pcb_path.stem}-ibom.html"
    if not ibom.is_file():
        raise FileNotFoundError(f"Cannot add iBOM link: {ibom} does not exist")
    target = Path(os.path.relpath(ibom, assembly_path.parent)).as_posix()
    get_page = getattr(writer, "get_page", None) or writer.getPage
    page = get_page(0)
    box = page.mediabox if hasattr(page, "mediabox") else page.mediaBox
    height = float(box.height if hasattr(box, "height") else box.getHeight())
    width = float(box.width if hasattr(box, "width") else box.getWidth())
    factor = 72.0 / 25.4
    padding = 0.5
    rect = ((left-padding) * factor, height - (bottom+padding) * factor,
            (right+padding) * factor, height - (top-padding) * factor)
    if rect[0] < 0 or rect[1] < 0 or rect[2] > width or rect[3] > height:
        raise ValueError("iBOM link text lies outside the PDF page")
    style_link_label(page, width / factor, height / factor, label, left, top, right, bottom)
    attach_launch_link(writer, page, target, rect)
    print(f"First-page blue underlined iBOM link: {target}; text bounds "
          f"({left:.1f}, {top:.1f}) to ({right:.1f}, {bottom:.1f}) mm")


def style_link_label(page, width, height, label, left, top, right, bottom):
    """Replace the plotted black label with a blue, underlined vector label."""
    padding = 0.5
    text_height = bottom - top
    baseline = bottom - text_height * 0.17
    font_size = text_height * 0.75
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm"
        height="{height}mm" viewBox="0 0 {width} {height}">
      <rect x="{left-padding}" y="{top-padding}" width="{right-left+2*padding}"
        height="{text_height+2*padding}" fill="white"/>
      <text x="{left}" y="{baseline}" font-family="DejaVu Sans"
        font-size="{font_size}" fill="#0645AD"
        text-decoration="underline">{html.escape(label)}</text>
    </svg>'''
    reader_class = getattr(pdf, "PdfReader", None) or pdf.PdfFileReader
    with tempfile.TemporaryDirectory(prefix="ibom-link-") as temporary:
        svg_file, pdf_file = Path(temporary) / "link.svg", Path(temporary) / "link.pdf"
        svg_file.write_text(svg, encoding="utf-8")
        subprocess.run(["rsvg-convert", "-f", "pdf", "-o", str(pdf_file), str(svg_file)], check=True)
        overlay = reader_class(io.BytesIO(pdf_file.read_bytes())).pages[0]
        merge = getattr(page, "merge_page", None) or page.mergePage
        merge(overlay)


def attach_launch_link(writer, page, target, rect):
    # File launch action, rather than a website URL. The PDF viewer decides
    # whether to allow opening the local HTML in its associated application.
    g = pdf.generic
    string = getattr(g, "create_string_object", None) or g.createStringObject
    action = g.DictionaryObject({
        g.NameObject("/S"): g.NameObject("/Launch"),
        g.NameObject("/F"): g.DictionaryObject({
            g.NameObject("/Type"): g.NameObject("/Filespec"),
            g.NameObject("/F"): string(target),
            g.NameObject("/UF"): string(target),
        }),
        g.NameObject("/NewWindow"): g.BooleanObject(True),
    })
    annotation = g.DictionaryObject({
        g.NameObject("/Type"): g.NameObject("/Annot"),
        g.NameObject("/Subtype"): g.NameObject("/Link"),
        g.NameObject("/Rect"): g.ArrayObject([g.FloatObject(v) for v in rect]),
        g.NameObject("/Border"): g.ArrayObject([g.NumberObject(0)] * 3),
        g.NameObject("/A"): action,
    })
    add_object = getattr(writer, "_add_object", None) or writer._addObject
    annotations = page.get("/Annots")
    if annotations is None:
        annotations = g.ArrayObject()
        page[g.NameObject("/Annots")] = annotations
    elif hasattr(annotations, "get_object"):
        annotations = annotations.get_object()
    elif hasattr(annotations, "getObject"):
        annotations = annotations.getObject()
    annotations.append(add_object(annotation))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcb", type=Path, required=True)
    parser.add_argument("--sch", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config.kibot.yaml"))
    parser.add_argument("--define", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    args = parser.parse_args()
    args.config = args.config.resolve()
    args.pcb = args.pcb.resolve()
    args.sch = args.sch.resolve()
    args.output_dir = args.output_dir.resolve()
    defines = {}
    for definition in args.define:
        if "=" not in definition:
            parser.error("--define requires NAME=VALUE")
        key, value = definition.split("=", 1)
        defines[key] = value
    base = args.pcb.stem
    assembly = args.output_dir / "assembly" / f"{base}-pcb-assembly.pdf"
    converter = shutil.which("rsvg-convert")
    if converter is None:
        raise SystemExit("rsvg-convert is missing; use the tested KiBot CI image")
    reader_class = getattr(pdf, "PdfReader", None) or pdf.PdfFileReader
    writer_class = getattr(pdf, "PdfWriter", None) or pdf.PdfFileWriter
    original = reader_class(io.BytesIO(assembly.read_bytes()))
    first = original.pages[0]
    box = first.mediabox if hasattr(first, "mediabox") else first.mediaBox
    box_width = box.width if hasattr(box, "width") else box.getWidth()
    box_height = box.height if hasattr(box, "height") else box.getHeight()
    width, height = float(box_width) * 25.4 / 72, float(box_height) * 25.4 / 72
    if int(first.get("/Rotate", 0)) % 180:
        width, height = height, width
    widths = [(width - 2 * MARGIN_MM) * w for w in WEIGHTS]
    config, definitions = load_config(args.config, defines, with_definitions=True)
    mode = str(definitions.get("ASSEMBLY_BOM_MODE", "combined")).strip().lower()
    validate_bom_mode(mode)
    rows_by_side = {}
    if mode != "no bom":
        validate_source_boms(config)
        for side in ("Top", "Bottom"):
            source = args.output_dir / "assembly" / "tables" / f"{base}-assembly-bom-{side.lower()}.csv"
            rows_by_side[side] = read_rows(source)
    sections = build_sections(rows_by_side, mode, widths, height)
    if mode != "no bom":
        write_filtered_boms(args.output_dir, base, rows_by_side)
    print(f"Assembly BOM mode: {mode}; {len(sections)} appendix sections")
    total = len(original.pages) + sum(len(pages) for _, pages in sections)
    drawing_count = len(original.pages)
    with tempfile.TemporaryDirectory(prefix="assembly-bom-", dir=args.config.parent) as temporary:
        temp = Path(temporary)
        rendered_file = temp / "assembly-with-worksheet.pdf"
        config = add_bom_pages(config, sections, rendered_file)
        # Keep the temporary config in the original config's directory so
        # any relative paths keep the same base directory.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".kibot.yaml", prefix=".assembly-bom-",
                                         dir=args.config.parent, delete=False) as stream:
            config_path = Path(stream.name)
            yaml.safe_dump(config, stream, sort_keys=False, allow_unicode=True)
        try:
            subprocess.run(["kibot", "--skip-pre", "all", "--no-priority", "-c", str(config_path),
                            "-e", str(args.sch), "-b", str(args.pcb), "-d", str(args.output_dir),
                            "pcb_assembly_pdf"], check=True, cwd=args.config.parent)
        finally:
            config_path.unlink(missing_ok=True)
        original = reader_class(io.BytesIO(rendered_file.read_bytes()))
        if len(original.pages) != total:
            raise ValueError(f"Expected {total} worksheet pages, got {len(original.pages)}")
        current = drawing_count
        for side, pages in sections:
            for i, rows in enumerate(pages, 1):
                svg_file, pdf_file = temp / "table.svg", temp / "table.pdf"
                svg_file.write_text(make_svg(width, height, widths, rows, side, base,
                                            i, len(pages), current + 1, total), encoding="utf-8")
                subprocess.run([converter, "-f", "pdf", "-o", str(pdf_file), str(svg_file)], check=True)
                overlay = reader_class(io.BytesIO(pdf_file.read_bytes())).pages[0]
                page = original.pages[current]
                merge = getattr(page, "merge_page", None) or page.mergePage
                merge(overlay)
                current += 1
        writer = writer_class()
        add_page = getattr(writer, "add_page", None) or writer.addPage
        bookmark = getattr(writer, "add_outline_item", None) or writer.addBookmark
        for page in original.pages:
            add_page(page)
        add_ibom_link(writer, config, args.pcb, assembly, args.output_dir)
        for side, pages in sections:
            bookmark(side, drawing_count)
            drawing_count += len(pages)
        copy_bookmarks(original, bookmark)
        result = io.BytesIO()
        writer.write(result)
        assembly.write_bytes(result.getvalue())
    print(f"Assembly PDF with project worksheet: {total} pages, including "
          f"{sum(len(pages) for _, pages in sections)} BOM pages")


def copy_bookmarks(original, bookmark):
    # Retain the original drawing-page bookmarks when provided by KiBot.
    outlines = original.outline if hasattr(original, "outline") else original.getOutlines()
    destination_page = (getattr(original, "get_destination_page_number", None)
                        or original.getDestinationPageNumber)

    def copy_outlines(items, parent=None):
        previous = parent
        for item in items:
            if isinstance(item, list):
                copy_outlines(item, previous)
            else:
                number = destination_page(item)
                if number >= 0:
                    previous = bookmark(str(item.get("/Title", "Drawing")), number, parent=parent)

    copy_outlines(outlines)


if __name__ == "__main__":
    main()
