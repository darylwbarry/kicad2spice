#!/usr/bin/env python3
"""Convert KiCad netlist (.net version E) to SPICE/QSPICE netlist (.cir).

Features:
  - Converts R, C, L passives and SUBCKT instances
  - Infers type from reference designator for un-annotated components
  - Jumpers (JP*) are emitted as 1mΩ resistors (active/closed state)
  - Optionally generates SPICE models for un-modelled ICs via Claude on
    OpenRouter, using datasheets found in a local datasheets/ directory
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# S-expression parser
# ---------------------------------------------------------------------------

def tokenize(text: str) -> Iterator[str]:
    """Yield tokens from a KiCad S-expression string."""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == '(':
            yield '('
            i += 1
        elif c == ')':
            yield ')'
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                ch = text[j]
                if ch == '\\' and j + 1 < n:
                    nxt = text[j + 1]
                    if nxt == '"':
                        buf.append('"')
                    elif nxt == '\\':
                        buf.append('\\')
                    elif nxt == 'n':
                        buf.append('\n')
                    else:
                        buf.append(nxt)
                    j += 2
                elif ch == '"':
                    j += 1
                    break
                else:
                    buf.append(ch)
                    j += 1
            yield ''.join(buf)
            i = j
        else:
            j = i
            while j < n and text[j] not in ' \t\n\r()\"':
                j += 1
            yield text[i:j]
            i = j


def parse(tokens) -> 'list | str':
    """Recursively parse tokens into a nested list / string tree."""
    tok = next(tokens)
    if tok == '(':
        children = []
        while True:
            nxt = _peek(tokens)
            if nxt == ')':
                next(tokens)
                break
            children.append(parse(tokens))
        return children
    else:
        return tok


class _Peekable:
    def __init__(self, it):
        self._it = it
        self._buf = []

    def __iter__(self):
        return self

    def __next__(self):
        if self._buf:
            return self._buf.pop()
        return next(self._it)

    def peek(self):
        if not self._buf:
            self._buf.append(next(self._it))
        return self._buf[-1]


def _peek(tokens: _Peekable) -> str:
    return tokens.peek()


def parse_tree(text: str) -> list:
    tokens = _Peekable(tokenize(text))
    return parse(tokens)


# ---------------------------------------------------------------------------
# Tree helper functions
# ---------------------------------------------------------------------------

def find_children(node: list, tag: str) -> list:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def find_child(node: list, tag: str) -> 'list | None':
    for c in node:
        if isinstance(c, list) and c and c[0] == tag:
            return c
    return None


def get_atom(node: list, tag: str) -> 'str | None':
    child = find_child(node, tag)
    if child is None or len(child) < 2:
        return None
    val = child[1]
    return val if isinstance(val, str) else None


def get_property(node: list, name: str) -> 'str | None':
    """Extract value from `(property (name "X") (value "Y"))` child."""
    for child in find_children(node, 'property'):
        if get_atom(child, 'name') == name:
            v = find_child(child, 'value')
            if v and len(v) >= 2 and isinstance(v[1], str):
                return v[1]
    return None


def has_valueless_property(node: list, name: str) -> bool:
    """Return True if `(property (name "X"))` exists with no `(value ...)` child."""
    for child in find_children(node, 'property'):
        if get_atom(child, 'name') == name:
            return find_child(child, 'value') is None
    return False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NetNode:
    ref: str
    pin: str


@dataclass
class Net:
    code: str
    name: str
    spice_name: str
    nodes: list = field(default_factory=list)


@dataclass
class Component:
    ref: str
    value: str
    sim_device: 'str | None'
    sim_pins: 'str | None'
    sim_library: 'str | None'
    sim_name: 'str | None'
    dnp: bool
    pin_to_spice: dict = field(default_factory=dict)    # {"1": "VOUT", ...}
    spice_pin_order: list = field(default_factory=list) # ["VOUT", ...] by int key


@dataclass
class Netlist:
    source: str
    components: dict = field(default_factory=dict)          # ref → Component
    ref_pin_to_net: dict = field(default_factory=dict)      # (ref, pin) → Net
    ref_pin_to_func: dict = field(default_factory=dict)     # (ref, pin) → pinfunction str


# ---------------------------------------------------------------------------
# Net name sanitization
# ---------------------------------------------------------------------------

def sanitize_net_name(raw: str) -> str:
    if raw == 'GND':
        return '0'
    name = raw
    name = re.sub(r'^/[^/]+/', '', name)
    if name.startswith('Net-(') and name.endswith(')'):
        name = name[5:-1]
    if name.startswith('unconnected-(') and name.endswith(')'):
        name = 'NC_' + name[13:-1]
    name = re.sub(r'[()/ \t~{}]', '_', name)
    name = name.replace('-', '_')
    name = re.sub(r'_+', '_', name).strip('_')
    if name and name[0].isdigit():
        name = 'N_' + name
    return name or 'UNNAMED'


# ---------------------------------------------------------------------------
# Sim.Pins parsing
# ---------------------------------------------------------------------------

def parse_sim_pins(sim_pins_str: str) -> 'tuple[dict, list]':
    pin_to_spice = {}
    pairs = []
    for token in sim_pins_str.split():
        if '=' in token:
            kicad_pin, spice_name = token.split('=', 1)
            pin_to_spice[kicad_pin.strip()] = spice_name.strip()
            try:
                pairs.append((int(kicad_pin.strip()), spice_name.strip()))
            except ValueError:
                pairs.append((0, spice_name.strip()))
    pairs.sort(key=lambda x: x[0])
    return pin_to_spice, [name for _, name in pairs]


# ---------------------------------------------------------------------------
# Inference from reference designator
# ---------------------------------------------------------------------------

# JP → treated as R (jumper → 1mΩ resistor when active/closed)
_INFER_MAP = {'C': 'C', 'R': 'R', 'L': 'L', 'FB': 'R', 'JP': 'R'}
_DEFAULT_TWO_PIN_PINS = {'1': '+', '2': '-'}
_DEFAULT_TWO_PIN_ORDER = ['+', '-']


def _ref_prefix(ref: str) -> str:
    m = re.match(r'[A-Za-z]+', ref)
    return m.group().upper() if m else ''


def infer_sim_device(ref: str) -> 'str | None':
    return _INFER_MAP.get(_ref_prefix(ref))


# ---------------------------------------------------------------------------
# Netlist parser
# ---------------------------------------------------------------------------

def parse_netlist(path: str) -> Netlist:
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    tree = parse_tree(text)

    if not isinstance(tree, list) or not tree or tree[0] != 'export':
        sys.exit(f'ERROR: {path}: not a KiCad export netlist')

    design = find_child(tree, 'design')
    source = get_atom(design, 'source') if design else path

    components: dict = {}
    comps_node = find_child(tree, 'components')
    if comps_node:
        for comp in find_children(comps_node, 'comp'):
            ref = get_atom(comp, 'ref') or ''
            value = get_atom(comp, 'value') or ''
            dnp = has_valueless_property(comp, 'dnp')

            sim_device = get_property(comp, 'Sim.Device')
            sim_pins_str = get_property(comp, 'Sim.Pins')
            sim_library = get_property(comp, 'Sim.Library')
            sim_name = get_property(comp, 'Sim.Name')

            pin_to_spice: dict = {}
            spice_pin_order: list = []

            if sim_pins_str:
                pin_to_spice, spice_pin_order = parse_sim_pins(sim_pins_str)

            if sim_device is None:
                inferred = infer_sim_device(ref)
                if inferred:
                    sim_device = inferred

            if sim_device in ('R', 'C', 'L') and not pin_to_spice:
                pin_to_spice = dict(_DEFAULT_TWO_PIN_PINS)
                spice_pin_order = list(_DEFAULT_TWO_PIN_ORDER)

            components[ref] = Component(
                ref=ref, value=value, sim_device=sim_device,
                sim_pins=sim_pins_str, sim_library=sim_library,
                sim_name=sim_name, dnp=dnp,
                pin_to_spice=pin_to_spice, spice_pin_order=spice_pin_order,
            )

    ref_pin_to_net: dict = {}
    ref_pin_to_func: dict = {}
    nets_node = find_child(tree, 'nets')
    if nets_node:
        raw_to_net: dict = {}
        spice_name_count: dict = {}

        for net_node in find_children(nets_node, 'net'):
            code = get_atom(net_node, 'code') or ''
            name = get_atom(net_node, 'name') or ''
            spice_name = sanitize_net_name(name)
            raw_to_net[name] = Net(code=code, name=name, spice_name=spice_name)
            spice_name_count[spice_name] = spice_name_count.get(spice_name, 0) + 1

        seen_spice: dict = {}
        collision_warned: set = set()
        for net in raw_to_net.values():
            sn = net.spice_name
            if spice_name_count[sn] > 1:
                if sn not in collision_warned:
                    print(f'WARNING: net name collision for "{sn}"', file=sys.stderr)
                    collision_warned.add(sn)
                cnt = seen_spice.get(sn, 0) + 1
                seen_spice[sn] = cnt
                if cnt > 1:
                    net.spice_name = f'{sn}_{cnt}'
            else:
                seen_spice[sn] = 1

        for net_node in find_children(nets_node, 'net'):
            name = get_atom(net_node, 'name') or ''
            net = raw_to_net[name]
            for node in find_children(net_node, 'node'):
                ref = get_atom(node, 'ref') or ''
                pin = get_atom(node, 'pin') or ''
                func = get_atom(node, 'pinfunction') or ''
                net.nodes.append(NetNode(ref=ref, pin=pin))
                ref_pin_to_net[(ref, pin)] = net
                if func:
                    ref_pin_to_func[(ref, pin)] = func

    return Netlist(source=source, components=components,
                   ref_pin_to_net=ref_pin_to_net, ref_pin_to_func=ref_pin_to_func)


# ---------------------------------------------------------------------------
# Natural sort key for reference designators
# ---------------------------------------------------------------------------

def _ref_sort_key(ref: str) -> tuple:
    m = re.match(r'([A-Za-z_]+)(\d+)(.*)', ref)
    if m:
        return (m.group(1).upper(), int(m.group(2)), m.group(3))
    return (ref.upper(), 0, '')


# ---------------------------------------------------------------------------
# AI model generation via OpenRouter
# ---------------------------------------------------------------------------

def _try_import_pypdf():
    try:
        import pypdf
        return pypdf
    except ImportError:
        try:
            import PyPDF2 as pypdf
            return pypdf
        except ImportError:
            return None


def extract_pdf_text(path: str, max_chars: int = 10000) -> str:
    """Extract text from a PDF file. Returns empty string if unable to parse."""
    pypdf = _try_import_pypdf()
    if pypdf is None:
        print('NOTE: pypdf not installed; proceeding without datasheet text '
              '(pip install pypdf)', file=sys.stderr)
        return ''
    try:
        reader = pypdf.PdfReader(path)
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or '')
            except Exception:
                pass
            if sum(len(p) for p in parts) >= max_chars:
                break
        text = '\n'.join(parts)
        return text[:max_chars]
    except Exception as e:
        print(f'WARNING: could not read PDF {path}: {e}', file=sys.stderr)
        return ''


def find_datasheet(ic_value: str, datasheets_dir: str) -> 'str | None':
    """Search datasheets_dir for a file matching ic_value (case-insensitive)."""
    ds_path = Path(datasheets_dir)
    if not ds_path.is_dir():
        return None

    value_upper = ic_value.upper()

    # Collect candidates: PDFs and text files
    candidates = list(ds_path.glob('*.pdf')) + list(ds_path.glob('*.txt')) + \
                 list(ds_path.glob('*.PDF'))

    # 1. Exact stem match
    for f in candidates:
        if f.stem.upper() == value_upper:
            return str(f)

    # 2. Stem starts with value (e.g. "SN74AUC1G17_datasheet.pdf" for "SN74AUC1G17DCKR")
    for f in candidates:
        if value_upper.startswith(f.stem.upper()) or f.stem.upper().startswith(value_upper):
            return str(f)

    # 3. Try stripping common package suffixes from value and retry
    # e.g. "SN74AUC1G17DCKR" → "SN74AUC1G17" by removing trailing [A-Z]{1,4}\d?$
    base = re.sub(r'[A-Z]{1,4}\d?$', '', value_upper)
    if base and base != value_upper:
        for f in candidates:
            if f.stem.upper().startswith(base) or base.startswith(f.stem.upper()):
                return str(f)

    return None


def _get_ic_pins(ref: str, netlist: Netlist) -> 'list[tuple[str, str, str]]':
    """Return [(pin_num, portname, spice_net), ...] sorted by int(pin_num)."""
    connected = [
        (pin, netlist.ref_pin_to_func.get((ref, pin), f'P{pin}'),
         netlist.ref_pin_to_net[(ref, pin)].spice_name)
        for (r, pin) in netlist.ref_pin_to_net
        if r == ref
    ]
    try:
        connected.sort(key=lambda x: int(x[0]))
    except ValueError:
        connected.sort(key=lambda x: x[0])
    return connected


def call_openrouter(messages: list, api_key: str,
                    model: str = 'anthropic/claude-sonnet-4-6') -> str:
    """Call the OpenRouter chat completions API. Returns the assistant message text."""
    url = 'https://openrouter.ai/api/v1/chat/completions'
    payload = json.dumps({
        'model': model,
        'messages': messages,
        'max_tokens': 2048,
    }).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://github.com/kicad2spice',
            'X-Title': 'kicad2spice',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content']
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        sys.exit(f'ERROR: OpenRouter API error {e.code}: {body[:300]}')
    except Exception as e:
        sys.exit(f'ERROR: OpenRouter request failed: {e}')


def _sanitize_subckt_name(value: str) -> str:
    """Make a SPICE-safe subcircuit name from an IC part number."""
    name = re.sub(r'[^A-Za-z0-9_]', '_', value)
    name = re.sub(r'_+', '_', name).strip('_')
    if name and name[0].isdigit():
        name = 'U_' + name
    return name or 'IC'


def generate_model_for_ic(ref: str, comp: Component, netlist: Netlist,
                           datasheets_dir: str, api_key: str,
                           generated_dir: str) -> bool:
    """
    Generate a SPICE .subckt model for `comp` via OpenRouter.
    Saves the model to generated_dir/{subckt_name}.sub and updates comp in-place.
    Returns True on success.
    """
    subckt_name = _sanitize_subckt_name(comp.value)
    out_path = Path(generated_dir) / f'{subckt_name}.sub'

    # Gather pin info
    pin_info = _get_ic_pins(ref, netlist)
    if not pin_info:
        print(f'WARNING: {ref}: no connected pins found, cannot generate model',
              file=sys.stderr)
        return False

    port_names = [func for _, func, _ in pin_info]
    pin_nums   = [pin  for pin, _, _ in pin_info]
    port_list  = ' '.join(port_names)

    # Check cache
    if out_path.exists():
        print(f'  {ref}: using cached model {out_path}', file=sys.stderr)
        _apply_generated_model(comp, subckt_name, str(out_path), pin_nums, port_names)
        return True

    # Find and read datasheet
    ds_file = find_datasheet(comp.value, datasheets_dir)
    if ds_file:
        print(f'  {ref}: found datasheet {os.path.basename(ds_file)}', file=sys.stderr)
        suffix = Path(ds_file).suffix.lower()
        if suffix == '.pdf':
            ds_text = extract_pdf_text(ds_file)
        else:
            try:
                ds_text = Path(ds_file).read_text(encoding='utf-8', errors='replace')[:10000]
            except Exception:
                ds_text = ''
    else:
        print(f'  {ref}: no datasheet found for "{comp.value}", generating from part name only',
              file=sys.stderr)
        ds_text = ''

    datasheet_section = (
        f'Datasheet excerpt:\n"""\n{ds_text[:8000]}\n"""'
        if ds_text else
        '(No datasheet available — infer behavior from the part number.)'
    )

    pin_desc_lines = '\n'.join(
        f'  Pin {pin}: {func} → connected to net "{net}"'
        for pin, func, net in pin_info
    )

    system_prompt = (
        'You are an expert in SPICE and QSPICE circuit simulation. '
        'You generate accurate, compact behavioral SPICE subcircuit models. '
        'Respond with ONLY the .subckt block — no explanation, no markdown fences.'
    )

    user_prompt = f"""Generate a SPICE behavioral subcircuit model for the following IC.

IC Part Number: {comp.value}

{datasheet_section}

Pins connected in this circuit (pin number: function: net type):
{pin_desc_lines}

STRICT REQUIREMENTS:
1. Subcircuit name must be exactly: {subckt_name}
2. First line must be exactly: .subckt {subckt_name} {port_list}
3. Port order must be EXACTLY: {port_list}
4. Last line must be exactly: .ends {subckt_name}
5. Use standard SPICE syntax compatible with QSPICE/LTspice
6. Model the key electrical behavior (logic, regulation, amplification, etc.)
7. For digital ICs: use behavioral voltage sources (B-sources) or gate primitives
8. Keep the model concise — prefer behavioral over transistor-level

Return ONLY the .subckt ... .ends block.
"""

    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_prompt},
    ]

    print(f'  {ref}: calling OpenRouter for "{comp.value}" model...', file=sys.stderr)
    response = call_openrouter(messages, api_key)

    # Extract .subckt block from response (strip any accidental markdown)
    subckt_match = re.search(
        r'(\.subckt\b.+?\.ends\b[^\n]*)', response, re.DOTALL | re.IGNORECASE
    )
    if not subckt_match:
        print(f'WARNING: {ref}: OpenRouter response did not contain a valid '
              f'.subckt block, skipping', file=sys.stderr)
        print(f'  Response preview: {response[:200]}', file=sys.stderr)
        return False

    model_text = subckt_match.group(1).strip()

    # Save to file
    Path(generated_dir).mkdir(parents=True, exist_ok=True)
    header = (f'* Auto-generated by kicad2spice for {comp.value}\n'
              f'* Source IC: {ref} in {os.path.basename(netlist.source)}\n')
    out_path.write_text(header + model_text + '\n', encoding='utf-8')
    print(f'  {ref}: saved model → {out_path}', file=sys.stderr)

    _apply_generated_model(comp, subckt_name, str(out_path), pin_nums, port_names)
    return True


def _apply_generated_model(comp: Component, subckt_name: str, lib_path: str,
                            pin_nums: list, port_names: list) -> None:
    """Update a Component in-place to use a (newly generated or cached) model."""
    comp.sim_device = 'SUBCKT'
    comp.sim_name = subckt_name
    comp.sim_library = lib_path
    comp.pin_to_spice = {pin: name for pin, name in zip(pin_nums, port_names)}
    comp.spice_pin_order = list(port_names)


def run_model_generation(netlist: Netlist, datasheets_dir: str,
                          api_key: str, generated_dir: str) -> None:
    """
    Find all unmodeled ICs (U* without Sim.Device) and generate SPICE models
    for them. Models are grouped by part value so each unique IC is only
    generated once; all instances are updated.
    """
    # Group unmodeled IC refs by part value
    by_value: dict[str, list[str]] = {}
    for ref, comp in netlist.components.items():
        if comp.dnp:
            continue
        if _ref_prefix(ref) not in ('U',):
            continue
        if comp.sim_device is not None:
            continue
        by_value.setdefault(comp.value, []).append(ref)

    if not by_value:
        print('kicad2spice: no unmodeled ICs to process', file=sys.stderr)
        return

    total = len(by_value)
    print(f'kicad2spice: generating models for {total} unique IC(s)...', file=sys.stderr)

    for i, (value, refs) in enumerate(sorted(by_value.items()), 1):
        primary_ref = refs[0]
        primary_comp = netlist.components[primary_ref]
        print(f'[{i}/{total}] {value} (instances: {", ".join(refs)})', file=sys.stderr)

        ok = generate_model_for_ic(
            primary_ref, primary_comp, netlist,
            datasheets_dir, api_key, generated_dir,
        )

        if ok:
            # Apply the same model to all other instances of the same part
            for ref in refs[1:]:
                other = netlist.components[ref]
                _apply_generated_model(
                    other, primary_comp.sim_name, primary_comp.sim_library,
                    list(primary_comp.pin_to_spice.keys()),
                    list(primary_comp.spice_pin_order),
                )

        # Brief pause between API calls to be polite to the rate limiter
        if i < total:
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# SPICE generator
# ---------------------------------------------------------------------------

def _normalize_lib_path(raw: str, lib_path: 'str | None') -> str:
    normalized = raw.replace('\\', '/')
    if lib_path:
        basename = os.path.basename(normalized)
        return os.path.join(lib_path, basename).replace('\\', '/')
    return normalized


def generate_spice(netlist: Netlist, lib_path: 'str | None' = None) -> str:
    lines = []
    lines.append('* Generated by kicad2spice')
    lines.append(f'* Source: {os.path.basename(netlist.source)}')
    lines.append('')

    # Collect .include directives (unique, first-seen order)
    seen_libs: dict = {}
    for comp in netlist.components.values():
        if comp.sim_device == 'SUBCKT' and comp.sim_library:
            norm = _normalize_lib_path(comp.sim_library, lib_path)
            seen_libs[norm] = True

    if seen_libs:
        for lib in seen_libs:
            lines.append(f'.include "{lib}"')
        lines.append('')

    skipped = 0
    written_passive = 0
    written_subckt = 0

    for ref in sorted(netlist.components, key=_ref_sort_key):
        comp = netlist.components[ref]

        if comp.dnp:
            skipped += 1
            continue

        device = comp.sim_device

        if device is None:
            print(f'WARNING: {ref} ({comp.value}) has no Sim.Device and no '
                  f'inferred type, skipping', file=sys.stderr)
            skipped += 1
            continue

        if device in ('R', 'C', 'L'):
            n1 = netlist.ref_pin_to_net.get((ref, '1'))
            n2 = netlist.ref_pin_to_net.get((ref, '2'))
            if n1 is None or n2 is None:
                print(f'WARNING: {ref}: missing net connection (pin 1 or 2)',
                      file=sys.stderr)
                skipped += 1
                continue

            # Jumpers (JP*) and ferrite beads (FB*) use 1mΩ (active/closed)
            prefix = _ref_prefix(ref)
            if prefix in ('JP', 'FB'):
                value = '1m'
            else:
                value = comp.value

            lines.append(f'{ref} {n1.spice_name} {n2.spice_name} {value}')
            written_passive += 1

        elif device == 'SUBCKT':
            if not comp.sim_name:
                print(f'WARNING: {ref}: SUBCKT has no Sim.Name, skipping',
                      file=sys.stderr)
                skipped += 1
                continue
            if not comp.spice_pin_order:
                print(f'WARNING: {ref}: SUBCKT has no Sim.Pins, skipping',
                      file=sys.stderr)
                skipped += 1
                continue

            spice_to_kicad = {v: k for k, v in comp.pin_to_spice.items()}
            node_names = []
            ok = True
            for spice_pin in comp.spice_pin_order:
                kicad_pin = spice_to_kicad.get(spice_pin)
                if kicad_pin is None:
                    print(f'WARNING: {ref}: SPICE pin "{spice_pin}" not in Sim.Pins map',
                          file=sys.stderr)
                    node_names.append('?')
                    ok = False
                    continue
                net = netlist.ref_pin_to_net.get((ref, kicad_pin))
                if net is None:
                    print(f'WARNING: {ref} pin {kicad_pin} ({spice_pin}) not connected',
                          file=sys.stderr)
                    node_names.append('NC')
                else:
                    node_names.append(net.spice_name)

            lines.append(f'X{ref} {" ".join(node_names)} {comp.sim_name}')
            if ok:
                written_subckt += 1
            else:
                skipped += 1

        else:
            print(f'WARNING: {ref}: unsupported Sim.Device "{device}", skipping',
                  file=sys.stderr)
            skipped += 1

    lines.append('')
    lines.append('.end')

    total = written_passive + written_subckt
    print(f'kicad2spice: wrote {total} elements '
          f'(R/C/L: {written_passive}, SUBCKT: {written_subckt}), '
          f'skipped {skipped}', file=sys.stderr)

    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_dotenv(env_file: str = '.env') -> None:
    """Load KEY=VALUE pairs from `env_file` into os.environ (if not already set).
    Ignores blank lines and lines starting with #.
    """
    path = Path(env_file)
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main():
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description='Convert KiCad .net netlist to SPICE .cir format'
    )
    parser.add_argument('input', help='KiCad netlist file (.net)')
    parser.add_argument('-o', '--output', help='Output .cir file (default: stdout)')
    parser.add_argument('--lib-path',
                        help='Override base directory for .sub library files')
    parser.add_argument('--generate-models', action='store_true',
                        help='Generate SPICE models for unmodelled ICs via Claude '
                             'on OpenRouter (requires OPENROUTER_API_KEY env var)')
    parser.add_argument('--datasheets-dir', default=None,
                        help='Directory containing IC datasheets (default: '
                             'datasheets/ next to the input .net file)')
    parser.add_argument('--generated-dir', default=None,
                        help='Where to save AI-generated .sub models (default: '
                             'models/ next to the input .net file)')
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f'ERROR: file not found: {args.input}')

    input_dir = os.path.dirname(os.path.abspath(args.input))

    datasheets_dir = args.datasheets_dir or os.path.join(input_dir, 'datasheets')
    generated_dir = args.generated_dir or os.path.join(input_dir, 'models')

    netlist = parse_netlist(args.input)

    if args.generate_models:
        api_key = os.environ.get('OPENROUTER_API_KEY', '')
        if not api_key:
            sys.exit('ERROR: --generate-models requires OPENROUTER_API_KEY env var to be set')
        run_model_generation(netlist, datasheets_dir, api_key, generated_dir)

    spice_text = generate_spice(netlist, lib_path=args.lib_path)

    if args.output:
        Path(args.output).write_text(spice_text, encoding='utf-8')
        print(f'kicad2spice: output written to {args.output}', file=sys.stderr)
    else:
        sys.stdout.write(spice_text)


if __name__ == '__main__':
    main()
