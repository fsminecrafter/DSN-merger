#!/usr/bin/env python3
"""
merge.py
──────────────────
Merges EasyEDA design-rule constraints into a Specctra DSN autorouter file,
including per-net rule overrides (e.g. Power nets get wider tracks).

Three ways to supply design rules (pick one):

  1. --epro  <file.epro>   EasyEDA Pro project file  ← most complete
  2. --json  <file.json>   EasyEDA exported rule JSON ← global rules only
  3. --power-nets GND 3V3  Manual list of power net names (used with --json)

Usage examples
--------------
  python merge.py board.dsn --epro project.epro
  python merge.py board.dsn --epro project.epro -o output.dsn

  python merge.py board.dsn --json rules.json
  python merge.py board.dsn --json rules.json --power-nets GND 3V3 3V3V

If -o is omitted the output is written as <input>_merged.dsn next to the input.

Units
-----
DSN files use mil at resolution 1000.  All values are converted:
  mil = mm / 0.0254
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path


MM_PER_MIL = 0.0254

EPCB_RULE_CATEGORY = {
    "1": "Safe Spacing", "2": "Other Spacing", "3": "Track",
    "4": "Blind/Buried Via", "5": "Via Size", "6": "Plane Zone",
    "7": "Copper Zone", "8": "Paste Mask Expansion",
    "9": "Solder Mask Expansion", "10": "Differential Pair",
    "11": "Net Length", "12": "Common",
}


def mm_to_mil(mm):
    return mm / MM_PER_MIL

def fmt(v):
    return f"{v:.4f}".rstrip("0").rstrip(".")


# ── .epro loader ──────────────────────────────────────────────────────────────

def load_from_epro(epro_path):
    if not zipfile.is_zipfile(epro_path):
        sys.exit(f"Error: {epro_path} is not a valid .epro (zip) file")

    epcb_text = None
    with zipfile.ZipFile(epro_path) as zf:
        for name in zf.namelist():
            if name.startswith("PCB/") and name.endswith(".epcb"):
                epcb_text = zf.read(name).decode("utf-8", errors="replace")
                print(f"  Found PCB file: {name}")
                break

    if epcb_text is None:
        sys.exit("Error: no .epcb file found inside the .epro archive")

    records = []
    for line in epcb_text.splitlines():
        line = line.strip()
        if line.startswith("["):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # Collect RULE entries: ["RULE", cat_key, rule_name, is_default, [unit, ...values]]
    rules = {}
    for r in records:
        if r and r[0] == "RULE" and len(r) >= 5:
            cat_key, rule_name, is_default, payload = r[1], r[2], bool(r[3]), r[4]
            values = payload[1:] if isinstance(payload, list) and payload else []
            rules[(cat_key, rule_name)] = {"is_default": is_default, "values": values}

    # Collect RULE_SELECTOR entries: ["RULE_SELECTOR", ["NET", name], _, {cat_key: rule_name}]
    power_nets = set()
    for r in records:
        if r and r[0] == "RULE_SELECTOR" and len(r) >= 4:
            net_name  = r[1][1] if isinstance(r[1], list) and len(r[1]) > 1 else ""
            overrides = r[3] if isinstance(r[3], dict) else {}
            if "3" in overrides and overrides["3"] == "Power" and net_name:
                power_nets.add(net_name)

    # Extract values (epcb stores in mil)
    clearance_mm = 0.102
    for (cat, _), data in rules.items():
        if cat == "1" and data["is_default"] and data["values"]:
            # values[0] is the nested spacing table (list of lists); [0][0] = Track-Track cell in mil
            table = data["values"][0] if data["values"] else []
            if isinstance(table, list) and table and isinstance(table[0], list) and table[0]:
                clearance_mm = table[0][0] * MM_PER_MIL
            break

    default_width_mm = 0.254
    power_width_mm   = 0.4
    for (cat, name), data in rules.items():
        if cat == "3" and data["values"] and len(data["values"]) >= 2:
            width_mm = data["values"][1] * MM_PER_MIL
            if data["is_default"]:
                default_width_mm = width_mm
            if name == "Power":
                power_width_mm = width_mm

    via_outer_mm = 0.61
    via_inner_mm = 0.305
    for (cat, _), data in rules.items():
        if cat == "5" and data["is_default"] and len(data["values"]) >= 5:
            # epcb stores via radius in mil; multiply by 2 to get diameter in mil, then convert to mm
            via_outer_mm = data["values"][1] * 2 * MM_PER_MIL
            via_inner_mm = data["values"][4] * 2 * MM_PER_MIL
            break

    return {
        "clearance_mm":     clearance_mm,
        "default_width_mm": default_width_mm,
        "via_outer_mm":     via_outer_mm,
        "via_inner_mm":     via_inner_mm,
        "power_width_mm":   power_width_mm,
        "power_nets":       power_nets,
    }


# ── JSON loader ───────────────────────────────────────────────────────────────

def load_from_json(json_path, power_nets=None):
    data = json.loads(json_path.read_text(encoding="utf-8"))

    clearance_mm = 0.102
    for entry in data.get("Spacing", {}).get("Safe Spacing", {}).values():
        if entry.get("isSetDefault"):
            t = entry.get("table", [])
            if t and t[0]:
                clearance_mm = t[0][0]
            break

    default_width_mm = 0.254
    power_width_mm   = 0.4
    for name, entry in data.get("Physics", {}).get("Track", {}).items():
        f = entry.get("form", {})
        if entry.get("isSetDefault"):
            default_width_mm = f.get("strokeWidthDefault", 0.254)
        if name == "Power":
            power_width_mm = f.get("strokeWidthDefault", 0.4)

    via_outer_mm = 0.61
    via_inner_mm = 0.305
    for entry in data.get("Physics", {}).get("Via Size", {}).values():
        if entry.get("isSetDefault"):
            f = entry.get("form", {})
            via_outer_mm = f.get("viaOuterdiameterDefault", 0.61)
            via_inner_mm = f.get("viaInnerdiameterDefault", 0.305)
            break

    return {
        "clearance_mm":     clearance_mm,
        "default_width_mm": default_width_mm,
        "via_outer_mm":     via_outer_mm,
        "via_inner_mm":     via_inner_mm,
        "power_width_mm":   power_width_mm,
        "power_nets":       set(power_nets) if power_nets else set(),
    }


# ── DSN patching ──────────────────────────────────────────────────────────────

def patch_structure_rules(dsn, clear_mil, width_mil):
    c, w = fmt(clear_mil), fmt(width_mil)
    dsn = re.sub(r'\(rule\(clear [0-9.]+\)\)',
                 f'(rule(clear {c}))', dsn, count=1)
    dsn = re.sub(r'\(rule\(clear [0-9.]+ \(type default_smd\)\)\)',
                 f'(rule(clear {c} (type default_smd)))', dsn, count=1)
    dsn = re.sub(r'\(rule\(clear [0-9.]+ \(type smd_smd\)\)\)',
                 f'(rule(clear {c} (type smd_smd)))', dsn, count=1)
    dsn = re.sub(r'\(rule\(width [0-9.]+\)\)',
                 f'(rule(width {w}))', dsn, count=1)
    return dsn


def patch_via_padstack(dsn, outer_mil):
    via_start = dsn.find("(padstack via0")
    if via_start == -1:
        print("  [warn] via0 padstack not found – skipping via size patch")
        return dsn
    depth = via_end = 0
    for i, ch in enumerate(dsn[via_start:], start=via_start):
        if ch == "(":   depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                via_end = i + 1
                break
    via_block = dsn[via_start:via_end]
    patched = re.sub(
        r'(\(shape\(circle\s+\d+\s+)[0-9.]+(\))',
        lambda m: f"{m.group(1)}{fmt(outer_mil)}{m.group(2)}",
        via_block,
    )
    return dsn[:via_start] + patched + dsn[via_end:]


def patch_net_class_rules(dsn, rules):
    clear_mil         = mm_to_mil(rules["clearance_mm"])
    default_width_mil = mm_to_mil(rules["default_width_mm"])
    power_width_mil   = mm_to_mil(rules["power_width_mm"])
    power_nets        = rules["power_nets"]

    def replace_class_block(m):
        full_block   = m.group(0)
        name_m       = re.search(r'\(class\s+(\S+)', full_block)
        class_name   = name_m.group(1).strip("'\"") if name_m else ""
        width_mil    = power_width_mil if class_name in power_nets else default_width_mil

        def replace_rule(rm):
            indent = rm.group(1)
            return (
                f"{indent}(rule \n"
                f"{indent}  (width {fmt(width_mil)})\n"
                f"{indent}  (clearance {fmt(clear_mil)})"
            )

        return re.sub(
            r'( +)\(rule \s*\n\s*\(width [0-9.]+\)\s*\n\s*\(clearance [0-9.]+\)',
            replace_rule,
            full_block,
        )

    dsn = re.sub(
        r'\(class\s+\S+.*?(?=\n    \(class |\Z)',
        replace_class_block,
        dsn,
        flags=re.DOTALL,
    )
    return dsn


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Merge EasyEDA design rules into a Specctra DSN file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python merge.py board.dsn --epro project.epro
  python merge.py board.dsn --epro project.epro -o output.dsn
  python merge.py board.dsn --json rules.json
  python merge.py board.dsn --json rules.json --power-nets GND 3V3 3V3V
        """,
    )
    parser.add_argument("dsn_file", help="Input .dsn file")
    parser.add_argument("-o", "--output", default=None,
                        help="Output .dsn (default: <input>_merged.dsn)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--epro", metavar="FILE",
                     help=".epro project file (includes per-net rule assignments)")
    src.add_argument("--json", metavar="FILE",
                     help="EasyEDA exported design-rule JSON")
    parser.add_argument("--power-nets", nargs="+", metavar="NET", default=[],
                        help="Nets to assign the Power track rule (--json mode only)")

    args = parser.parse_args()

    dsn_path = Path(args.dsn_file)
    if not dsn_path.exists():
        sys.exit(f"Error: DSN file not found: {dsn_path}")
    output_path = Path(args.output) if args.output else \
        dsn_path.with_name(dsn_path.stem + "_merged.dsn")

    if args.epro:
        epro_path = Path(args.epro)
        if not epro_path.exists():
            sys.exit(f"Error: .epro file not found: {epro_path}")
        print(f"Reading .epro : {epro_path}")
        rules = load_from_epro(epro_path)
    else:
        json_path = Path(args.json)
        if not json_path.exists():
            sys.exit(f"Error: JSON file not found: {json_path}")
        print(f"Reading JSON  : {json_path}")
        rules = load_from_json(json_path, power_nets=args.power_nets)

    print(f"Reading DSN   : {dsn_path}\n")
    print("Design rules:")
    print(f"  Clearance (default)  : {rules['clearance_mm']:.4f} mm  →  {fmt(mm_to_mil(rules['clearance_mm']))} mil")
    print(f"  Track width (default): {rules['default_width_mm']:.4f} mm  →  {fmt(mm_to_mil(rules['default_width_mm']))} mil")
    print(f"  Track width (Power)  : {rules['power_width_mm']:.4f} mm  →  {fmt(mm_to_mil(rules['power_width_mm']))} mil")
    print(f"  Via outer diameter   : {rules['via_outer_mm']:.4f} mm  →  {fmt(mm_to_mil(rules['via_outer_mm']))} mil")
    print(f"  Via inner (drill)    : {rules['via_inner_mm']:.4f} mm")
    if rules["power_nets"]:
        print(f"  Power nets           : {', '.join(sorted(rules['power_nets']))}")
    else:
        print("  Power nets           : (none)")
    print()

    dsn_text = dsn_path.read_text(encoding="utf-8")

    print("Patching structure rules …")
    dsn_text = patch_structure_rules(dsn_text,
        mm_to_mil(rules["clearance_mm"]), mm_to_mil(rules["default_width_mm"]))

    print("Patching via padstack size …")
    dsn_text = patch_via_padstack(dsn_text, mm_to_mil(rules["via_outer_mm"]))

    print("Patching per-net class rules …")
    dsn_text = patch_net_class_rules(dsn_text, rules)

    output_path.write_text(dsn_text, encoding="utf-8")
    print(f"\nDone! Written to: {output_path}")


if __name__ == "__main__":
    main()
