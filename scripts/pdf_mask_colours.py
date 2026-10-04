#!/usr/bin/env python3
"""Derive PDF mask colours from KiCad physical stackup, without dependencies.

Prints two space-separated #RRGGBBAA values (top, bottom) to stdout.
Diagnostics go to stderr. Does not modify the PCB or KiBot configuration.
Named RGB values follow KiCad 10's standard fabrication colour palette.
Custom source alpha is replaced by --opacity.
"""

import argparse
import math
from pathlib import Path
import re
import sys


NAMED_COLOURS = {
    "green": (60, 150, 80), "red": (128, 0, 0),
    "blue": (0, 0, 128), "purple": (80, 0, 80),
    "black": (20, 20, 20), "white": (200, 200, 200),
    "yellow": (128, 128, 0),
}
TOKEN = re.compile(r'\s+|\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+')


def parse_board(text):
    """Parse KiCad S-expressions, respecting quoted parentheses and escapes."""
    root = []
    stack = [root]
    end = 0
    for match in TOKEN.finditer(text):
        if match.start() != end:
            raise ValueError("Invalid board syntax near character %d" % end)
        end = match.end()
        token = match.group()
        if token.isspace():
            continue
        if token == "(":
            node = []
            stack[-1].append(node)
            stack.append(node)
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("Unbalanced board parentheses")
            stack.pop()
        elif token.startswith('"'):
            stack[-1].append(re.sub(r'\\(.)', r'\1', token[1:-1]))
        else:
            stack[-1].append(token)
    if end != len(text) or len(stack) != 1:
        raise ValueError("Incomplete board syntax")
    if len(root) != 1 or not root[0] or root[0][0] != "kicad_pcb":
        raise ValueError("Expected a KiCad PCB file")
    return root[0]


def child(node, name):
    return next((item for item in node if isinstance(item, list)
                 and item and item[0] == name), [])


def mask_names(board):
    setup = child(board, "setup")
    stackup = child(setup, "stackup")
    result = {}
    for item in stackup:
        if isinstance(item, list) and len(item) > 1 and item[0] == "layer":
            if item[1] in ("F.Mask", "B.Mask"):
                colour = child(item, "color")
                result[item[1]] = colour[1] if len(colour) > 1 else ""
    return result


def rgb(value):
    value = value.strip()
    if value.lower() in NAMED_COLOURS:
        return NAMED_COLOURS[value.lower()]
    if re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    raise ValueError("Expected a standard mask colour name or #RRGGBB[AA]")


def fraction(value):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("Must be between 0 and 1")
    return number


def lighten(colour, amount, opacity):
    channels = [int(c + (255 - c) * amount + 0.5) for c in colour]
    channels.append(int(opacity * 255 + 0.5))
    return "#" + "".join("%02X" % channel for channel in channels)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcb", required=True, type=Path)
    parser.add_argument("--lighten", type=fraction, default=0.75,
                        help="White blend: 0 original colour, 1 white (default 0.75)")
    parser.add_argument("--opacity", type=fraction, default=0.5,
                        help="0 transparent, 1 opaque (default 0.5)")
    parser.add_argument("--fallback", default="#808080",
                        help="Colour for missing/unknown stackup values (default grey)")
    args = parser.parse_args()
    try:
        fallback = rgb(args.fallback)
        names = mask_names(parse_board(args.pcb.read_text(encoding="utf-8")))
        colours = []
        for layer in ("F.Mask", "B.Mask"):
            name = names.get(layer, "")
            try:
                source = rgb(name)
            except ValueError:
                source = fallback
                print("WARNING: %s stackup colour %r is missing/unsupported; "
                      "using %s" % (layer, name, args.fallback), file=sys.stderr)
            value = lighten(source, args.lighten, args.opacity)
            print("PDF mask colour: %s %r -> %s" % (layer, name, value),
                  file=sys.stderr)
            colours.append(value)
        print(" ".join(colours))
    except (OSError, ValueError) as error:
        parser.exit(1, "Mask colour helper: %s\n" % error)


if __name__ == "__main__":
    main()
