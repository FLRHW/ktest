#!/usr/bin/env python3
r"""Clone libraries used by a modern KiCad project. Python 3.9+, no dependencies.

Copying is the default. --activate additionally updates project library tables
and PCB 3D-model references, with backups of changed project files.
Every run pauses for confirmation that the project manager is open and editors
are closed. Enter rechecks manager detection; q cancels. When detection is
unavailable, the user's readiness acknowledgment is accepted. A separate yes/no
prompt precedes writes; dry runs only request readiness. Prompts are separated
from other output by blank lines.

Output names remain human-readable, preserving valid original names. Numbered
suffixes resolve sanitized or case-insensitive collisions, with separate name
allocation for symbol and footprint directories.

Linux:   python3 scripts/clone_libs_v1_15.py
Windows: py scripts\clone_libs_v1_15.py
Anywhere: python3 clone_libs_v1_15.py /path/to/project
Preview: python3 clone_libs_v1_15.py --dry-run
Copy (default): python3 clone_libs_v1_15.py
Copy and activate: python3 clone_libs_v1_15.py --activate

If multiple KiCad versions are installed, select yours with --kicad-version 9.
For an AppImage installation, leave KiCad running while executing this script.
Active KiCad mounts are discovered automatically. No temporary mount path needs
to be entered. When multiple candidate model directories are present, automatic
selection is refused instead of choosing arbitrarily; close other KiCad
AppImages or define the correct path in KiCad's saved Configure Paths settings.
For a nonstandard installation, --config-dir selects the exact directory that
contains kicad_common.json, sym-lib-table and fp-lib-table. --var NAME=VALUE can
supply a missing variable without editing this script. For example:
    python3 clone_libs.py --var "MY_LIBS=/mnt/shared/KiCad Libraries"

The script scans files matching a selected .kicad_pro basename, following schematic
sheet links into subdirectories or external locations. Nested unrelated projects
are not scanned. --all-designs scans all root-level schematics and boards.
When no single project can be selected, all root designs are scanned.
--diagnose prints path/table discovery details and performs a dry run.
If the argument is omitted, it looks in the working directory,
then the script's directory and its parents, supporting project/scripts/.

Output:
    lib/lib_sym/        whole used symbol libraries
    lib/lib_fp/         selected footprint files
    lib/3D_models/      referenced model files (numbered suffixes avoid collisions)
    lib/sym-lib-table   native KiCad table (installed in project root only with --activate)
    lib/fp-lib-table    native KiCad table (installed in project root only with --activate)
    lib/clone-report.json
With --activate, project-level tables are merged with these entries; existing
unrelated entries remain. Board model paths and copied footprint model paths use
${KIPRJMOD}. Original schematic files are not edited. Backups and restoration
instructions (restore.json) go in lib/project-backup/.
Repeated runs refresh a managed snapshot in place, retaining its previous contents
in a sibling lib-backup-<date>-<time>-<unique>/lib directory. Original source table
entries are saved in clone-report.json so newly used parts can be retrieved from
master libraries after activation. If masters are unavailable, existing local
copies are used and missing parts remain fatal errors. Refresh does not merge
manual edits in snapshot libraries: the previous snapshot preserves those edits.
A populated directory without a valid report is never replaced. Copy mode
can refresh an inactive snapshot; copying over the currently active snapshot
requires --activate because its project mappings may need updating. --copy-only
is retained as an explicit alias for the default copy mode.

Unresolved symbols, footprints, library tables and sheet files cause exit code 2
before creating a snapshot. Unresolved 3D entries produce warnings by default:
only working models are copied; unresolved entries are omitted from copied
footprints and from boards during activation. Source library files and
unactivated boards remain unchanged. Warnings are recorded in clone-report.json.
--strict-models makes unresolved model groups fatal. Successful completion
(including nonfatal model warnings) returns 0.

Alternate 3D paths: entries in the same footprint with the same case-sensitive
model filename and identical non-path settings are treated as alternatives.
.stp and .step extensions are equivalent. The first accessible path wins.
Only one such entry is retained in copied footprints and activated boards.
Different filenames or settings represent distinct model groups. A missing
alternative is not an error if another path in its group resolves. The report
records the original paths and selected source. Source library files are never
modified.

Scope: modern .kicad_sch/.kicad_pcb, KiCad packed/unpacked symbol and .pretty libraries.
Legacy/database/remote libraries, embedded 3D resources and old 3D aliases are
reported as unsupported. SPICE models, datasheets, textures and other external
resources are outside the scope. This is a library snapshot, not a full archive.
"""

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

SCRIPT_VERSION = '1.15'


class Error(Exception):
    pass


def kicad_running():
    """Best-effort project-manager check. None means unavailable, not 'closed'."""
    def matches(name):
        name = name.strip().replace('\\', '/').rsplit('/', 1)[-1].lower()
        return name in ('kicad', 'kicad.exe')
    try:
        if sys.platform.startswith('linux') and Path('/proc').is_dir():
            checked = False
            for path in Path('/proc').iterdir():
                if not path.name.isdigit():
                    continue
                try:
                    try:
                        name = str((path / 'exe').readlink()).removesuffix(' (deleted)')
                    except OSError:
                        name = (path / 'comm').read_text()
                    checked = True
                    if matches(name):
                        return True
                except OSError:
                    continue  # Processes may exit during enumeration.
            return False if checked else None
        command = (['tasklist', '/FO', 'CSV', '/NH'] if sys.platform == 'win32'
                   else ['ps', '-A', '-o', 'comm='])
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        if sys.platform == 'win32':
            import csv
            names = [row[0] for row in csv.reader(result.stdout.splitlines()) if row]
        else:
            names = result.stdout.splitlines()
        return any(matches(name) for name in names)
    except (OSError, subprocess.SubprocessError):
        return None


def warn_if_kicad_closed():
    if kicad_running() is False:
        print('WARNING: No running KiCad process detected. If using a KiCad AppImage, '
              'open KiCad and leave its project manager running so the temporary '
              'library mount stays available, then rerun this command. '
              'Before activation, close schematic/PCB editors first.', file=sys.stderr)


def prompt_input(prompt):
    """Visually separate every user prompt, including retries and cancellation."""
    print()
    try:
        return input(prompt)
    finally:
        print()


def wait_for_kicad():
    """Wait before discovery so newly opened AppImage mounts can be found."""
    prompt = ('Press Enter when this project is open in the manager and the editors '
              'are closed, or type q to cancel: ')
    while True:
        try:
            answer = prompt_input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('\nCancelled. No files changed.')
            return False
        if answer in ('q', 'quit', 'cancel', 'no', 'n'):
            print('Cancelled. No files changed.')
            return False
        if answer:
            print('Please press Enter to continue or type q to cancel.')
            continue
        state = kicad_running()
        if state is True:
            return True
        if state is None:
            print('Unable to verify the project-manager process; proceeding based '
                  'on your readiness acknowledgment.')
            return True  # Explicit readiness acknowledgment when detection fails.
        print('KiCad project manager is not detected. Open this project in the '
              'manager and leave it open. No files have been scanned or changed.')
        prompt = 'Press Enter to recheck after opening the manager, or type q to cancel: '
        # Detection is attempted again only after the user opens KiCad and responds.


def confirm_proceed(prompt):
    """Only an explicit yes/y authorizes writes. EOF and interruption cancel."""
    while True:
        try:
            answer = prompt_input(prompt + ' [yes/no]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('\nCancelled. No files changed.')
            return False
        if answer in ('yes', 'y'):
            return True
        if answer in ('no', 'n', ''):
            print('Cancelled. No files changed.')
            return False
        print('Please answer yes or no (Enter cancels).')


class Atom(str):
    """S-expression value with source offsets (for lossless targeted edits)."""
    def __new__(cls, value, start=0, end=0):
        obj = str.__new__(cls, value)
        obj.start, obj.end = start, end
        return obj


class Node(list):
    """S-expression node with full source span for removing alternate entries."""
    def __init__(self, start):
        super().__init__()
        self.start, self.end = start, start


def parse(text):
    tokens = re.finditer(r'\s+|;[^\n]*|[()]|"(?:\\.|[^"\\])*"|[^\s()"]+', text)
    roots, stack = [], []
    end = 0
    for match in tokens:
        if match.start() != end:
            raise Error('Invalid or unterminated S-expression string')
        end = match.end()
        token = match.group()
        if token.isspace() or token.startswith(';'):
            continue
        if token == '(':
            node = Node(match.start())
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ')':
            if not stack:
                raise Error('Unbalanced S-expression')
            stack.pop().end = match.end()
        else:
            if token.startswith('"'):
                # Decode only KiCad escapes; preserve unknown escapes/backslashes.
                token = re.sub(r'\\([\\"nr t])', lambda m: {
                    '\\': '\\', '"': '"', 'n': '\n', 'r': '\r',
                    't': '\t', ' ': ' '}.get(m[1], m[0]), token[1:-1])
            (stack[-1] if stack else roots).append(Atom(token, match.start(), match.end()))
    if end != len(text) or stack or len(roots) != 1 or not isinstance(roots[0], list):
        raise Error('Expected one balanced S-expression')
    return roots[0]


def children(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def value(node, key, default=''):
    found = children(node, key)
    return str(found[0][1]) if found and len(found[0]) > 1 else default


def walk(node, key):
    if node and node[0] == key:
        yield node
    for child in node:
        if isinstance(child, list):
            yield from walk(child, key)


def quote(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def dump(node):
    if not node:
        return '()'
    # Keywords must remain unquoted for KiCad's keyword-aware lexer.
    return '(' + str(node[0]) + ''.join(
        ' ' + (dump(x) if isinstance(x, list) else quote(x)) for x in node[1:]) + ')'


def read(path):
    text = path.read_text(encoding='utf-8-sig')
    return text, parse(text)


def version_key(path):
    return tuple(int(x) for x in re.findall(r'\d+', path.name))


def config_dir(args):
    if args.config_dir:
        path = Path(args.config_dir).expanduser().resolve()
        if not path.is_dir():
            raise Error(f'Configuration directory does not exist: {path}')
        return path
    if os.environ.get('KICAD_CONFIG_HOME'):
        base = Path(os.environ['KICAD_CONFIG_HOME']).expanduser()
    elif sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'kicad'
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Preferences/kicad'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'kicad'
    if args.kicad_version:
        version = args.kicad_version
        path = base / (version if '.' in version else version + '.0')
        if not path.is_dir():
            raise Error(f'KiCad configuration directory does not exist: {path}')
        return path
    paths = [p for p in base.glob('*') if p.is_dir() and re.fullmatch(r'\d+\.\d+', p.name)]
    if paths:
        return max(paths, key=version_key)
    if (base / 'kicad_common.json').exists() or (base / 'sym-lib-table').exists():
        return base
    return None


class Resolver:
    def __init__(self, project, config, overrides):
        self.project, self.config = project, config
        self.vars = {}
        if config and (config / 'kicad_common.json').exists():
            data = json.loads((config / 'kicad_common.json').read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                raise Error('kicad_common.json must contain a JSON object')
            environment = data.get('environment')
            if environment is None:
                environment = {}
            if not isinstance(environment, dict):
                raise Error('kicad_common.json: environment must be an object or null')
            variables = environment.get('vars')
            if variables is None:
                variables = {}
            if not isinstance(variables, dict):
                raise Error('kicad_common.json: environment.vars must be an object or null')
            for name, val in variables.items():
                if val is None:
                    continue  # An unset variable must not suppress installation defaults.
                if not isinstance(val, str):
                    raise Error(f'kicad_common.json: path variable {name} must be a string or null')
                self.vars[name] = val
        # KiCad's process environment overrides its saved Configure Paths values.
        self.vars.update(os.environ)
        for item in overrides:
            if '=' not in item or not item.split('=', 1)[0]:
                raise Error('--var requires NAME=VALUE')
            key, val = item.split('=', 1)
            self.vars[key] = val
        self.vars['KIPRJMOD'] = str(project)
        self.major = version_key(config)[0] if config and version_key(config) else None
        self.install_defaults()

    def install_defaults(self):
        # Standard installation discovery only; never guesses a custom nickname.
        roots = []
        appimage_roots = []
        self.discovery_notes = []
        if self.vars.get('KICAD_STOCK_DATA_HOME'):
            roots.append(Path(self.vars['KICAD_STOCK_DATA_HOME']).expanduser())
        cli = shutil.which('kicad-cli') or shutil.which('kicad')
        if cli:
            roots.append(Path(cli).resolve().parent.parent / 'share/kicad')
        if sys.platform == 'win32':
            for base, suffix in [(os.environ.get('ProgramFiles'), 'KiCad'),
                                 (os.environ.get('LOCALAPPDATA'), 'Programs/KiCad')]:
                if base:
                    root = Path(base) / suffix
                    roots += sorted(root.glob('*/share/kicad'), key=lambda p: version_key(p.parents[1]), reverse=True)
        elif sys.platform == 'darwin':
            roots += [Path('/Applications/KiCad/KiCad.app/Contents/SharedSupport')]
        else:
            roots += [Path('/usr/share/kicad'), Path('/usr/local/share/kicad')]
            # AppImage mounts exist only while the runtime is active. Match the
            # mount mechanism, not a user's random per-launch directory name.
            appdir = self.vars.get('APPDIR')
            if appdir:
                appimage_roots.extend([Path(appdir) / 'share/kicad', Path(appdir) / 'usr/share/kicad'])
            temporary_dirs = {Path(tempfile.gettempdir()), Path('/tmp')}
            for temporary in sorted(temporary_dirs):
                for mount in sorted(temporary.glob('.mount_*')):
                    for relative in ('share/kicad', 'usr/share/kicad'):
                        stock = mount / relative
                        if stock.is_dir() and any((stock / folder).is_dir()
                                                  for folder in ('symbols', 'footprints', '3dmodels')):
                            appimage_roots.append(stock)
            roots.extend(appimage_roots)
        # Saved/custom symbol and footprint roots may reveal an installation root.
        for name, val in list(self.vars.items()):
            if re.fullmatch(r'KICAD\d+_(SYMBOL_DIR|FOOTPRINT_DIR)', name):
                try:
                    roots.append(self.path(val).parent)
                except Error:
                    pass
        self.stock_roots = list(dict.fromkeys(roots))
        self.checked_paths = {}
        if self.major:
            for suffix, folders in [('SYMBOL_DIR', ['symbols']), ('FOOTPRINT_DIR', ['footprints']),
                                    ('3DMODEL_DIR', ['3dmodels', 'packages3d']), ('TEMPLATE_DIR', ['template'])]:
                name = f'KICAD{self.major}_{suffix}'
                self.checked_paths[name] = [r / folder for r in self.stock_roots for folder in folders]
                if name not in self.vars:
                    candidates = list(dict.fromkeys(p.resolve() for p in self.checked_paths[name] if p.is_dir()))
                    mounted = list(dict.fromkeys((r / folder).resolve()
                                  for r in appimage_roots for folder in folders if (r / folder).is_dir()))
                    if mounted:
                        candidates = mounted  # Running AppImage supplies its own stock libraries.
                    if len(candidates) == 1 or (candidates and not mounted and suffix != '3DMODEL_DIR'):
                        self.vars[name] = str(candidates[0])
                    elif len(candidates) > 1:
                        self.discovery_notes.append(f'{name}: multiple candidate directories; '
                                                    + ', '.join(map(str, candidates)))

    def expand(self, text):
        pattern = re.compile(r'\$\{([^}]+)\}|\$\(([^)]+)\)|\$([A-Za-z_][A-Za-z0-9_]*)|%([^%]+)%')
        seen = set()
        for _ in range(30):
            if text in seen:
                raise Error(f'Cyclic path variable: {text}')
            seen.add(text)
            missing = set()
            def replace(match):
                name = next(x for x in match.groups() if x is not None)
                val = self.vars.get(name)
                if val is None and self.major:
                    old = re.fullmatch(r'KICAD\d+_(SYMBOL_DIR|FOOTPRINT_DIR|3DMODEL_DIR|3RD_PARTY)', name)
                    if old:
                        val = self.vars.get(f'KICAD{self.major}_{old[1]}')
                if val is None:
                    missing.add(name)
                    return match[0]
                return str(val)
            expanded = pattern.sub(replace, text)
            if missing:
                raise Error('Undefined path variable(s): ' + ', '.join(sorted(missing)))
            if not pattern.search(expanded):
                return os.path.expanduser(expanded)
            text = expanded
        raise Error('Path variable expansion too deep')

    def path(self, raw, base=None):
        text = self.expand(raw)
        if text.startswith(':') or '://' in text:
            raise Error(f'Unsupported path/embedded resource/legacy 3D alias: {raw}')
        if sys.platform != 'win32' and (re.match(r'^[A-Za-z]:[\\/]', text) or text.startswith('\\\\')):
            raise Error(f'Windows path is not accessible on this OS: {text}')
        path = Path(text.replace('\\', '/'))
        return (path if path.is_absolute() else (base or self.project) / path).resolve()


def tables(project, config, name, resolver=None):
    merged = {}
    def load(path, ancestors=()):
        path = path.resolve()
        if path in ancestors:
            raise Error(f'Nested library table cycle: {path}')
        if len(ancestors) >= 40:
            raise Error(f'Nested library table depth exceeded: {path}')
        if resolver:
            resolver.table_sources.add(path)
        _, root = read(path)
        if root[0] != name.replace('-', '_'):
            raise Error(f'Invalid table: {path}')
        result = {}
        for entry in children(root, 'lib'):
            if resolver and value(entry, 'type') == 'Table':
                if children(entry, 'disabled'):
                    continue
                nested = resolver.path(value(entry, 'uri'), path.parent)
                result.update(load(nested, ancestors + (path,)))
            else:
                # Resolve relative leaf URIs against their containing table.
                if resolver:
                    entry = [list(x) if isinstance(x, list) else x for x in entry]
                    for field in children(entry, 'uri'):
                        raw = str(field[1])
                        # Defer variable expansion and unsupported-type checks until
                        # a library is actually used; unrelated broken entries are OK.
                        if not any(mark in raw for mark in ('$', '%', '://')) and not (
                                Path(raw).is_absolute() or re.match(r'^[A-Za-z]:[\\/]', raw)):
                            field[1] = str(path.parent / raw)
                result[value(entry, 'name')] = entry
        return result
    for folder in [config, project]:
        if folder and (folder / name).exists():
            merged.update(load(folder / name))
    return merged


def safe_name(name):
    # Preserve readable Unicode, spaces, dots and hyphens. Only sanitize characters
    # forbidden by Windows, trailing dots/spaces, reserved names and long names.
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).rstrip(' .') or 'library'
    stem = stem.encode('utf-8')[:180].decode('utf-8', errors='ignore').rstrip(' .') or 'library'
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if stem.split('.', 1)[0].upper() in reserved:
        stem = '_' + stem
    return stem


def allocate_name(name, occupied, is_file=False):
    """Keep a name if available; resolve collisions using _2, _3, etc."""
    name = safe_name(name)
    suffix = Path(name).suffix if is_file else ''
    stem = name[:-len(suffix)] if suffix else name
    candidate, number = name, 2
    while candidate.casefold() in occupied:
        candidate = f'{stem}_{number}{suffix}'
        number += 1
    occupied.add(candidate.casefold())
    return candidate


def readable_library_names(nicknames):
    # Reserve already-valid nicknames first, so a sanitized nickname cannot
    # take a valid original's name (e.g. A:B cannot take A_B's destination).
    occupied, result = set(), {}
    names = sorted(set(nicknames))
    for name in names:
        if safe_name(name) == name:
            result[name] = allocate_name(name, occupied)
    for name in names:
        if name not in result:
            result[name] = allocate_name(name, occupied)
    return result


def local_entry(name, uri):
    return ['lib', ['name', name], ['type', 'KiCad'], ['uri', uri],
            ['options', ''], ['descr', 'Project-local library snapshot']]


def table_text(kind, entries):
    return '(' + kind.replace('-', '_') + '\n  (version 7)\n' + ''.join(
        '  ' + dump(entry) + '\n' for _, entry in sorted(entries.items())) + ')\n'


def rewrite(text, edits):
    for start, end, replacement in sorted(set(edits), reverse=True):
        text = text[:start] + (quote(replacement) if replacement is not None else '') + text[end:]
    return text


def discover_project(arg):
    if arg:
        p = Path(arg).expanduser().resolve()
        if p.is_file() and p.suffix in ('.kicad_pro', '.kicad_sch', '.kicad_pcb'):
            return p.parent
        if not p.is_dir():
            raise Error(f'Project directory does not exist: {p}')
        return p
    # Current directory wins; then script directory/parents (supports scripts/).
    for p in [Path.cwd().resolve(), *Path(__file__).resolve().parents]:
        if any(p.glob('*.kicad_pro')) or any(p.glob('*.kicad_sch')) or any(p.glob('*.kicad_pcb')):
            return p
    raise Error('No project found. Supply the project directory as an argument.')


class Cloner:
    def __init__(self, args):
        self.args = args
        self.project = discover_project(args.project)
        self.output = self.project / args.output
        if Path(args.output).is_absolute() or '..' in Path(args.output).parts or self.output == self.project:
            raise Error('--output must be a subdirectory of the project, e.g. lib')
        self.output = self.output.resolve()
        if not self.output.is_relative_to(self.project):
            raise Error('Output must remain inside the project')
        self.config = config_dir(args)
        self.resolver = Resolver(self.project, self.config, args.var)
        self.resolver.table_sources = set()
        self.symbols, self.footprints = defaultdict(set), defaultdict(set)
        self.sym_table = tables(self.project, self.config, 'sym-lib-table', self.resolver)
        self.fp_table = tables(self.project, self.config, 'fp-lib-table', self.resolver)
        self.files, self.models, self.boards = {}, {}, {}
        self.model_output_names = set()
        self.entries = {'sym-lib-table': {}, 'fp-lib-table': {}}
        self.issues, self.scanned = [], []
        self.model_choices = []
        self.model_warnings = []
        selected = Path(args.project) if args.project else None
        projects = sorted(self.project.glob('*.kicad_pro'))
        self.project_stem = (selected.stem if selected and selected.suffix in
                             ('.kicad_pro', '.kicad_sch', '.kicad_pcb') else
                             projects[0].stem if len(projects) == 1 else None)

    def prepare_refresh(self):
        """Recover original table entries before selecting snapshot sources."""
        self.previous_report = None
        self.source_entries = {'sym-lib-table': {}, 'fp-lib-table': {}}
        populated = self.output.is_dir() and any(self.output.iterdir())
        if self.output.exists() and not self.output.is_dir():
            raise Error(f'Output is not a directory: {self.output}')
        if populated:
            report_path = self.output / 'clone-report.json'
            if not report_path.is_file():
                raise Error('Output is not a managed snapshot (clone-report.json missing); nothing replaced.')
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if Path(report.get('project', '')).resolve() != self.project or not report.get('script_revision'):
                raise Error('Snapshot report does not identify this project; nothing replaced.')
            self.previous_report = report
        # A different destination can still recover sources from the currently
        # activated snapshot, identified by the project table's library URI.
        self.active_output = False
        reports = []
        if self.previous_report:
            reports.append((self.output, self.previous_report))
        for table in (self.sym_table, self.fp_table):
            for entry in table.values():
                try:
                    path = self.resolver.path(value(entry, 'uri'))
                    if path.is_relative_to(self.output):
                        self.active_output = True
                    root = path.parent.parent
                    report_path = root / 'clone-report.json'
                    if root.is_relative_to(self.project) and report_path.is_file() and all(root != x[0] for x in reports):
                        report = json.loads(report_path.read_text(encoding='utf-8'))
                        if Path(report.get('project', '')).resolve() == self.project:
                            reports.append((root, report))
                except (Error, OSError, ValueError):
                    continue
        for kind, current in [('sym-lib-table', self.sym_table), ('fp-lib-table', self.fp_table)]:
            # Retain source identities even when the design temporarily stops
            # using a library, so re-adding its parts works on a later refresh.
            for _, report in reversed(reports):
                for nickname, saved in report.get('source_tables', {}).get(kind, {}).items():
                    original = parse(saved)
                    self.source_entries[kind][nickname] = original
                    current.setdefault(nickname, original)
            global_entries = tables(self.project / '.no-project-tables', self.config, kind, self.resolver)
            for nickname, entry in list(current.items()):
                original = entry
                try:
                    path = self.resolver.path(value(entry, 'uri'))
                except Error:
                    path = None
                for root, report in reports:
                    # Copy-only reports also retain the original sources.
                    saved = report.get('source_tables', {}).get(kind, {}).get(nickname)
                    if path is None or not path.is_relative_to(root):
                        continue
                    if saved:
                        original = parse(saved)
                    else:
                        # Upgrade v1.13 snapshots using their source/destination
                        # manifest. Current global URIs survive AppImage remounts.
                        relative = path.relative_to(root)
                        matches = [f for f in report.get('files', [])
                                   if Path(f['destination']) == relative or relative in Path(f['destination']).parents]
                        if matches:
                            source = Path(matches[0]['source'])
                            if path.is_dir():
                                source = source.parent
                            original = local_entry(nickname, str(source))
                            global_entry = global_entries.get(nickname)
                            if global_entry and (str(source).startswith('/tmp/.mount_') or
                                                  value(global_entry, 'uri') == str(source)):
                                original = global_entry
                    break
                self.source_entries[kind][nickname] = original
                # Prefer original libraries, but permit an offline refresh from
                # existing local copies. Missing new items still fail planning.
                try:
                    self.library(nickname, {nickname: original}, kind == 'sym-lib-table')
                    current[nickname] = original
                except (Error, OSError):
                    if original != entry:
                        print(f'WARNING: Original library unavailable for {nickname}; using existing local copy.')

    def design_files(self, extension):
        if self.project_stem and not self.args.all_designs:
            path = self.project / (self.project_stem + extension)
            return [path] if path.exists() else []
        return sorted(self.project.glob('*' + extension))

    def issue(self, message):
        if message not in self.issues:
            self.issues.append(message)

    def ref(self, ref, target, context):
        if not ref:
            return
        if ':' not in ref:
            self.issue(f'{context}: reference has no library nickname: {ref}')
            return
        lib, item = ref.split(':', 1)
        if not lib or not item:
            self.issue(f'{context}: invalid reference: {ref}')
        else:
            target[lib].add(item)

    def scan(self):
        pending = self.design_files('.kicad_sch')
        seen = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            text, root = read(path)
            self.scanned.append(str(path))
            for symbol in children(root, 'symbol'):
                self.ref(value(symbol, 'lib_id'), self.symbols, str(path))
                for prop in children(symbol, 'property'):
                    if len(prop) > 2 and prop[1] == 'Footprint':
                        self.ref(str(prop[2]), self.footprints, str(path))
            for sheet in children(root, 'sheet'):
                for prop in children(sheet, 'property'):
                    if len(prop) > 2 and str(prop[1]).lower() in ('sheetfile', 'sheet file'):
                        try:
                            child = self.resolver.path(str(prop[2]), path.parent)
                            if not child.is_file():
                                raise Error(f'{path}: missing hierarchical schematic: {child}')
                            pending.append(child)
                        except Error as exc:
                            self.issue(str(exc))
        for path in self.design_files('.kicad_pcb'):
            text, root = read(path)
            self.scanned.append(str(path))
            board_edits = []
            for footprint in children(root, 'footprint'):
                ref = str(footprint[1])
                self.ref(ref, self.footprints, str(path))
                source_parent = None
                if ':' in ref:
                    nickname, name = ref.split(':', 1)
                    try:
                        folder = self.library(nickname, self.fp_table, False)
                        source_parent = folder
                    except Error:
                        pass  # Library failure is reported by copy planning below.
                board_edits += self.model_edits(footprint, str(path), source_parent)
            self.boards[path] = rewrite(text, board_edits)
        if not self.scanned:
            raise Error('No modern schematic or PCB files found at the project root')

    def library(self, nickname, table, symbol):
        entry = table.get(nickname)
        if entry is None:
            raise Error(f'Library nickname absent from project/global tables: {nickname}')
        if children(entry, 'disabled'):
            raise Error(f'Library is disabled: {nickname}')
        if value(entry, 'type') != 'KiCad':
            raise Error(f'Unsupported library type for {nickname}: {value(entry, "type")}')
        path = self.resolver.path(value(entry, 'uri'))
        if symbol and not (path.is_file() and path.suffix == '.kicad_sym' or
                           path.is_dir() and any(path.glob('*.kicad_sym'))):
            raise Error(f'Symbol library missing or unsupported: {path}')
        if not symbol and (not path.is_dir() or path.suffix != '.pretty'):
            raise Error(f'Footprint library missing or unsupported: {path}')
        return path

    def uri(self, relative):
        return '${KIPRJMOD}/' + (self.output.relative_to(self.project) / relative).as_posix()

    def model_edits(self, node, context, footprint_folder=None):
        edits = []
        groups = defaultdict(list)
        for model in walk(node, 'model'):
            if len(model) < 2 or isinstance(model[1], list):
                continue
            # A directory/OS change preserves the model filename. Do not group
            # differently placed instances or different files in an assembly.
            filename = str(model[1]).replace('\\', '/').rsplit('/', 1)[-1]
            if filename.lower().endswith(('.step', '.stp')):
                filename = filename.rsplit('.', 1)[0] + '.step'
            settings = tuple(sorted(dump(x) if isinstance(x, list) else str(x)
                                    for x in model[2:]))
            groups[(filename, settings)].append(model)
        for (filename, _), alternatives in groups.items():
            selected, source, failures = None, None, []
            for model in alternatives:
                raw = str(model[1])
                try:
                    base = footprint_folder if raw.startswith(('./', '../', '.\\', '..\\')) else self.project
                    candidate = self.resolver.path(raw, base)
                    if not candidate.is_file():
                        raise Error(f'3D model not found: {candidate}')
                    selected, source = model, candidate
                    break
                except Error as exc:
                    failures.append(f'{raw!r}: {exc}')
            if selected is None:
                message = f'{context}: no accessible path for model {filename!r}; tried ' + '; '.join(failures)
                if self.args.strict_models:
                    self.issue(message)
                else:
                    self.model_warnings.append({'context': context, 'model': filename,
                                                'paths': [str(x[1]) for x in alternatives],
                                                'failures': failures, 'message': message})
                    for model in alternatives:
                        edits.append((model.start, model.end, None))
                continue
            relative = self.models.get(source)
            if relative is None:
                relative = Path('3D_models') / allocate_name(source.name, self.model_output_names, is_file=True)
                self.models[source] = relative
            atom = selected[1]
            edits.append((atom.start, atom.end, self.uri(relative)))
            for model in alternatives:
                if model is not selected:
                    edits.append((model.start, model.end, None))
            if len(alternatives) > 1:
                self.model_choices.append({'context': context, 'model': filename,
                                           'paths': [str(x[1]) for x in alternatives],
                                           'selected_source': str(source)})
        return edits

    def plan(self):
        self.scan()
        # These destinations are separate directories and cannot collide with
        # each other, even when symbol/footprint nicknames differ only by case.
        symbol_names = readable_library_names(self.symbols)
        footprint_names = readable_library_names(self.footprints)
        for nickname, used in sorted(self.symbols.items()):
            try:
                source = self.library(nickname, self.sym_table, True)
                sources = sorted(source.glob('*.kicad_sym')) if source.is_dir() else [source]
                available = set()
                for src in sources:
                    _, root = read(src)
                    available.update(str(x[1]) for x in children(root, 'symbol'))
                missing = used - available
                if missing:
                    raise Error(f'{nickname}: symbols absent from library: {", ".join(sorted(missing))}')
                relative = Path('lib_sym') / (symbol_names[nickname] +
                                              ('.kicad_symdir' if source.is_dir() else '.kicad_sym'))
                for src in sources:
                    self.files[relative / src.name if source.is_dir() else relative] = (src, None)
                self.entries['sym-lib-table'][nickname] = local_entry(nickname, self.uri(relative))
            except (Error, OSError) as exc:
                self.issue(str(exc))
        for nickname, used in sorted(self.footprints.items()):
            try:
                folder = self.library(nickname, self.fp_table, False)
                relative = Path('lib_fp') / (footprint_names[nickname] + '.pretty')
                for name in sorted(used):
                    source = folder / (name + '.kicad_mod')
                    if source.parent != folder or not source.is_file():
                        self.issue(f'{nickname}:{name}: footprint file missing/invalid: {source}')
                        continue
                    text, root = read(source)
                    edits = self.model_edits(root, str(source), folder)
                    self.files[relative / source.name] = (source, rewrite(text, edits))
                self.entries['fp-lib-table'][nickname] = local_entry(nickname, self.uri(relative))
            except (Error, OSError) as exc:
                self.issue(str(exc))

    def run(self):
        print(f'{Path(__file__).name} revision {SCRIPT_VERSION}')
        print(f'Project: {self.project}')
        if self.args.dry_run or self.args.diagnose:
            print('Mode: dry run; no files will be changed.')
        elif self.args.activate:
            print('Mode: copy and activate local libraries, with project-file backups.')
        else:
            print('Mode: copy only; project library tables and PCB files will be unchanged.')
        print(f'KiCad configuration: {self.config or "not found (project tables/environment only)"}')
        for note in self.resolver.discovery_notes:
            print('DISCOVERY: ' + note)
        self.prepare_refresh()
        self.plan()
        for choice in self.model_choices:
            print(f'Alternate paths resolved: {choice["context"]}: '
                  f'{choice["model"]} -> {choice["selected_source"]}')
        if self.args.diagnose:
            print(f'Design selection: {self.project_stem if self.project_stem and not self.args.all_designs else "all root designs"}')
            print(f'Resolved library table entries: {len(self.sym_table)} symbols, {len(self.fp_table)} footprints')
            for path in sorted(self.resolver.table_sources):
                print(f'Table read: {path}')
            for name in sorted(self.resolver.checked_paths):
                print(f'Path variable {name}: {self.resolver.vars.get(name, "UNDEFINED")}')
                if name not in self.resolver.vars:
                    print('  Checked: ' + ', '.join(map(str, self.resolver.checked_paths[name])))
            for name, val in sorted(self.resolver.vars.items()):
                if re.fullmatch(r'KICAD\d+_3RD_PARTY', name):
                    print(f'Path variable {name}: {val}')
        print(f'Found {len(self.scanned)} design files; {len(self.symbols)} symbol libraries; '
              f'{sum(map(len, self.footprints.values()))} distinct footprints; {len(self.models)} 3D models')
        for issue in self.issues:
            print('UNRESOLVED: ' + issue, file=sys.stderr)
        for warning in self.model_warnings:
            print('WARNING: Skipping unresolved 3D entry: ' + warning['message'], file=sys.stderr)
        if self.model_warnings:
            print(f'{len(self.model_warnings)} unresolved model group(s) will be omitted from local copies.')
        if self.args.dry_run or self.args.diagnose:
            print('Dry run: no files changed.')
            return 2 if self.issues else 0
        if self.issues:
            raise Error('Nothing written. Fix unresolved resources and run again; use --var NAME=VALUE if needed.')
        if self.active_output and not self.args.activate:
            raise Error('Cannot refresh the active snapshot in copy mode: project references may need updating. '
                        'Run with --activate, or copy to a different --output directory.')
        if self.previous_report:
            print(f'Refreshing managed snapshot: {self.output}. Previous contents will be retained in a sibling backup.')
        if self.args.activate:
            print(f'Libraries will be copied to: {self.output}')
            print(f'The project library tables will be created or modified: '
                  f'{self.project / "sym-lib-table"}, {self.project / "fp-lib-table"}.')
            print('PCB 3D-model references will be updated to local copies. '
                  'Unresolved and duplicate alternative model entries will be omitted.')
            print(f'Original changed project files will be backed up in: {self.output / "project-backup"}')
            question = 'Proceed with copying and modifying this project?'
        else:
            print(f'Libraries will be copied to: {self.output}; project files will not be modified.')
            question = 'Proceed with copying only?'
        if not confirm_proceed(question):
            return 0
        self.output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.clone-libs-', dir=self.output.parent))
        previous = None
        backup_container = None
        installed = False
        try:
            for relative, (source, edited) in self.files.items():
                dest = staging / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                if edited is None:
                    shutil.copy2(source, dest)
                else:
                    dest.write_text(edited, encoding='utf-8')
            for source, relative in self.models.items():
                dest = staging / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
            for kind, entries in self.entries.items():
                (staging / kind).write_text(table_text(kind, entries), encoding='utf-8')
            report = {'project': str(self.project), 'config': str(self.config) if self.config else None,
                      'scanned': self.scanned, 'activation_requested': self.args.activate,
                      'script_revision': SCRIPT_VERSION,
                      'source_tables': {kind: {name: dump(entry) for name, entry in entries.items()}
                                        for kind, entries in self.source_entries.items()},
                      'model_alternatives': self.model_choices,
                      'strict_models': self.args.strict_models, 'unresolved_models': self.model_warnings,
                      'files': [{'source': str(src), 'destination': rel.as_posix()} for rel, (src, _) in self.files.items()],
                      'models': [{'source': str(src), 'destination': rel.as_posix()} for src, rel in self.models.items()]}
            (staging / 'clone-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            if self.previous_report:
                # Refuse to replace a snapshot changed since planning/approval.
                current = json.loads((self.output / 'clone-report.json').read_text(encoding='utf-8'))
                if current != self.previous_report:
                    raise Error('Snapshot report changed during planning; nothing replaced.')
                import datetime
                stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                backup_container = Path(tempfile.mkdtemp(prefix=self.output.name + '-backup-' + stamp + '-',
                                                         dir=self.output.parent))
                previous = backup_container / self.output.name
                self.output.rename(previous)
            elif self.output.exists():
                self.output.rmdir()  # Empty only; refuses newly appearing contents.
            staging.rename(self.output)
            installed = True
            if self.args.activate:
                self.activate()
        except BaseException:
            if previous and previous.exists():
                if installed:
                    # Keep failed snapshot for inspection; restore original lib.
                    self.output.rename(backup_container / 'failed-refresh')
                previous.rename(self.output)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if previous:
            print(f'Previous snapshot retained: {previous}')
        print(f'Snapshot created: {self.output}')
        if not self.args.activate:
            print('Project files unchanged. To activate, run again with --activate; the existing snapshot will be refreshed.')
        return 0

    def activate(self):
        changes = {}
        for kind, additions in self.entries.items():
            path = self.project / kind
            merged = tables(self.project, None, kind)
            # Remove only this snapshot's managed mappings when no longer used.
            if self.previous_report:
                for nickname, entry in list(merged.items()):
                    try:
                        if self.resolver.path(value(entry, 'uri')).is_relative_to(self.output):
                            merged.pop(nickname)
                    except Error:
                        pass
            merged.update(additions)
            changes[path] = table_text(kind, merged)
        for path, text in self.boards.items():
            if path.read_text(encoding='utf-8-sig') != text:
                changes[path] = text
        backup = self.output / 'project-backup'
        backup.mkdir()
        existing = set()
        for path in changes:
            if path.exists():
                shutil.copy2(path, backup / path.name)
                existing.add(path)
        (backup / 'restore.json').write_text(json.dumps({
            'restore': [p.name for p in existing],
            'remove_if_restoring': [p.name for p in changes if p not in existing]}, indent=2), encoding='utf-8')
        try:
            for path, text in changes.items():
                fd, tmp = tempfile.mkstemp(prefix='.clone-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
                        out.write(text)
                    os.replace(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
        except BaseException:
            for path in changes:
                if path in existing:
                    shutil.copy2(backup / path.name, path)
                elif path.exists():
                    path.unlink()
            raise
        print(f'Local libraries activated. Original project files backed up in: {backup}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version', version=f'%(prog)s revision {SCRIPT_VERSION}')
    parser.add_argument('project', nargs='?', help='Project directory or .kicad_pro file; auto-detected if omitted')
    parser.add_argument('--config-dir', help='Exact versioned KiCad configuration directory')
    parser.add_argument('--kicad-version', help='Choose configuration version (e.g. 9 or 9.0); default: highest found')
    parser.add_argument('--var', action='append', default=[], metavar='NAME=VALUE', help='Override a path variable; repeatable')
    parser.add_argument('--output', default='lib', help='Output subdirectory; existing managed snapshots are refreshed with backups (default: lib)')
    parser.add_argument('--dry-run', action='store_true', help='Resolve and validate without writing files')
    parser.add_argument('--diagnose', action='store_true', help='Dry run with library table/path discovery details')
    parser.add_argument('--all-designs', action='store_true', help='Scan every root design instead of selected project files')
    parser.add_argument('--strict-models', action='store_true', help='Treat unresolved 3D model groups as errors instead of warnings')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--copy-only', action='store_false', dest='activate', help='Copy resources without modifying project tables or PCB files')
    mode.add_argument('--activate', action='store_true', dest='activate', help='Update project library tables and PCB model references to local copies, with backups')
    parser.set_defaults(activate=False)
    args = parser.parse_args()
    try:
        print('REMINDER: Open this project in KiCad, keeping only the project manager open. '
              'Close schematic, PCB and footprint editor windows before proceeding. '
              'Leave the manager running while this script reads AppImage libraries.')
        if not wait_for_kicad():
            return 0
        return Cloner(args).run()
    except (Error, OSError, ValueError, RecursionError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
