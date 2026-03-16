#!/usr/bin/env python3
"""
merger.py
──────────────────
Merges EasyEDA design-rule constraints (exported as JSON) into a Specctra
DSN autorouter file.

EasyEDA exports the .dsn with placeholder/default rule values and exports
the actual design rules separately as a JSON file.  This script reads both,
derives the correct values from the JSON, and patches:

  • structure-level rules  (clear, width)
  • via padstack size       (outer / inner diameter)
  • per-net class rules     (width, clearance)

Usage
-----
    python merger.py <input.dsn> <rules.json> [-o output.dsn]

If -o is omitted the output file is written next to the input as
<basename>_merged.dsn.

Units
-----
The JSON file stores values in **mm**.
The DSN file uses **mil** (1 mil = 0.0254 mm) at resolution 1000,
so all JSON values are converted: mil = mm / 0.0254
"""

import argparse
import json
import re
import sys
from pathlib import Path


# ── unit helpers ─────────────────────────────────────────────────────────────

MM_PER_MIL = 0.0254


def mm_to_mil(mm: float) -> float:
    return mm / MM_PER_MIL


def fmt(value_mil: float) -> str:
    """Format a mil value for DSN: up to 4 decimal places, no trailing zeros."""
    return f"{value_mil:.4f}".rstrip("0").rstrip(".")


# ── JSON rule extraction ──────────────────────────────────────────────────────

def get_default_rule(rules_json: dict, category: str, key: str):
    """Return the isSetDefault entry under rules_json[category][key]."""
    section = rules_json.get(category, {})
    # First try the named key, then fall back to whichever entry has isSetDefault
    candidates = section.get(key, {})
    if isinstance(candidates, dict) and "isSetDefault" in candidates:
        return candidates
    for v in section.values():
        if isinstance(v, dict) and v.get("isSetDefault"):
            return v
    return {}


def extract_clearance_mm(rules_json: dict) -> float:
    """Primary track-to-track clearance (mm)."""
    spacing = rules_json.get("Spacing", {}).get("Safe Spacing", {})
    for entry in spacing.values():
        if entry.get("isSetDefault"):
            table = entry.get("table", [])
            if table and table[0]:
                return table[0][0]   # [Track][Track] cell
    return 0.102  # fallback


def extract_track_width_mm(rules_json: dict) -> dict:
    """Returns {"min": mm, "default": mm, "max": mm} for the default track rule."""
    track_section = rules_json.get("Physics", {}).get("Track", {})
    for entry in track_section.values():
        if entry.get("isSetDefault"):
            f = entry.get("form", {})
            return {
                "min":     f.get("strokeWidthMin", 0.127),
                "default": f.get("strokeWidthDefault", 0.254),
                "max":     f.get("strokeWidthMax", 2.54),
            }
    return {"min": 0.127, "default": 0.254, "max": 2.54}


def extract_via_size_mm(rules_json: dict) -> dict:
    """Returns {"outer": mm, "inner": mm} for the default via size."""
    via_entry = get_default_rule(
        rules_json.get("Physics", {}), "Via Size", "viaSize"
    )
    f = via_entry.get("form", {})
    return {
        "outer": f.get("viaOuterdiameterDefault", 0.61),
        "inner": f.get("viaInnerdiameterDefault", 0.305),
    }


def extract_smd_clearance_mm(rules_json: dict) -> float:
    """SMD-pad clearance (Track↔SMD Pad), falls back to general clearance."""
    spacing = rules_json.get("Spacing", {}).get("Safe Spacing", {})
    for entry in spacing.values():
        if entry.get("isSetDefault"):
            table = entry.get("table", [])
            if len(table) > 1 and table[1]:
                return table[1][0]   # [SMD Pad][Track] cell
    return extract_clearance_mm(rules_json)


# ── DSN patching ──────────────────────────────────────────────────────────────

def patch_structure_rules(dsn: str, clear_mil: float, width_mil: float) -> str:
    """
    Replace the three structure-level rule lines:
        (rule(clear ...))
        (rule(clear ... (type default_smd)))
        (rule(clear ... (type smd_smd)))
        (rule(width ...))
    """
    c = fmt(clear_mil)
    w = fmt(width_mil)

    dsn = re.sub(
        r'\(rule\(clear [0-9.]+\)\)',
        f'(rule(clear {c}))',
        dsn,
        count=1,
    )
    dsn = re.sub(
        r'\(rule\(clear [0-9.]+ \(type default_smd\)\)\)',
        f'(rule(clear {c} (type default_smd)))',
        dsn,
        count=1,
    )
    dsn = re.sub(
        r'\(rule\(clear [0-9.]+ \(type smd_smd\)\)\)',
        f'(rule(clear {c} (type smd_smd)))',
        dsn,
        count=1,
    )
    dsn = re.sub(
        r'\(rule\(width [0-9.]+\)\)',
        f'(rule(width {w}))',
        dsn,
        count=1,
    )
    return dsn


def patch_via_padstack(dsn: str, outer_mil: float) -> str:
    """
    Replace the diameter in every  (shape(circle N <diam>))  line that belongs
    to the via0 padstack.  The DSN circle radius = outer diameter in mils.
    """
    # Locate the via0 padstack block
    via_start = dsn.find("(padstack via0")
    if via_start == -1:
        print("  [warn] via0 padstack not found – skipping via size patch")
        return dsn

    # Find the end of that padstack block (matching closing paren)
    depth = 0
    via_end = via_start
    for i, ch in enumerate(dsn[via_start:], start=via_start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                via_end = i + 1
                break

    via_block = dsn[via_start:via_end]
    patched_block = re.sub(
        r'(\(shape\(circle\s+\d+\s+)[0-9.]+(\))',
        lambda m: f"{m.group(1)}{fmt(outer_mil)}{m.group(2)}",
        via_block,
    )
    return dsn[:via_start] + patched_block + dsn[via_end:]


def patch_net_class_rules(dsn: str, width_mil: float, clear_mil: float) -> str:
    """
    Replace  (rule \\n  (width ...)\\n  (clearance ...))  blocks that appear
    inside the network / class section (after the library).
    """
    def replacer(m):
        indent = m.group(1)
        return (
            f"{indent}(rule \n"
            f"{indent}  (width {fmt(width_mil)})\n"
            f"{indent}  (clearance {fmt(clear_mil)})"
        )

    # Match multi-line rule blocks:  (rule \n    (width N)\n    (clearance N)
    dsn = re.sub(
        r'( +)\(rule \s*\n\s*\(width [0-9.]+\)\s*\n\s*\(clearance [0-9.]+\)',
        replacer,
        dsn,
    )
    return dsn


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Merge EasyEDA design-rule JSON into a Specctra DSN file."
    )
    parser.add_argument("dsn_file",  help="Input .dsn file")
    parser.add_argument("json_file", help="EasyEDA design-rule .json / .txt file")
    parser.add_argument(
        "-o", "--output",
        help="Output .dsn file (default: <input>_merged.dsn)",
        default=None,
    )
    args = parser.parse_args()

    dsn_path  = Path(args.dsn_file)
    json_path = Path(args.json_file)

    if not dsn_path.exists():
        sys.exit(f"Error: DSN file not found: {dsn_path}")
    if not json_path.exists():
        sys.exit(f"Error: JSON file not found: {json_path}")

    output_path = Path(args.output) if args.output else \
        dsn_path.with_name(dsn_path.stem + "_merged.dsn")

    # ── load ──────────────────────────────────────────────────────────────────
    print(f"Reading DSN : {dsn_path}")
    dsn_text = dsn_path.read_text(encoding="utf-8")

    print(f"Reading JSON: {json_path}")
    rules_json = json.loads(json_path.read_text(encoding="utf-8"))

    # ── extract rule values ───────────────────────────────────────────────────
    clear_mm      = extract_clearance_mm(rules_json)
    track_mm      = extract_track_width_mm(rules_json)
    via_mm        = extract_via_size_mm(rules_json)

    clear_mil     = mm_to_mil(clear_mm)
    width_mil     = mm_to_mil(track_mm["default"])
    via_outer_mil = mm_to_mil(via_mm["outer"])

    print()
    print("Derived rule values (from JSON):")
    print(f"  Track-to-track clearance : {clear_mm:.4f} mm  →  {clear_mil:.2f} mil")
    print(f"  Default track width      : {track_mm['default']:.4f} mm  →  {width_mil:.2f} mil")
    print(f"  Via outer diameter       : {via_mm['outer']:.4f} mm  →  {via_outer_mil:.2f} mil")
    print(f"  Via inner (drill)        : {via_mm['inner']:.4f} mm")
    print()

    # ── patch ─────────────────────────────────────────────────────────────────
    print("Patching structure rules …")
    dsn_text = patch_structure_rules(dsn_text, clear_mil, width_mil)

    print("Patching via padstack size …")
    dsn_text = patch_via_padstack(dsn_text, via_outer_mil)

    print("Patching per-net class rules …")
    dsn_text = patch_net_class_rules(dsn_text, width_mil, clear_mil)

    # ── write ─────────────────────────────────────────────────────────────────
    output_path.write_text(dsn_text, encoding="utf-8")
    print(f"\nDone! Merged DSN written to: {output_path}")


if __name__ == "__main__":
    main()
