# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Intent

This project is a tool to convert KiCad netlists (`.net` format) to SPICE simulation input files. The sample netlist `TestStructures.net` is the primary test input — it was exported from KiCad Eeschema 9.0.7 and represents a multi-sheet PCB design for ringing/signal-integrity test structures.

## Usage

```bash
python3 kicad2spice.py input.net [-o output.cir] [--lib-path /path/to/spice/libs]
```

- Omit `-o` to print to stdout.
- `--lib-path` overrides the base directory for `.sub` library file paths (replaces the path prefix, keeps the filename).
- Warnings and the summary line go to stderr.

## KiCad Netlist Format (version "E")

The input format is a KiCad S-expression netlist with four top-level sections:

1. **`(design ...)`** — Metadata: source schematic path, export date, tool version, and hierarchical sheet definitions (each with a number, name, and timestamp path like `/SheetName/`).

2. **`(components ...)`** — One `(comp ...)` entry per schematic symbol, containing:
   - `(ref "...")` — Reference designator (e.g., `C101`, `R203`, `U604`)
   - `(value "...")` — Component value string
   - `(footprint "...")` — KiCad footprint library reference
   - `(datasheet "...")` — Datasheet URL or `~`
   - `(fields ...)` — Arbitrary key-value properties (manufacturer, MPN, SPICE model, etc.)
   - `(libsource ...)` — Symbol library and entry name
   - `(property ...)` — Additional schematic properties
   - `(sheetpath ...)` — Which hierarchical sheet this component lives in

3. **`(libparts ...)`** — Symbol library definitions (pins with names and electrical types).

4. **`(nets ...)`** — One `(net ...)` entry per electrical net, containing:
   - `(code "...")` — Integer net index
   - `(name "...")` — Net name (e.g., `Vcc5p0`, `GND`, `Net-(U604-A)`)
   - `(class "Default")`
   - `(node ...)` entries: each has `(ref ...)`, `(pin ...)`, optional `(pinfunction ...)`, and `(pintype ...)`

## TestStructures.net Overview

- **446 components** across 3 hierarchical sheets: `/` (root), `/BypassNetworkTest/`, `/LoopTest/`
- **64 nets** including power rails (`Vcc5p0`, `Vcc3p0_reg`, `Vcc2p0_reg`), `GND`, and auto-named signal nets
- Component types: capacitors (C), resistors (R), ICs (U), connectors (J), ferrite beads (FB), jumpers (JP), test points (TP)
- SPICE-relevant fields on components may include model references in `(fields ...)` entries
