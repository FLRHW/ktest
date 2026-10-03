"""Append paginated top/bottom BOM sections to the KiBot assembly PDF.

Uses rsvg-convert and KiBot's bundled PDF library in the existing pinned image.
No downloads, local Docker, or additional Python packages are required in CI.
"""
import argparse
import csv
import html
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap

try:
    from kibot import PyPDF2 as pdf
except ImportError:
    import pypdf as pdf  # For verification outside the CI image.

HEADERS = ("References", "Qty", "Value", "Footprint", "MPN")
WEIGHTS = (0.25, 0.07, 0.16, 0.27, 0.25)
FONT_MM = 3.0  # About 8.5 pt; kept constant when paper size changes.
LINE_MM = 4.2
MARGIN_MM = 12.0


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
    max_lines = int((height - 64.0 - 3.0) // LINE_MM)
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
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" '
             f'height="{height}mm" viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="white"/>']

    def text(x, y, value, size=FONT_MM, bold=False, mono=False):
        font = "DejaVu Sans Mono" if mono else "DejaVu Sans"
        parts.append(f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" '
                     f'font-weight="{"bold" if bold else "normal"}">{html.escape(value)}</text>')

    def line(x1, y1, x2, y2):
        parts.append(f'<path d="M{x1},{y1} L{x2},{y2}" fill="none" stroke="#555" stroke-width="0.2"/>')

    text(MARGIN_MM, 19, f"{side}-side fitted BOM", 5, True)
    # Wrap an unusually long project basename instead of clipping its header.
    project_lines = wrap_cell(project, width - 2 * MARGIN_MM)
    text(MARGIN_MM, 27, project_lines[0])
    text(MARGIN_MM, 33, f"{side} BOM page {section_page} of {section_total}", 2.7)
    y, x = 40.0, MARGIN_MM
    parts.append(f'<rect x="{x}" y="{y}" width="{sum(widths)}" height="8" fill="#eeeeee"/>')
    for heading, col_width in zip(HEADERS, widths):
        text(x + 2, y + 5.5, heading, bold=True)
        x += col_width
    line(MARGIN_MM, y, width - MARGIN_MM, y)
    y += 8
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
        line(x, 40, x, 48 if not rows and inner else y)
        x += col_width
    line(MARGIN_MM, height - 16, width - MARGIN_MM, height - 16)
    text(MARGIN_MM, height - 10, "Assembly BOM - fitted parts only", 2.7)
    text(width - 60, height - 10, f"PDF page {document_page} of {document_total}", 2.7)
    parts.append("</svg>")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcb", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    args = parser.parse_args()
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
    writer = writer_class()
    add_page = getattr(writer, "add_page", None) or writer.addPage
    bookmark = getattr(writer, "add_outline_item", None) or writer.addBookmark
    for page in original.pages:
        add_page(page)
    bookmark("Assembly drawings", 0)
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
    current = len(original.pages)
    with tempfile.TemporaryDirectory(prefix="assembly-bom-") as temporary:
        temp = Path(temporary)
        for side, pages in sections:
            bookmark(f"{side}-side BOM", current)
            for i, rows in enumerate(pages, 1):
                svg_file, pdf_file = temp / "page.svg", temp / "page.pdf"
                svg_file.write_text(make_svg(width, height, widths, rows, side, base,
                                            i, len(pages), current + 1, total), encoding="utf-8")
                subprocess.run([converter, "-f", "pdf", "-o", str(pdf_file), str(svg_file)], check=True)
                rendered = reader_class(io.BytesIO(pdf_file.read_bytes()))
                add_page(rendered.pages[0])
                current += 1
        result = io.BytesIO()
        writer.write(result)
        # Replace only after every page has been generated and merged.
        assembly.write_bytes(result.getvalue())
    print(f"Assembly PDF: {len(original.pages)} drawing pages + "
          f"{total-len(original.pages)} BOM pages ({width:.1f} x {height:.1f} mm)")


if __name__ == "__main__":
    main()
