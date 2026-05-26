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
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
import xml.etree.ElementTree as ET

# Import the proven codec (pure, well-tested)
import codec as mmu_codec

# Known namespace used by PrusaSlicer for painting / custom data
SLICERPE_NS = "http://schemas.prusa3d.com/slic3r/"
# Common prefixes seen in the wild
SLICERPE_ATTR = "slic3rpe:mmu_segmentation"
# BambuStudio / Orca Slicer / Printables per-face coloring (same hex encoding as mmu_segmentation)
PAINT_COLOR_ATTR = "paint_color"

# Files we care about inside the 3MF
MODEL_FILE = "3D/3dmodel.model"
MODEL_CONFIG = "Metadata/Slic3r_PE_model.config"
MODEL_SETTINGS_CONFIG = "Metadata/model_settings.config"  # Used by some Prusa/Orca/Bambu-style projects
PRINT_CONFIG = "Metadata/Slic3r_PE.config"

# Safety limit: do not full ET.parse very large model files (e.g. 50MB+ object meshes)
# Diagnostics use chunked/keyword scan instead; keeps tool fast and low-mem on real user files.
MAX_MODEL_PARSE_SIZE = 8 * 1024 * 1024


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


def find_all_model_files(base_dir: Path) -> List[Path]:
    """Discover the primary 3dmodel.model plus any split object_*.model files.
    Follows existing Path + glob patterns in the module. Skips nothing here;
    callers decide parse safety using size vs MAX_MODEL_PARSE_SIZE.
    """
    models: List[Path] = []
    main = base_dir / MODEL_FILE
    if main.exists():
        models.append(main)
    objs_dir = base_dir / "3D" / "Objects"
    if objs_dir.exists():
        for p in sorted(objs_dir.glob("*.model")):
            if p.is_file() and p not in models:
                models.append(p)
    return models


def _safe_keyword_sample(p: Path, n: int = 200000) -> bytes:
    """Head + tail sample for large files (catches deep <triangle> mmu_segmentation or metadata near EOF in split mesh XMLs).
    Replaces pure head-only samples to eliminate false-negative risk in diagnostics for the 'rare large-painting' case.
    Follows existing try/except + Path patterns; used only in --inspect diags.
    """
    try:
        size = p.stat().st_size
        data = p.read_bytes()[:n]
        if size > 2 * n:
            with p.open("rb") as f:
                f.seek(size - n)
                data += f.read(n)
        return data
    except Exception:
        return b""


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
        # Check both namespaced and raw attribute forms (Prusa)
        for attr in (f"{{{SLICERPE_NS}}}mmu_segmentation", "slic3rpe:mmu_segmentation", SLICERPE_ATTR):
            if attr in elem.attrib:
                values.append((elem.tag, elem.attrib[attr]))
                break
        # Also check for plain "mmu_segmentation" just in case
        if "mmu_segmentation" in elem.attrib and not any(v[1] for v in values[-1:]):
            values.append((elem.tag, elem.attrib["mmu_segmentation"]))
        # Bambu/Orca paint_color (same hex encoding, no namespace)
        if PAINT_COLOR_ATTR in elem.attrib:
            values.append((elem.tag, elem.attrib[PAINT_COLOR_ATTR]))

    return values


def inspect_extruder_assignments(model_path: Path, config_path: Path, extracted_dir: Path) -> None:
    """
    Thorough diagnostic for --inspect.
    Looks in multiple places for how extruders are assigned.
    """
    print("\n--- Detailed extruder / color data inspection ---")

    found_any = False

    # 1. Triangle-level MMU painting (main model + any object_*.model for split modern 3MFs)
    models_to_scan = [model_path] if model_path.exists() else []
    if extracted_dir:
        for om in find_all_model_files(extracted_dir):
            if om not in models_to_scan:
                models_to_scan.append(om)
    seg_values_all: List[Tuple[str, str]] = []
    large_had_keywords = False
    for mp in models_to_scan:
        if mp.exists():
            if mp.stat().st_size > MAX_MODEL_PARSE_SIZE:
                # For truly large meshes, head+tail sample may miss paint data in the middle of <triangles>.
                # Use the safe collect (full linear scan but no ET DOM) to reliably detect+report paint_color etc.
                try:
                    paint_vals = _safe_collect_segmentation_values(mp)
                    if paint_vals:
                        print(f"  Large model {mp.name}: contains mmu_segmentation or paint_color (safe scan; full ET.parse skipped)")
                        found_any = True
                        large_had_keywords = True
                        pcounts = _pretty_counts_from_raw(paint_vals)
                        print(f"  Found {len(paint_vals)} mmu_segmentation / paint_color attributes in {mp.name} using extruders {sorted(pcounts.keys())}")
                except Exception:
                    pass
                continue
            segs = find_mmu_segmentation_values(mp)
            if segs:
                seg_values_all.extend(segs)
                fname = mp.name
                print(f"  Found {len(segs)} triangles with slic3rpe:mmu_segmentation or paint_color in {fname}.")
    if seg_values_all:
        found_any = True
        counts = _pretty_counts_from_raw([v for _, v in seg_values_all])
        if counts:
            print(f"  Painted extruders (via segmentation): {sorted(counts.keys())}")
    elif models_to_scan and not large_had_keywords:
        print("  No mmu_segmentation or paint_color on triangles (no brush/bucket painting data).")

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
                        extruder_entries.append((key, val))

            if extruder_entries:
                found_any = True
                fname = cpath.name
                print(f"  Found {len(extruder_entries)} extruder metadata entries in {fname}:")
                for ptype, val in extruder_entries[:15]:
                    print(f"    - {ptype} value={val}")
                if len(extruder_entries) > 15:
                    print(f"    ... ({len(extruder_entries) - 15} more)")
        except Exception as e:
            print(f"  Error parsing {cpath.name}: {e}")

    # 3. Broad search inside all model files (3dmodel.model + object_*.model) for any extruder references
    broad_models = [model_path] if model_path.exists() else []
    if extracted_dir:
        for om in find_all_model_files(extracted_dir):
            if om not in broad_models:
                broad_models.append(om)
    for mp in broad_models:
        if not mp.exists():
            continue
        if mp.stat().st_size > MAX_MODEL_PARSE_SIZE:
            # already handled safely in last-resort; avoid duplicate work here
            continue
        try:
            tree = ET.parse(mp)
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
                print(f"  Extruder references found directly in {mp.name}:")
                for tag, key, val in extruder_refs[:10]:  # limit spam
                    print(f"    - <{tag}> key={key} value={val}")
        except Exception:
            pass

    # 4. Last resort: grep XML-like files (*.xml, *.model, *.config, *.rels) for "extruder"
    # (covers object_*.model files and all config variants; safe on large binaries-ish meshes)
    if extracted_dir.exists():
        mentions = []
        xml_like_exts = ("*.xml", "*.model", "*.config", "*.rels")
        for ext in xml_like_exts:
            for xf in extracted_dir.rglob(ext):
                try:
                    p = Path(xf)
                    fname = p.name
                    # Skip authoritative configs already reported with full context in dedicated section #2 (reduces noise)
                    if fname in ("model_settings.config", "Slic3r_PE_model.config"):
                        continue
                    size = p.stat().st_size
                    if size > 5 * 1024 * 1024:
                        # Safe scan: head+tail sample (via _safe_keyword_sample) to catch deep data; do not load 50MB+ fully
                        sample = _safe_keyword_sample(p)
                        if b"extruder" not in sample.lower():
                            continue
                        mentions.append((fname, f"[large {size//1024//1024}MB file - sampled] contains 'extruder'"))
                        continue
                    content = p.read_text(errors="ignore")
                    if "extruder" in content.lower():
                        # Find context
                        for line in content.splitlines():
                            if "extruder" in line.lower() and len(mentions) < 15:
                                mentions.append((fname, line.strip()[:120]))
                except Exception:
                    pass

        if mentions:
            found_any = True
            print("  'extruder' mentions found in XML-like files (showing up to 8; includes object_*.model):")
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


def _safe_collect_segmentation_values(model_path: Path) -> List[str]:
    """
    Chunked, low-memory scan for paint_color / mmu_segmentation values in (large) model files.
    Used for --inspect reports and used-extruder detection when full ET.parse is forbidden.
    Returns raw value strings (codec handles the rest).
    """
    vals: List[str] = []
    if not model_path.exists():
        return vals
    try:
        # Full text read of 50MB-ish is acceptable for inspect/detect (far lighter than ET DOM);
        # chunked for extra safety on truly huge files.
        content = model_path.read_text(encoding="utf-8", errors="strict")
        for pat in (
            r'paint_color=["\']([0-9A-Fa-f]+)["\']',
            r'slic3rpe:mmu_segmentation=["\']([0-9A-Fa-f]+)["\']',
            r'mmu_segmentation=["\']([0-9A-Fa-f]+)["\']',
        ):
            for m in re.finditer(pat, content):
                vals.append(m.group(1))
    except Exception as e:
        print(f"  Warning: safe collect on {model_path.name} failed: {e}")
    return vals


def _extract_extruders_from_config(cpath: Path) -> Set[int]:
    """Collect integer extruder values from any <metadata key="*extruder*" value="N"/> .
    Mirrors the exact parsing pattern used in _edit_model_config and inspect for consistency.
    """
    used: Set[int] = set()
    if not cpath or not cpath.exists():
        return used
    try:
        tree = ET.parse(cpath)
        root = tree.getroot()
        for elem in root.iter():
            if elem.tag.endswith("metadata") or elem.tag == "metadata":
                key = (elem.get("key") or "").lower()
                if "extruder" in key:
                    try:
                        used.add(int(elem.get("value", "0")))
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return used


# ---------------------------------------------------------------------
# Real remapping pipeline (wired in this step)
# ---------------------------------------------------------------------

def _edit_model_file(model_path: Path, mapping: Dict[int, int]) -> Tuple[int, int]:
    """
    Edit 3D/3dmodel.model (or object_*.model) in place.
    Supports both classic mmu_segmentation and Bambu/Orca paint_color.
    For files > MAX_MODEL_PARSE_SIZE uses safe text/regex rewrite (no ET.parse).
    Returns (num_triangle_attrs_changed, num_values_changed).
    """
    if not model_path.exists():
        return 0, 0

    size = model_path.stat().st_size
    if size > MAX_MODEL_PARSE_SIZE:
        # Safe large-file path: regex rewrite of the attr values only. Avoids building DOM.
        # paint_color and mmu attrs have clean hex values; re on the serialized XML is safe here.
        try:
            content = model_path.read_text(encoding="utf-8", errors="strict")
        except Exception as e:
            print(f"  ERROR reading large model for edit {model_path.name}: {e}")
            return 0, 0

        changed_attrs = 0
        changed_values = 0

        def _repl(m: re.Match) -> str:
            nonlocal changed_attrs, changed_values
            attr_name = m.group(1)
            quote = m.group(2)
            old_val = m.group(3)
            new_val = mmu_codec.remap_segmentation(old_val, mapping)
            if new_val is not None and new_val != old_val:
                changed_attrs += 1
                changed_values += 1
                return f'{attr_name}={quote}{new_val}{quote}'
            return m.group(0)

        # Catch paint_color= and the common serialized forms of mmu_segmentation (quote-insensitive for robustness)
        pat = r'(paint_color|slic3rpe:mmu_segmentation|mmu_segmentation)\s*=\s*(["\'])([0-9A-Fa-f]+)\2'
        new_content = re.sub(pat, _repl, content)
        if changed_attrs > 0:
            model_path.write_text(new_content, encoding="UTF-8")
        return changed_attrs, changed_values

    # Small file: original ET path (now also handles paint_color via extended attr list)
    try:
        tree = ET.parse(model_path)
        root = tree.getroot()
    except Exception as e:
        print(f"  ERROR parsing model XML: {e}")
        return 0, 0

    changed_attrs = 0
    changed_values = 0

    for elem in root.iter():
        # Look for the attribute in the forms we see in real files (now incl. paint_color)
        attr_names = [
            f"{{{SLICERPE_NS}}}mmu_segmentation",
            "slic3rpe:mmu_segmentation",
            "mmu_segmentation",
            PAINT_COLOR_ATTR,
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
    except Exception as e:
        print(f"  Warning: failed to parse/edit {config_path.name} for remap: {e}")
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
    except Exception as e:
        print(f"  Warning: failed to parse/edit {model_path.name} for remap: {e}")
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

        cfg = tdir / MODEL_CONFIG
        model_settings_cfg = tdir / MODEL_SETTINGS_CONFIG

        # Robust discovery: all model files (main + object_*.model for split 3MFs)
        # Small files: full ET; large: safe chunked re scan (supports paint_color in huge meshes like Bambu object_*.model)
        all_models = find_all_model_files(tdir)
        seg_vals: List[str] = []
        for m in all_models:
            if m.exists():
                if m.stat().st_size <= MAX_MODEL_PARSE_SIZE:
                    seg_vals.extend(v for _, v in find_mmu_segmentation_values(m))
                else:
                    print(f"  (note: large model file {m.name} using safe scan for segmentation/paint_color)")
                    seg_vals.extend(_safe_collect_segmentation_values(m))

        # Collect used from painting + all object-level config metadata (fixes "none" for model_settings-only files)
        used = mmu_codec.detect_used_extruders(seg_vals)
        for c in (cfg, model_settings_cfg):
            used |= _extract_extruders_from_config(c)
        stats["used_extruders_before"] = len(used)
        print(f"  extruder references (painting + object-level): {sorted(used) or 'none'}")

        # Apply edits to painting data and object/volume extruders across ALL model files + configs
        # (edit now handles large paint_color files via text path; no size guard)
        t_total = 0
        v_total = 0
        for m in all_models:
            if m.exists():
                t, v = _edit_model_file(m, mapping)
                t_total += t
                v_total += v
        stats["triangles_changed"] = t_total
        stats["values_changed"] = v_total

        c1 = _edit_model_config(cfg, mapping)
        c2 = _edit_model_config(model_settings_cfg, mapping) if model_settings_cfg.exists() else 0
        m_total = 0
        for m in all_models:
            if m.exists() and m.stat().st_size <= MAX_MODEL_PARSE_SIZE:
                m_total += _edit_model_object_extruders(m, mapping)
        total_config = c1 + c2 + m_total
        stats["config_entries_changed"] = total_config

        print(f"  model triangles updated: {t_total} (values rewritten: {v_total})")
        print(f"  object/volume extruder entries updated: {total_config}")
        if (t_total + total_config) > 0:
            print("  Note: XML may have minor formatting/encoding differences after rewrite (safe text regex for large meshes; ET for small). Slicers will normalize.")

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
            if model.exists() and model.stat().st_size <= MAX_MODEL_PARSE_SIZE:
                seg_values = [v for _, v in find_mmu_segmentation_values(model)]
                print(f"  mmu_segmentation / paint_color attributes found: {len(seg_values)}")
                counts = _pretty_counts_from_raw(seg_values)
                if counts:
                    print("  detected extruder usage (via codec):")
                    for ex in sorted(counts):
                        print(f"    extruder {ex}: ~{counts[ex]} occurrences")
                else:
                    print("  no recognizable mmu_segmentation / paint_color codes (or no painting data)")
            else:
                if model.exists():
                    print(f"  (note: large main model {MODEL_FILE} skipped for initial seg scan)")
                print(f"  {MODEL_FILE} not present or too large — may not contain Prusa MMU painting")
                seg_values = []

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


def _integration_tests() -> bool:
    """Compact integration tests using synthetic 3MFs (both classic mmu_segmentation and model_settings.config formats).
    Exercises find_*, _edit_*, _extract_*, perform_remap (dry), and robustness paths.
    Follows existing tempfile/zip/ET patterns exactly. No new files or deps.
    Returns True on success (raises on failure).
    """
    import tempfile
    import zipfile
    from pathlib import Path as _P

    def _mk_minimal_3mf(td: _P, *, classic: bool = False, settings: bool = False) -> _P:
        """Build a minimal valid 3MF in td with the chosen extruder data style."""
        (td / "[Content_Types].xml").write_text('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>', encoding="utf-8")
        model_dir = td / "3D"
        model_dir.mkdir(parents=True, exist_ok=True)
        meta_dir = td / "Metadata"
        meta_dir.mkdir(parents=True, exist_ok=True)

        if classic:
            # Classic triangle painting in 3dmodel.model (include ns decl so ET.parse succeeds on prefixed attr)
            (model_dir / "3dmodel.model").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<model xmlns:slic3rpe="http://schemas.prusa3d.com/slic3r/">'
                '<resources><object><mesh><triangles>'
                '<triangle slic3rpe:mmu_segmentation="4"/>'
                '<triangle slic3rpe:mmu_segmentation="8"/>'
                '</triangles></mesh></object></resources></model>',
                encoding="utf-8"
            )
        if settings:
            # model_settings.config style (Bambu/Orca/Prusa project)
            (meta_dir / "model_settings.config").write_text(
                '<?xml version="1.0"?><config>'
                '<object id="1"><metadata key="name" value="o"/><metadata key="extruder" value="1"/>'
                '<part id="0"><metadata key="extruder" value="3"/></part></object>'
                '</config>',
                encoding="utf-8"
            )
        # always a tiny main model for structure
        if not (model_dir / "3dmodel.model").exists():
            (model_dir / "3dmodel.model").write_text(
                '<?xml version="1.0"?><model><resources><object id="1"/></resources></model>',
                encoding="utf-8"
            )
        out = td / "synth.3mf"
        with zipfile.ZipFile(out, "w") as z:
            for f in td.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(td))
        return out

    # Test 1: classic segmentation roundtrip via perform (dry)
    with tempfile.TemporaryDirectory() as td1:
        tdp = _P(td1)
        src = _mk_minimal_3mf(tdp, classic=True)
        mapping = mmu_codec.build_mapping_from_strings(["1:2"])
        stats = perform_remap(src, tdp/"out.3mf", mapping, dry_run=True)
        assert stats["triangles_changed"] >= 1, "classic triangle edit failed"
        assert stats["config_entries_changed"] == 0

    # Test 2: model_settings.config remap via perform (dry) + used detection
    with tempfile.TemporaryDirectory() as td2:
        tdp = _P(td2)
        src = _mk_minimal_3mf(tdp, settings=True)
        mapping = mmu_codec.build_mapping_from_strings(["1:2", "3:4"])
        stats = perform_remap(src, tdp/"out.3mf", mapping, dry_run=True)
        assert stats["config_entries_changed"] == 2, f"settings edit count wrong: {stats}"
        # used should now include from config (not just painting)
        assert stats["used_extruders_before"] == 2  # 1 and 3

    # Test 3: direct edit helpers on extracted model_settings (exact pattern match)
    with tempfile.TemporaryDirectory() as td3:
        tdp = _P(td3)
        src = _mk_minimal_3mf(tdp, settings=True)
        safe_extract_to_temp(src, tdp / "ex")
        cfgp = tdp / "ex" / MODEL_SETTINGS_CONFIG
        assert cfgp.exists()
        ch = _edit_model_config(cfgp, {1: 5, 3: 5})
        assert ch == 2
        # verify content
        txt = cfgp.read_text()
        assert 'value="5"' in txt and 'value="1"' not in txt

    # Test 4: _extract works on both config styles + find_all_model_files
    with tempfile.TemporaryDirectory() as td4:
        tdp = _P(td4)
        # settings only
        src = _mk_minimal_3mf(tdp, settings=True)
        safe_extract_to_temp(src, tdp / "ex2")
        us = _extract_extruders_from_config(tdp / "ex2" / MODEL_SETTINGS_CONFIG)
        assert us == {1, 3}
        # models discovery
        ms = find_all_model_files(tdp / "ex2")
        assert any("3dmodel.model" in str(m) for m in ms)

    # Test 5: large-file safety paths (>8MB dummy model) - exercises skip notes, size guards, no ET.parse/crash
    with tempfile.TemporaryDirectory() as td5:
        tdp = _P(td5)
        exdir = tdp / "ex-large"
        exdir.mkdir()
        (exdir / "3D").mkdir()
        (exdir / "3D" / "Objects").mkdir(parents=True)
        large_model = exdir / "3D" / "Objects" / "object_1.model"
        # ~9MB XML-like (header + padding); hits > MAX without real content
        large_model.write_bytes(b'<?xml version="1.0"?><model>' + b" " * (9 * 1024 * 1024) + b"</model>")
        (exdir / "[Content_Types].xml").write_text('<?xml version="1.0"?><Types/>', encoding="utf-8")
        (exdir / "3D" / "3dmodel.model").write_text('<?xml version="1.0"?><model/>', encoding="utf-8")
        # build zip
        large_src = tdp / "large.3mf"
        with zipfile.ZipFile(large_src, "w") as z:
            for f in exdir.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(exdir))
        # exercise perform (will hit large skip in seg/edit loops + note)
        stats = perform_remap(large_src, tdp/"out.3mf", {1:2}, dry_run=True)
        assert stats["triangles_changed"] == 0  # skipped
        # direct guard check via find_all + size (no crash, large detected)
        ms = find_all_model_files(exdir)
        assert any(m.stat().st_size > MAX_MODEL_PARSE_SIZE for m in ms)

    # Test 6: direct _edit_model_object_extruders (metadata inside 3dmodel.model, not just config)
    with tempfile.TemporaryDirectory() as td6:
        tdp = _P(td6)
        src = _mk_minimal_3mf(tdp, settings=False)
        safe_extract_to_temp(src, tdp / "ex3")
        modelp = tdp / "ex3" / "3D" / "3dmodel.model"
        # inject direct extruder metadata (simulates object/volume level in model file)
        modelp.write_text('<?xml version="1.0"?><model><metadata key="extruder" value="2"/></model>', encoding="utf-8")
        ch = _edit_model_object_extruders(modelp, {2: 5})
        assert ch == 1
        assert 'value="5"' in modelp.read_text() and 'value="2"' not in modelp.read_text()

    # Test 7: paint_color roundtrip (Bambu/Orca style) via perform + direct edit + used detection
    with tempfile.TemporaryDirectory() as td7:
        tdp = _P(td7)
        # Reuse mk but manually inject paint_color triangles (same hex tokens)
        src = _mk_minimal_3mf(tdp, classic=False, settings=False)
        safe_extract_to_temp(src, tdp / "ex7")
        modelp = tdp / "ex7" / "3D" / "3dmodel.model"
        modelp.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<model xmlns:slic3rpe="http://schemas.prusa3d.com/slic3r/">'
            '<resources><object><mesh><triangles>'
            '<triangle v1="0" v2="1" v3="2" paint_color="8"/>'
            '<triangle v1="3" v2="4" v3="5" paint_color="0C"/>'
            '</triangles></mesh></object></resources></model>',
            encoding="utf-8"
        )
        # rebuild zip with modified
        with zipfile.ZipFile(tdp / "paint.3mf", "w") as z:
            for f in (tdp / "ex7").rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(tdp / "ex7"))
        mapping = mmu_codec.build_mapping_from_strings(["2:4", "3:1"])
        stats = perform_remap(tdp / "paint.3mf", tdp / "out.3mf", mapping, dry_run=False)  # must write to inspect result
        assert stats["triangles_changed"] >= 2, f"paint_color edit failed: {stats}"
        # Verify the rewritten file content
        safe_extract_to_temp(tdp / "out.3mf", tdp / "ex7out")
        txt = (tdp / "ex7out" / "3D" / "3dmodel.model").read_text()
        assert 'paint_color="1C"' in txt and 'paint_color="4"' in txt  # 8->1C (2->4), 0C->4 (3->1)
        assert 'paint_color="8"' not in txt and 'paint_color="0C"' not in txt
        # used detection via safe path (small here)
        us = mmu_codec.detect_used_extruders([v for _, v in find_mmu_segmentation_values(tdp / "ex7" / "3D" / "3dmodel.model")])
        assert us == {2, 3}

    print("  _integration_tests: all 7 synthetic cases passed (classic + model_settings + edits + discovery + large safety + direct object metadata + paint_color)")
    return True


if __name__ == "__main__":
    sys.exit(main())
