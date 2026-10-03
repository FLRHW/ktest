"""Add paginated BOM tables to assembly pages rendered with the PCB worksheet.

Uses rsvg-convert and KiBot's bundled PDF library in the existing pinned image.
No downloads, local Docker, or additional Python packages are required in CI.
"""
import argparse
import csv
import html
import io
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

HEADERS = ("References", "Qty", "Value", "Footprint", "MPN")
WEIGHTS = (0.25, 0.07, 0.16, 0.27, 0.25)
FONT_MM = 3.0  # About 8.5 pt; kept constant when paper size changes.
LINE_MM = 4.2
MARGIN_MM = 12.0
# Reserve the lower strip for the custom worksheet title block. Adjust this
# if a future worksheet has a taller title block; values are millimetres.
TITLE_BLOCK_CLEARANCE_MM = 55.0
TABLE_TOP_MM = 20.0
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
        text(MARGIN_MM + 2, y + 7, f"No fitted BOM components on the {side.lower()} side.")
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


def load_config(path, cli_defines):
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
    return config


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
            label = f"{side}-side fitted BOM ({number} of {len(pages)})"
            options["pages"].append({"sheet": label, "layer_var": label,
                                     "sheet_reference_color": "#000000", "layers": []})
    return config


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
    assembly = args.output_dir / "documentation" / f"{base}-pcb-assembly.pdf"
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
    sections = []
    for side in ("Top", "Bottom"):
        source = args.output_dir / "assembly" / "tables" / f"{base}-assembly-bom-{side.lower()}.csv"
        sections.append((side, paginate(read_rows(source), widths, height)))
    total = len(original.pages) + sum(len(pages) for _, pages in sections)
    drawing_count = len(original.pages)
    with tempfile.TemporaryDirectory(prefix="assembly-bom-", dir=args.config.parent) as temporary:
        temp = Path(temporary)
        rendered_file = temp / "assembly-with-worksheet.pdf"
        config = add_bom_pages(load_config(args.config, defines), sections, rendered_file)
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
        for side, pages in sections:
            bookmark(f"{side}-side BOM", drawing_count)
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
