"""
codec.py — Core remapping logic for PrusaSlicer mmu_segmentation hex values.

The painting data uses a small set of hex tokens to represent extruder assignments
on triangles (or sub-regions of split triangles).

Known mapping (1-based extruder -> hex token), derived from community reverse
engineering (Prusa forum + Kurt Gluck's work) and consistent with
PrusaSlicer 3mf.cpp (slic3rpe:mmu_segmentation attribute):

    1 -> "4"
    2 -> "8"
    3 -> "0C"
    4 -> "1C"
    5 -> "2C"
    ...

Longer strings (complex brush painting that splits triangles) contain these
same tokens embedded in a larger hex stream. We tokenize greedily (preferring
2-char tokens) and rewrite only the extruder ids.

This module is pure and has no side effects — easy to unit test.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Set, Optional
import re

# ---------------------------------------------------------------------
# The canonical mapping table
# ---------------------------------------------------------------------

# extruder (1-based) -> hex token (upper case, as it appears in 3MFs)
EXTRUDER_TO_TOKEN: Dict[int, str] = {
    1: "4",
    2: "8",
    3: "0C",
    4: "1C",
    5: "2C",
    6: "3C",
    7: "4C",
    8: "5C",
    9: "6C",
    10: "7C",
    11: "8C",
    12: "9C",
    13: "AC",
    14: "BC",
    15: "CC",
    16: "DC",
}

# Reverse for fast lookup: token -> extruder
TOKEN_TO_EXTRUDER: Dict[str, int] = {v: k for k, v in EXTRUDER_TO_TOKEN.items()}

# All known tokens, longest first (important for greedy tokenization)
KNOWN_TOKENS: List[str] = sorted(EXTRUDER_TO_TOKEN.values(), key=len, reverse=True)


def tokenize_segmentation(value: str) -> List[Tuple[int, str]]:
    """
    Turn a (possibly long) mmu_segmentation hex string into a list of
    (extruder, token) pairs using greedy longest-match on the known set.

    Returns [] on unparseable input (caller can fall back to dumb replace).
    The hex is upper-cased for matching; original case is not preserved
    because PrusaSlicer normalizes these values.
    """
    if not value or not isinstance(value, str):
        return []

    s = value.strip().upper()
    if not s:
        return []

    result: List[Tuple[int, str]] = []
    i = 0
    n = len(s)

    while i < n:
        matched = False
        for tok in KNOWN_TOKENS:
            if s.startswith(tok, i):
                ex = TOKEN_TO_EXTRUDER[tok]
                result.append((ex, tok))
                i += len(tok)
                matched = True
                break
        if not matched:
            # Unknown byte / partial token — we cannot safely interpret
            # the stream. Caller should treat the whole value as opaque.
            return []

    return result


def remap_segmentation(value: str, mapping: Dict[int, int]) -> Optional[str]:
    """
    Apply a user-supplied remapping (old_extruder -> new_extruder) to a
    segmentation value.

    Returns the new hex string (concatenated tokens) on success.
    Returns None if the value could not be tokenized safely (caller may
    then fall back to a conservative whole-string substitution or warn).
    """
    tokens = tokenize_segmentation(value)
    if not tokens:
        return None

    out_parts: List[str] = []
    for old_ex, tok in tokens:
        new_ex = mapping.get(old_ex, old_ex)  # identity if not mentioned
        new_tok = EXTRUDER_TO_TOKEN.get(new_ex, tok)
        out_parts.append(new_tok)

    return "".join(out_parts)


def detect_used_extruders(values: List[str]) -> Set[int]:
    """Return the set of logical extruders referenced across a list of raw values."""
    used: Set[int] = set()
    for v in values:
        for ex, _ in tokenize_segmentation(v):
            used.add(ex)
    return used


def build_mapping_from_strings(pairs: List[str]) -> Dict[int, int]:
    """
    Turn CLI-style strings into a clean mapping dict.

    Supports multiple forms:
      - Multiple --map flags: --map 2:4 --map 4:1
      - Comma or space separated in one flag: --map "2:4,4:1,1:3" or --map "2:4 4:1"

    Later entries win on duplicate keys. Validates ranges 1-16.
    """
    mapping: Dict[int, int] = {}

    for raw in pairs:
        # Split on commas and/or whitespace so users can do --map "1:2,3:4,5:6"
        tokens = [t.strip() for t in re.split(r'[,\s]+', raw) if t.strip()]

        for token in tokens:
            if ":" not in token:
                raise ValueError(f"Invalid mapping (expected OLD:NEW): {token} (from '{raw}')")
            left, right = token.split(":", 1)
            try:
                old = int(left.strip())
                new = int(right.strip())
            except ValueError:
                raise ValueError(f"Mapping values must be integers: {token} (from '{raw}')")
            if not (1 <= old <= 16 and 1 <= new <= 16):
                raise ValueError(f"Extruder numbers must be 1-16: {token} (from '{raw}')")
            mapping[old] = new

    return mapping


# ---------------------------------------------------------------------
# Simple self-test / usage when run directly
# ---------------------------------------------------------------------

def _self_test() -> None:
    print("codec.py self-test")

    # Simple cases from the forum
    assert tokenize_segmentation("4") == [(1, "4")]
    assert tokenize_segmentation("8") == [(2, "8")]
    assert tokenize_segmentation("0C") == [(3, "0C")]
    assert tokenize_segmentation("1C") == [(4, "1C")]
    assert tokenize_segmentation("2C") == [(5, "2C")]

    # Round-trip identity
    m = {1: 1, 2: 2, 3: 3}
    assert remap_segmentation("4", m) == "4"
    assert remap_segmentation("0C", m) == "0C"

    # Real swap 1 <-> 3
    m13 = {1: 3, 3: 1}
    assert remap_segmentation("4", m13) == "0C"
    assert remap_segmentation("0C", m13) == "4"
    assert remap_segmentation("8", m13) == "8"   # untouched

    # Long complex string (example taken from the forum thread).
    # The simple tokenizer intentionally only recognizes the short known codes.
    # On real complex data it will often return [] (opaque). This is the
    # documented v1 limitation. The important thing is that *simple* paintings
    # (the vast majority of bucket-fill use cases) work perfectly.
    long_example = "00040020064404006044A6400200A20020400004A60604006044A244AA30030341C41C44244641C44641C1CAA61C..."
    toks = tokenize_segmentation(long_example)
    print(f"  long example (truncated): {len(toks)} tokens (expected 0 for this structural blob)")
    # Demonstrate that we do not crash and the simple cases remain perfect
    print("  long example path exercised without crashing")

    # Detection
    vals = ["4", "8", "0C", "1C", long_example]
    used = detect_used_extruders(vals)
    assert 1 in used and 2 in used and 3 in used and 4 in used

    # CLI mapping builder
    m = build_mapping_from_strings(["1:3", "3:1", "2:2"])
    assert m == {1: 3, 3: 1, 2: 2}

    print("  all self-tests passed")


if __name__ == "__main__":
    _self_test()
