# kicad2spice

Converts KiCad netlists (`.net` version E) to SPICE/QSPICE simulation files (`.cir`).

Optionally generates behavioral SPICE subcircuit models for unmodelled ICs using Claude on OpenRouter, reading datasheets from a local `datasheets/` directory.

## Requirements

- Python 3.10+
- `pypdf` for reading PDF datasheets (optional but recommended): `pip install pypdf`
- An [OpenRouter](https://openrouter.ai) API key for AI model generation (optional)

## Setup

```bash
git clone https://github.com/darylwbarry/kicad2spice.git
cd kicad2spice
python3 -m venv venv
source venv/bin/activate
pip install pypdf
```

For AI model generation, copy `.env.example` to `.env` and add your key:

```bash
cp .env.example .env
# edit .env and set OPENROUTER_API_KEY=your_key_here
```

## Usage

```bash
# Basic conversion
python3 kicad2spice.py input.net -o output.cir

# With AI model generation for unmodelled ICs
python3 kicad2spice.py input.net --generate-models -o output.cir

# Override library path in .include directives
python3 kicad2spice.py input.net -o output.cir --lib-path /path/to/spice/libs
```

## Datasheets

Place IC datasheet PDFs in a `datasheets/` directory next to the `.net` file. When `--generate-models` is used, the tool searches for a file matching the IC part number (case-insensitive, handles package suffix variants):

```
datasheets/
  SN74AUC1G17.pdf
  ADP7157.pdf
  ...
```

Generated models are saved to `models/` and cached — re-runs skip ICs that already have a model there.

## How it works

### Component handling

| Ref prefix | Treatment |
|---|---|
| `R`, `C`, `L` | Passive element using KiCad `value` field |
| `U` with `Sim.Library` | SUBCKT instance with `.include` |
| `U` without model | Skipped (or AI-generated with `--generate-models`) |
| `JP` (jumpers) | 1 mΩ resistor — active/closed state |
| `FB` (ferrite beads) | 1 mΩ resistor — DC approximation |
| `J`, `TP` | Skipped with warning |

Components annotated with KiCad's `Sim.Device`, `Sim.Pins`, `Sim.Library`, and `Sim.Name` fields are used directly. For passives without those fields, the type is inferred from the reference designator prefix.

### Net names

KiCad net names are sanitized for SPICE:
- `GND` → `0`
- `/SheetName/signal` → `signal`
- `Net-(C101-Pad1)` → `C101_Pad1`

### AI model generation

When `--generate-models` is enabled, the tool finds all `U*` components without a SPICE model, groups them by part number, and calls Claude (`anthropic/claude-sonnet-4-6`) via OpenRouter once per unique part. If a matching datasheet PDF is found in `datasheets/`, its text is included in the prompt.

Generated `.subckt` files are saved to `models/` and reused on subsequent runs without additional API calls.
