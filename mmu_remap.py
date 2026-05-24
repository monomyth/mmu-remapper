#!/usr/bin/env python3
"""
mmu-remapper
Safe remapping of extruder/filament assignments inside PrusaSlicer
multi-material painted 3MF files.

See README.md and the approved implementation plan for full context.
"""

import argparse
import sys
import tempfile
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
import xml.etree.ElementTree as ET

# Import the proven codec (pure, well-tested)
import codec as mmu_codec

# Known namespace used by PrusaSlicer for painting / custom data
SLICERPE_NS = "http://schemas.prusa3d.com/slic3r/"
# Common prefixes seen in the wild
SLICERPE_ATTR = "slic3rpe:mmu_segmentation"

# Files we care about inside the 3MF
MODEL_FILE = "3D/3dmodel.model"
MODEL_CONFIG = "Metadata/Slic3r_PE_model.config"
MODEL_SETTINGS_CONFIG = "Metadata/model_settings.config"  # Used by some Prusa/Orca/Bambu-style projects
PRINT_CONFIG = "Metadata/Slic3r_PE.config"


def is_3mf(path: Path) -> bool:
    """Quick heuristic: 3MF is a ZIP with a [Content_Types].xml at root."""
    try:
        with zipfile.ZipFile(path, 'r') as z:
            return "[Content_Types].xml" in z.namelist()
    except Exception:
        return False


def list_3mf_contents(path: Path) -> List[str]:
    """Return the list of files inside the 3MF (ZIP)."""
    with zipfile.ZipFile(path, 'r') as z:
        return z.namelist()


def safe_extract_to_temp(src_3mf: Path, dest_dir: Path) -> None:
    """Extract the entire 3MF safely into dest_dir (overwrites cleanly)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Clean previous contents if any
    for p in dest_dir.iterdir():
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)

    with zipfile.ZipFile(src_3mf, 'r') as z:
        z.extractall(dest_dir)


def repack_3mf(src_dir: Path, out_3mf: Path) -> None:
    """Repack the directory tree into a new .3mf (ZIP) with reasonable compression."""
    out_3mf.parent.mkdir(parents=True, exist_ok=True)
    # Use ZIP_DEFLATED for compatibility; 3MF spec allows it
    with zipfile.ZipFile(out_3mf, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for file in src_dir.rglob("*"):
            if file.is_file():
                arcname = file.relative_to(src_dir)
                z.write(file, arcname)


def register_namespaces() -> None:
    """Register the slic3rpe namespace so ElementTree round-trips it nicely."""
    try:
        ET.register_namespace("slic3rpe", SLICERPE_NS)
        # Also try the common empty-prefix case many 3MFs use
        ET.register_namespace("", "http://schemas.microsoft.com/3dmanufacturing/core/2015/02")
    except Exception:
        pass


def find_mmu_segmentation_values(xml_path: Path) -> List[Tuple[str, str]]:
    """
    Scan the 3dmodel.model and return list of (element_tag, mmu_segmentation_value)
    for every triangle that has the attribute.
    This is a diagnostic / discovery helper.
    """
    values: List[Tuple[str, str]] = []
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        print(f"Warning: failed to parse {xml_path}: {e}")
        return values

    # Walk all elements; look for the attribute in any namespace form
    for elem in root.iter():
        # Check both namespaced and raw attribute forms
        for attr in (f"{{{SLICERPE_NS}}}mmu_segmentation", "slic3rpe:mmu_segmentation", SLICERPE_ATTR):
            if attr in elem.attrib:
                values.append((elem.tag, elem.attrib[attr]))
                break
        # Also check for plain "mmu_segmentation" just in case
        if "mmu_segmentation" in elem.attrib and not any(v[1] for v in values[-1:]):
            values.append((elem.tag, elem.attrib["mmu_segmentation"]))

    return values


def inspect_extruder_assignments(model_path: Path, config_path: Path, extracted_dir: Path) -> None:
    """
    Thorough diagnostic for --inspect.
    Looks in multiple places for how extruders are assigned.
    """
    print("\n--- Detailed extruder / color data inspection ---")

    found_any = False

    # 1. Triangle-level MMU painting
    if model_path.exists():
        seg_values = find_mmu_segmentation_values(model_path)
        if seg_values:
            found_any = True
            print(f"  Found {len(seg_values)} triangles with slic3rpe:mmu_segmentation (classic MMU painting).")
            counts = _pretty_counts_from_raw([v for _, v in seg_values])
            if counts:
                print(f"  Painted extruders (via segmentation): {sorted(counts.keys())}")
        else:
            print("  No slic3rpe:mmu_segmentation on triangles (no brush/bucket painting data).")

    # 2. Look in known model config files (both classic and newer model_settings.config)
    candidate_configs = [config_path]
    if extracted_dir:
        candidate_configs.append(extracted_dir / MODEL_SETTINGS_CONFIG)

    for cpath in candidate_configs:
        if not cpath or not cpath.exists():
            continue
        try:
            tree = ET.parse(cpath)
            root = tree.getroot()

            extruder_entries = []
            for elem in root.iter():
                if elem.tag.endswith("metadata") or elem.tag == "metadata":
                    key = (elem.get("key") or "").lower()
                    if "extruder" in key:
                        val = elem.get("value")
                        parent_type = elem.get("type") or "unknown"
                        extruder_entries.append((parent_type, val))

            if extruder_entries:
                found_any = True
                fname = cpath.name
                print(f"  Found {len(extruder_entries)} extruder metadata entries in {fname}:")
                for ptype, val in extruder_entries[:15]:
                    print(f"    - type={ptype}, value={val}")
                if len(extruder_entries) > 15:
                    print(f"    ... ({len(extruder_entries) - 15} more)")
        except Exception as e:
            print(f"  Error parsing {cpath.name}: {e}")

    # 3. Broad search inside 3dmodel.model for any extruder references
    if model_path.exists():
        try:
            tree = ET.parse(model_path)
            root = tree.getroot()
            extruder_refs = []
            for elem in root.iter():
                for attr_name, attr_val in elem.attrib.items():
                    if "extruder" in attr_name.lower():
                        extruder_refs.append((elem.tag, attr_name, attr_val))
                # Also check child metadata elements
                if "extruder" in (elem.get("key") or "").lower() or "extruder" in elem.tag.lower():
                    extruder_refs.append((elem.tag, elem.get("key"), elem.text or elem.get("value")))

            if extruder_refs:
                found_any = True
                print("  Extruder references found directly in 3dmodel.model:")
                for tag, key, val in extruder_refs[:10]:  # limit spam
                    print(f"    - <{tag}> key={key} value={val}")
        except Exception:
            pass

    # 4. Last resort: grep all XML files for "extruder"
    if extracted_dir.exists():
        import glob
        xml_files = glob.glob(str(extracted_dir / "**" / "*.xml"), recursive=True)
        mentions = []
        for xf in xml_files:
            try:
                content = Path(xf).read_text(errors="ignore")
                if "extruder" in content.lower():
                    # Find context
                    for line in content.splitlines():
                        if "extruder" in line.lower() and len(mentions) < 15:
                            mentions.append((Path(xf).name, line.strip()[:120]))
            except Exception:
                pass

        if mentions:
            found_any = True
            print("  'extruder' mentions found in other XML files (showing up to 8):")
            for fname, snippet in mentions[:8]:
                print(f"    [{fname}] {snippet}")

    if not found_any:
        print("  No obvious extruder assignments found in standard locations.")
        print("    The coloring you see in PrusaSlicer might be coming from a different mechanism")
        print("    (e.g. per-mesh color in the 3MF, or only in the main Slic3r_PE.config, or embedded differently).")

    print("--- End of detailed inspection ---\n")


# The old rough counter is superseded by mmu_codec.detect_used_extruders + tokenize.
# We keep a tiny wrapper for the --inspect pretty-printer.
def _pretty_counts_from_raw(values: List[str]) -> Dict[int, int]:
    """Best-effort count for the diagnostic --inspect path (uses the real codec)."""
    counts: Dict[int, int] = {}
    for v in values:
        for ex, _tok in mmu_codec.tokenize_segmentation(v):
            counts[ex] = counts.get(ex, 0) + 1
    return counts


# ---------------------------------------------------------------------
# Real remapping pipeline (wired in this step)
# ---------------------------------------------------------------------

def _edit_model_file(model_path: Path, mapping: Dict[int, int]) -> Tuple[int, int]:
    """
    Edit 3D/3dmodel.model in place.
    Returns (num_triangle_attrs_changed, num_values_changed).
    """
    if not model_path.exists():
        return 0, 0

    try:
        tree = ET.parse(model_path)
        root = tree.getroot()
    except Exception as e:
        print(f"  ERROR parsing model XML: {e}")
        return 0, 0

    changed_attrs = 0
    changed_values = 0

    for elem in root.iter():
        # Look for the attribute in the forms we see in real files
        attr_names = [
            f"{{{SLICERPE_NS}}}mmu_segmentation",
            "slic3rpe:mmu_segmentation",
            "mmu_segmentation",
        ]
        for an in attr_names:
            if an in elem.attrib:
                old_val = elem.attrib[an]
                new_val = mmu_codec.remap_segmentation(old_val, mapping)
                if new_val is not None and new_val != old_val:
                    elem.attrib[an] = new_val
                    changed_attrs += 1
                    changed_values += 1
                elif new_val is None and old_val:
                    # Opaque complex value — we leave it (documented limitation)
                    # A future improvement could do a conservative token substitution
                    pass
                break

    if changed_attrs > 0:
        tree.write(model_path, encoding="UTF-8", xml_declaration=True)

    return changed_attrs, changed_values


def _edit_model_config(config_path: Path, mapping: Dict[int, int]) -> int:
    """
    Edit Metadata/Slic3r_PE_model.config for object/volume extruder assignments.
    More tolerant matching than before.
    """
    if not config_path.exists():
        return 0

    try:
        tree = ET.parse(config_path)
        root = tree.getroot()
    except Exception:
        return 0

    changed = 0
    for elem in root.iter():
        # Look for any metadata-like element that mentions extruder
        tag = elem.tag.lower() if elem.tag else ""
        key = (elem.get("key") or "").lower()
        if "metadata" in tag or "metadata" in key:
            if "extruder" in key:
                try:
                    old = int(elem.get("value", "0"))
                    if old in mapping:
                        new_val = mapping[old]
                        if new_val != old:
                            elem.set("value", str(new_val))
                            changed += 1
                except (ValueError, TypeError):
                    pass

    if changed > 0:
        tree.write(config_path, encoding="UTF-8", xml_declaration=True)
    return changed


def _edit_model_object_extruders(model_path: Path, mapping: Dict[int, int]) -> int:
    """
    Also look inside 3dmodel.model for object/volume level extruder metadata.
    Some 3MFs store the assignments here instead of (or in addition to) the config file.
    """
    if not model_path.exists():
        return 0

    try:
        tree = ET.parse(model_path)
        root = tree.getroot()
    except Exception:
        return 0

    changed = 0
    for elem in root.iter():
        # Check attributes
        for attr_name in list(elem.attrib.keys()):
            if "extruder" in attr_name.lower():
                try:
                    old = int(elem.attrib[attr_name])
                    if old in mapping:
                        new_val = mapping[old]
                        if new_val != old:
                            elem.attrib[attr_name] = str(new_val)
                            changed += 1
                except ValueError:
                    pass

        # Check child metadata elements (common pattern)
        if "metadata" in elem.tag.lower():
            key = (elem.get("key") or elem.get("name") or "").lower()
            if "extruder" in key:
                try:
                    old = int(elem.get("value") or elem.text or "0")
                    if old in mapping:
                        new_val = mapping[old]
                        if new_val != old:
                            if "value" in elem.attrib:
                                elem.set("value", str(new_val))
                            else:
                                elem.text = str(new_val)
                            changed += 1
                except ValueError:
                    pass

    if changed > 0:
        tree.write(model_path, encoding="UTF-8", xml_declaration=True)
    return changed


def perform_remap(
    input_3mf: Path,
    output_3mf: Path,
    mapping: Dict[int, int],
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Full end-to-end remap. Returns a stats dict.
    """
    stats: Dict[str, int] = {
        "triangles_changed": 0,
        "values_changed": 0,
        "config_entries_changed": 0,
        "used_extruders_before": 0,
    }

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        safe_extract_to_temp(input_3mf, tdir)

        model = tdir / MODEL_FILE
        cfg = tdir / MODEL_CONFIG
        model_settings_cfg = tdir / MODEL_SETTINGS_CONFIG

        # Discover before stats
        if model.exists():
            seg_vals = [v for _, v in find_mmu_segmentation_values(model)]
            used = mmu_codec.detect_used_extruders(seg_vals)
            stats["used_extruders_before"] = len(used)
            print(f"  painting data references extruders: {sorted(used) or 'none'}")

        # Apply edits (painting + object/volume level)
        t, v = _edit_model_file(model, mapping)
        stats["triangles_changed"] = t
        stats["values_changed"] = v

        c1 = _edit_model_config(cfg, mapping)
        c2 = _edit_model_config(model_settings_cfg, mapping) if model_settings_cfg.exists() else 0
        m = _edit_model_object_extruders(model, mapping)

        total_config = c1 + c2 + m
        stats["config_entries_changed"] = total_config

        print(f"  model triangles updated: {t} (values rewritten: {v})")
        print(f"  object/volume extruder entries updated: {total_config}")

        if dry_run:
            print("  (dry-run: not writing output file)")
            return stats

        repack_3mf(tdir, output_3mf)
        print(f"  wrote: {output_3mf}")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remap extruder assignments in PrusaSlicer painted 3MF files."
    )
    parser.add_argument("input", type=Path, help="Input .3mf file")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Output .3mf (defaults to <input>.remapped.3mf)")
    parser.add_argument("--map", "--remap", dest="map", action="append", default=[],
                        help="Extruder remapping (repeatable). Examples: --map 2:4 --map 4:1  or  --map '2:4,4:1,1:3'  or  --map '2:4 4:1'")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only show what would change; do not write output")
    parser.add_argument("--inspect", action="store_true",
                        help="Diagnostic mode: deeply inspect how extruders/colors are stored (painting vs object/volume assignment)")
    args = parser.parse_args()

    inp: Path = args.input
    if not inp.exists():
        print(f"Error: {inp} does not exist", file=sys.stderr)
        return 1
    if not is_3mf(inp):
        print(f"Error: {inp} does not look like a valid 3MF (ZIP with [Content_Types].xml)", file=sys.stderr)
        return 1

    if args.output is None:
        args.output = inp.with_suffix("").with_name(inp.stem + ".remapped.3mf")

    register_namespaces()

    print(f"mmu-remapper: processing {inp}")

    if args.inspect or not args.map:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            safe_extract_to_temp(inp, tdir)
            model = tdir / MODEL_FILE
            if model.exists():
                seg_values = [v for _, v in find_mmu_segmentation_values(model)]
                print(f"  mmu_segmentation attributes found: {len(seg_values)}")
                counts = _pretty_counts_from_raw(seg_values)
                if counts:
                    print("  detected extruder usage (via codec):")
                    for ex in sorted(counts):
                        print(f"    extruder {ex}: ~{counts[ex]} occurrences")
                else:
                    print("  no recognizable mmu_segmentation codes (or no painting data)")
            else:
                print(f"  {MODEL_FILE} not present — may not contain Prusa MMU painting")

            cfg = tdir / MODEL_CONFIG
            if cfg.exists():
                print(f"  {MODEL_CONFIG} present")

            # New richer diagnostics
            inspect_extruder_assignments(model, cfg, tdir)

        if args.inspect:
            return 0

    if not args.map:
        print("\nNo --map given. Nothing to remap. Use --inspect or --help.")
        return 0

    # Build and validate the mapping using the codec helper
    try:
        mapping = mmu_codec.build_mapping_from_strings(args.map)
    except Exception as e:
        print(f"Error in --map: {e}", file=sys.stderr)
        return 1

    print(f"  applying mapping: {mapping}")

    if args.dry_run:
        print("  (dry-run mode)")

    stats = perform_remap(inp, args.output, mapping, dry_run=args.dry_run)

    print("\nSummary:")
    print(f"  extruders referenced before: {stats.get('used_extruders_before', '?')}")
    print(f"  triangle attributes rewritten: {stats['triangles_changed']}")
    print(f"  individual segmentation values rewritten: {stats['values_changed']}")
    print(f"  object config extruder entries updated: {stats['config_entries_changed']}")

    if not args.dry_run:
        print(f"\nSuccess. Load {args.output} in PrusaSlicer.")
        print("Activate the 'Multimaterial painting' gizmo to verify the colors moved as expected.")
    else:
        print("\nDry-run complete — no output file was written.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
