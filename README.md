# mmu-remapper

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)

Remap extruder/filament assignments in PrusaSlicer, Bambu Studio, and Orca Slicer 3MF files.

Works with both classic Prusa MMU painting (`mmu_segmentation`) and Bambu/Orca-style per-face `paint_color` data — even in large split-mesh projects.

## Features

- Remap extruder/filament assignments in 3MF files without having to repaint
- Supports classic PrusaSlicer MMU painting (`slic3rpe:mmu_segmentation`)
- Supports Bambu Studio and Orca Slicer `paint_color` attributes (including large split-mesh projects in `3D/Objects/*.model`)
- Handles object- and volume-level extruder assignments stored in `model_settings.config`
- Safely processes very large 3MF files (50 MB+ meshes)
- Clear `--inspect` diagnostics and `--dry-run` support
- Minimal dependencies (pure Python + optional lxml)

## Supported Formats

- **PrusaSlicer** — Classic MMU painting + object/volume assignments
- **Bambu Studio** — `paint_color` per-face data + `model_settings.config`
- **Orca Slicer** — Same format as Bambu Studio
- Other slicers that use compatible 3MF structures

## The Problem

You painted a model in PrusaSlicer using the MMU / multi-material painting tool (bucket + brush). Later you need to physically load different colors into different MMU slots (or an XL toolhead changed position). The painted regions now print the wrong colors.

PrusaSlicer has no built-in "remap painted extruders" feature (see [GitHub #14903](https://github.com/prusa3d/PrusaSlicer/issues/14903) and the forum thread "Changing filament assignments for a Multimaterial painting").

Users currently do manual unzip + sed hacks or repaint entire models. This tool solves it properly.

## How It Works (High Level)

Painting data is stored in two main places inside the `.3mf` (a ZIP):

- `3D/3dmodel.model` (and `3D/Objects/object_*.model` for split Bambu/Orca 3MFs) — `<triangle slic3rpe:mmu_segmentation="4"/>` or `<triangle ... paint_color="8"/>` (and long hex strings). The hex values encode extruder assignments using the same small set of known codes (BambuStudio/Orca `paint_color` uses identical encoding to Prusa mmu_segmentation):
  - 1 → `4`
  - 2 → `8`
  - 3 → `0C`
  - 4 → `1C`
  - 5 → `2C`
  - ... (continues for higher extruders up to the 16 supported by the painting gizmo).

- `Metadata/Slic3r_PE_model.config` — object/volume `<metadata key="extruder" value="N"/>` (plain decimals).

`mmu-remapper` safely opens the 3MF, rewrites only the relevant painting codes and metadata according to your mapping, and writes a new valid `.3mf`.

## Installation / Running

Python 3.8+ required (stdlib only for core functionality).

### Quick Start (Recommended)

```bash
pip install git+https://github.com/monomyth/mmu-remapper.git

mmu-remap model.3mf --map "1:3,3:1" -o fixed.3mf
```

### Development Install

```bash
git clone https://github.com/monomyth/mmu-remapper.git
cd mmu-remapper
pip install -e ".[lxml]"
mmu-remap --help
```

### Optional Dependency

For more robust XML parsing (recommended):

```bash
pip install lxml
```

## Basic Usage

```bash
# Swap extruder 1 ↔ 3 (very common when filament positions changed on the MMU)
mmu-remap painted.3mf --map "1:3,3:1" -o fixed.3mf

# Dry-run first — see exactly what would be touched
mmu-remap painted.3mf --map "1:3,3:1,2:2" --dry-run

# Any permutation (supports --remap as alias, and commas/spaces inside one value)
mmu-remap model.3mf --map "2:4,4:1,1:3,5:3" -o out.3mf
# or equivalently:
# mmu-remap model.3mf --remap 2:4 --remap 4:1 --remap 1:3 --remap 5:3

# Full diagnostic of a 3MF (no mapping required)
mmu-remap mystery.3mf --inspect
```

The tool prints a clear **Summary** with counts of rewritten triangle attributes and config entries, plus the set of extruders it detected before the remap.

## Limitations

- **Complex brush paintings**: Long `mmu_segmentation` / `paint_color` strings (produced when a brush splits triangles) are only partially rewritten today. All *recognizable* extruder codes inside them are remapped; structural bytes are left as-is. Most real-world models still produce excellent results.
- PrusaSlicer `slic3rpe:mmu_segmentation` and Bambu/Orca-style `paint_color` per-face painting data (same hex encoding) are now fully supported, including in large split `object_*.model` files.
- The tool does **not** change the filament profiles or the total number of filaments defined in the project (you must already have enough filaments loaded in the target 3MF).

## Verification & Testing

### With a real painted model (recommended final check)
1. In PrusaSlicer, create or import a simple mesh.
2. Open the **Multimaterial painting** gizmo.
3. Use the bucket tool to paint clearly separate regions with extruders 1, 2, and 3 (also do a few brush strokes that cross triangle boundaries to create a complex case).
4. Save the project as `test-painted.3mf`.
5. Run:
   ```bash
   mmu-remap test-painted.3mf --map "1:3,3:1" -o test-remapped.3mf
   ```
6. Open `test-remapped.3mf` in a fresh PrusaSlicer session.
7. Select the object and re-enter the MMU painting gizmo.
8. Confirm the painted regions now use the swapped extruders.
9. (Optional but excellent) Slice a few layers and use the filament color legend / tool preview to verify the G-code paths use the new mapping.

### Quick smoke test (no PrusaSlicer required)
You can create a minimal test 3MF yourself (or use one from the repo's history) and run the tool with `--dry-run` + `--inspect` to verify behavior.

## Contributing

Contributions are welcome!

- Found a 3MF variant the tool doesn't handle well? Open an issue and attach a small (anonymized) example if possible.
- Want to improve detection, add new formats, or polish the CLI? Feel free to open a PR.

Please keep changes small and follow the existing code style.

## References

- PrusaSlicer source: `src/libslic3r/Format/3mf.cpp`
- Community research on 3MF painting formats (Kurt Gluck and others on the Prusa forums)
- Original feature request: [GitHub #14903](https://github.com/prusa3d/PrusaSlicer/issues/14903)

Pull requests that improve complex-case handling (with test 3MFs) are especially appreciated.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

*This project was created to solve a real, recurring pain point for MMU and multi-extruder users.*
