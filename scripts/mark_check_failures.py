#!/usr/bin/env python3
"""Run in the KiBot container, after PDF generation and BOM appending."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from xml.sax.saxutils import escape

try:
    from kibot import PyPDF2 as pdf
except ImportError:
    import PyPDF2 as pdf


def check_report(path, kind):
    data = json.loads(path.read_text(encoding='utf-8'))
    expected = f'https://schemas.kicad.org/{kind}.v1.json'
    if data.get('$schema') != expected:
        raise RuntimeError(f'Unsupported {kind.upper()} report schema: {path}')
    if kind == 'erc':
        violations = [v for sheet in data['sheets'] for v in sheet['violations']]
    else:
        violations = [v for section in ('violations', 'unconnected_items', 'schematic_parity')
                      for v in data.get(section, [])]
    active = [v for v in violations if not v.get('excluded', False)]
    return sum(v.get('severity', 'error') == 'error' for v in active)


def preserve_bookmarks(reader, writer):
    outlines = reader.outline if hasattr(reader, 'outline') else reader.getOutlines()
    get_page = (getattr(reader, 'get_destination_page_number', None)
                or reader.getDestinationPageNumber)
    add = getattr(writer, 'add_outline_item', None) or writer.addBookmark

    def walk(items, parent=None):
        previous = parent
        for item in items:
            if isinstance(item, list):
                walk(item, previous)
            else:
                number = get_page(item)
                if number >= 0:
                    previous = add(str(item.get('/Title', 'Drawing')), number, parent=parent)
    walk(outlines)


def stamp(source, messages, directory):
    reader_class = getattr(pdf, 'PdfReader', None) or pdf.PdfFileReader
    writer_class = getattr(pdf, 'PdfWriter', None) or pdf.PdfFileWriter
    writer = writer_class()
    overlay_streams = []
    with source.open('rb') as original:
        reader = reader_class(original)
        for index, page in enumerate(reader.pages):
            box = page.mediabox if hasattr(page, 'mediabox') else page.mediaBox
            width = float(box.width if hasattr(box, 'width') else box.getWidth())
            height = float(box.height if hasattr(box, 'height') else box.getHeight())
            font_size = min(width / 11, height / 10)
            labels = []
            for row, message in enumerate(messages):
                y = height / 2 + (row - (len(messages) - 1) / 2) * font_size * 1.3
                labels.append(f'<text x="{width/2}" y="{y}" text-anchor="middle" '
                              f'font-size="{font_size}" font-weight="bold">{escape(message)}</text>')
            labels.append(f'<text x="{width/2}" y="{height/2 + font_size * 2}" '
                          f'text-anchor="middle" font-size="{font_size/3}">'
                          'CHECK FAILED — SEE ERC / DRC REPORTS</text>')
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * 25.4 / 72}mm" '
                   f'height="{height * 25.4 / 72}mm" viewBox="0 0 {width} {height}">'
                   '<g fill="#CC0000" fill-opacity="0.65" font-family="sans-serif">'
                   + ''.join(labels) + '</g></svg>')
            svg_path = directory / 'overlay.svg'
            overlay_path = directory / 'overlay.pdf'
            svg_path.write_text(svg, encoding='utf-8')
            subprocess.run(['rsvg-convert', '-f', 'pdf', '-o', str(overlay_path),
                            str(svg_path)], check=True)
            # Keep readers alive until the writer has resolved all PDF objects.
            stream = overlay_path.open('rb')
            overlay_streams.append(stream)
            overlay = reader_class(stream)
            merge = getattr(page, 'merge_page', None) or page.mergePage
            merge(overlay.pages[0])
            add_page = getattr(writer, 'add_page', None) or writer.addPage
            add_page(page)
        preserve_bookmarks(reader, writer)
        metadata = reader.metadata if hasattr(reader, 'metadata') else reader.getDocumentInfo()
        if metadata:
            add_metadata = getattr(writer, 'add_metadata', None) or writer.addMetadata
            add_metadata({str(k): str(v) for k, v in metadata.items() if v is not None})
        temporary = source.with_name(source.name + '.check-tmp')
        try:
            with temporary.open('wb') as target:
                writer.write(target)
            os.replace(temporary, source)
        finally:
            if temporary.exists():
                temporary.unlink()
            for stream in overlay_streams:
                stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pcb', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path('output'))
    args = parser.parse_args()
    base = args.pcb.stem
    counts = {kind: check_report(args.output_dir / f'{base}-{kind}.json', kind)
              for kind in ('erc', 'drc')}
    status = '\n'.join(f'{kind.upper()}: {count} error(s)' for kind, count in counts.items())
    (args.output_dir / 'check-status.txt').write_text(status + '\n', encoding='utf-8')
    print(status, flush=True)
    messages = [f'{kind.upper()} FAILURE' for kind, count in counts.items() if count]
    if not messages:
        print('Checks passed; no PDF failure notices required.', flush=True)
        return
    sources = []
    for suffix in ('schematic', 'pcb-assembly', 'pcb-fabrication'):
        matches = list(args.output_dir.rglob(f'{base}-{suffix}.pdf'))
        if len(matches) != 1:
            raise RuntimeError(f'Expected exactly one {base}-{suffix}.pdf; found {len(matches)}')
        sources.append(matches[0])
    with tempfile.TemporaryDirectory(prefix='kicad-check-notice-') as temporary:
        for source in sources:
            stamp(source, messages, Path(temporary))
            print(f'Added failure notice to every page: {source}', flush=True)
    print('::warning::' + '; '.join(messages) + '. Generated PDFs contain failure notices.', flush=True)


if __name__ == '__main__':
    main()
