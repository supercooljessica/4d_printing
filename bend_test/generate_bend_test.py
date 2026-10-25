#!/usr/bin/env python3
"""Generate a centered, two-layer 60 x 20 mm bend test in ASCII G-code.

The restrictive layer runs along X (the 60 mm axis), with 0.42 mm beads by
default. The active layer runs along Y (the 20 mm axis). Both layers are
always 0.20 mm high. Active bead width is also the target center spacing;
centers are adjusted slightly where needed to cover the rectangle exactly.
Print speed changes inversely with bead cross-section, keeping volumetric
flow at the restrictive-layer level.

Examples, from the 4dp folder:
    python3 bend_test/generate_bend_test.py Prusa_MK3S.txt
    python3 bend_test/generate_bend_test.py Prusa_MK4.txt --active-width 0.3
    python3 bend_test/generate_bend_test.py Prusa_XL.txt --active-width 0.5

Uses the selected TXT profile for printer startup, shutdown, placement, and
travel feed. Startup temperatures and mesh print area are adapted for this
rectangle and the requested wood filament temperatures.
"""

import argparse
import math
import re
from pathlib import Path

LAYER_HEIGHT_MM = 0.20
RECTANGLE_X_MM = 60.0
RECTANGLE_Y_MM = 20.0
DEFAULT_PITCH_MM = 0.50  # Kept for polar_test's independent spiral settings.
RESTRICTIVE_WIDTH_MM = 0.42
MIN_ACTIVE_WIDTH_MM = 0.20
MAX_ACTIVE_WIDTH_MM = 0.80
NOZZLE_MM = 0.40
FILAMENT_MM = 1.75
DEFAULT_PRINT_SPEED_MM_S = 20.0
NOZZLE_TEMP_C = 195
BED_TEMP_C = 45
RETRACTION_MM = 0.8


def read_profile(path):
    sections = {"settings": [], "header": [], "footer": []}
    section = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line in ("[settings]", "[header]", "[footer]"):
            section = line[1:-1]
        elif section is not None:
            sections[section].append(line)
    settings = {}
    for line in sections["settings"]:
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    model = settings["model"]
    if model not in {"MK3S", "MK4", "XL"}:
        raise ValueError(f"Unsupported printer model: {model}")
    if float(settings["nozzle_diameter_mm"]) != NOZZLE_MM:
        raise ValueError("This print requires a 0.4 mm nozzle")
    if float(settings["filament_diameter_mm"]) != FILAMENT_MM:
        raise ValueError("This print assumes 1.75 mm filament")
    header = "\n".join(sections["header"]).strip()
    footer = "\n".join(sections["footer"]).strip()
    if not header or not footer or "M83" not in header or "G90" not in header:
        raise ValueError("Selected profile lacks the expected G-code header/footer")
    return settings, header, footer


def replace_s_temperature(line, value):
    """Replace an S setpoint on one G-code line, retaining its comment."""
    code, separator, comment = line.partition(";")
    code, count = re.subn(r"\bS-?\d+(?:\.\d+)?\b", f"S{value}", code, count=1)
    if count != 1:
        raise ValueError(f"Expected an S temperature in: {line}")
    return code.rstrip() + (" ;" + comment if separator else "")


def adapt_header(header, settings, x_min, y_min):
    model = settings["model"]
    original_nozzle = float(settings["first_layer_nozzle_c"])
    original_bed = float(settings["bed_c"])
    changed = {"bed_set": 0, "bed_wait": 0, "nozzle_set": 0, "nozzle_wait": 0, "area": 0}
    result = []
    for line in header.splitlines():
        stripped = line.strip()
        if "printing object cube_10mm.stl" in line:
            continue
        if stripped.startswith("M486 A"):
            result.append("M486 Abend_test")
            continue
        if stripped.startswith("M140 ") and re.search(rf"\bS{original_bed:g}\b", stripped):
            line = replace_s_temperature(line, BED_TEMP_C)
            changed["bed_set"] += 1
        elif stripped.startswith("M190 ") and re.search(rf"\bS{original_bed:g}\b", stripped):
            line = replace_s_temperature(line, BED_TEMP_C)
            changed["bed_wait"] += 1
        elif stripped.startswith("M104 ") and re.search(rf"\bS{original_nozzle:g}\b", stripped):
            line = replace_s_temperature(line, NOZZLE_TEMP_C)
            changed["nozzle_set"] += 1
        elif stripped.startswith("M109 ") and re.search(rf"\bS{original_nozzle:g}\b", stripped):
            line = replace_s_temperature(line, NOZZLE_TEMP_C)
            changed["nozzle_wait"] += 1
        if stripped.startswith("M221 S"):
            line = "M221 S100 ; flow for calculated extrusion"
        if model == "MK3S" and stripped.startswith("G80 X"):
            line = f"G80 X{x_min - 1:g} Y{y_min - 1:g} W62 H22 ; mesh over rectangle"
            changed["area"] += 1
        elif model in {"MK4", "XL"} and stripped.startswith("M555 X"):
            line = f"M555 X{x_min:g} Y{y_min:g} W60 H20 ; print area"
            changed["area"] += 1
        elif model == "XL" and stripped == "G1 X207 Y168 Z5 F24000":
            line = f"G1 X{x_min + 32:g} Y{y_min - 7:g} Z5 F24000"
        elif model == "XL" and stripped.startswith("G29 P9 X175 Y168"):
            line = f"G29 P9 X{x_min:g} Y{y_min - 7:g} W32 H7"
        result.append(line)
    if any(count != 1 for count in changed.values()):
        raise ValueError(f"Could not adapt all printer startup commands: {changed}")
    adapted = "\n".join(result)
    if "S230" in adapted or "S215" in adapted or "M140 S60" in adapted or "M190 S60" in adapted:
        raise ValueError("A source PLA print temperature remains in the adapted header")
    return adapted


def line_area(width_mm):
    """Prusa-style bead: rectangle with semicircular ends in cross-section."""
    h = LAYER_HEIGHT_MM
    return h * (width_mm - h) + math.pi * (h / 2) ** 2


def line_centers(center, extent, width):
    """Tile an extent with bead centers, allowing only slight overlap."""
    count = math.ceil(extent / width - 1e-9)
    if count < 2:
        raise ValueError("Bead width leaves fewer than two lines")
    first = center - extent / 2 + width / 2
    step = (extent - width) / (count - 1)
    return [first + i * step for i in range(count)]


def generate(settings, header, footer, active_width, restrictive_width=RESTRICTIVE_WIDTH_MM):
    if not (math.isfinite(active_width) and MIN_ACTIVE_WIDTH_MM <= active_width <= MAX_ACTIVE_WIDTH_MM):
        raise ValueError("Active width must be from 0.20 to 0.80 mm")
    if not (math.isfinite(restrictive_width) and 0.20 <= restrictive_width <= 0.80):
        raise ValueError("Restrictive width must be from 0.20 to 0.80 mm")
    model = settings["model"]
    cx = float(settings["x_min"]) + float(settings["cube_mm"]) / 2
    cy = float(settings["y_min"]) + float(settings["cube_mm"]) / 2
    x0, x1 = cx - RECTANGLE_X_MM / 2, cx + RECTANGLE_X_MM / 2
    y0, y1 = cy - RECTANGLE_Y_MM / 2, cy + RECTANGLE_Y_MM / 2
    if x0 < 0 or y0 < 0:
        raise ValueError("Rectangle would extend below bed origin")
    header = adapt_header(header, settings, x0, y0)
    filament_area = math.pi * (FILAMENT_MM / 2) ** 2
    restrictive_area = line_area(restrictive_width)
    active_area = line_area(active_width)
    active_speed = DEFAULT_PRINT_SPEED_MM_S * restrictive_area / active_area
    y_positions = line_centers(cy, RECTANGLE_Y_MM, restrictive_width)
    x_positions = line_centers(cx, RECTANGLE_X_MM, active_width)
    restrictive_pitch = y_positions[1] - y_positions[0]
    active_pitch = x_positions[1] - x_positions[0]
    travel_feed = int(settings["travel_feed_mm_min"])
    lines = [
        "; Generated by generate_bend_test.py",
        f"; Printer: {model}; rectangle: 60 x 20 mm centered at X{cx:g} Y{cy:g}",
        "; Layer 1 restrictive: X-direction snake, Z0.20 mm",
        f"; Layer 2 active: Y-direction snake, Z0.40 mm, bead width {active_width:.3f} mm",
        f"; Widths: restrictive {restrictive_width:.3f} mm, active {active_width:.3f} mm",
        f"; Center spacing: restrictive {restrictive_pitch:.4f} mm, active {active_pitch:.4f} mm",
        "; Both bead patterns cover the full 60 x 20 mm rectangle",
        "; Widths below the 0.4 mm nozzle diameter are nominal volume targets",
        f"; Speeds: restrictive {DEFAULT_PRINT_SPEED_MM_S:.3f} mm/s, active {active_speed:.3f} mm/s",
        "; Wood filament: nozzle 195 C, bed 45 C; 1.75 mm filament, 0.4 mm nozzle",
        "; Absolute XYZ; relative E; active flow per time equals restrictive flow per time",
        header,
        "",
    ]
    current = None
    extrusion_total = [0.0, 0.0]

    def travel(x, y):
        nonlocal current
        lines.append(f"G0 X{x:.3f} Y{y:.3f} F{travel_feed}")
        current = (x, y)

    def extrude(x, y, width, speed, layer_index):
        nonlocal current
        distance = math.dist(current, (x, y))
        amount = distance * line_area(width) / filament_area
        if amount <= 0:
            raise ValueError("Zero-length extrusion")
        lines.append(f"G1 X{x:.3f} Y{y:.3f} E{amount:.5f} F{speed * 60:.0f}")
        current = (x, y)
        extrusion_total[layer_index] += amount

    # First layer: long X strokes, short extruded U-turns at alternating ends.
    first_x = x0 + restrictive_width / 2
    last_x = x1 - restrictive_width / 2
    lines.extend([";LAYER_CHANGE", ";Z:0.20", ";HEIGHT:0.20", ";TYPE:Restrictive layer"])
    lines.append("G0 Z0.600 F720")
    travel(first_x, y_positions[0])
    lines.append("G0 Z0.200 F720")
    initial_retracted = float(settings["initial_retracted_mm"])
    if initial_retracted:
        lines.append(f"G1 E{initial_retracted:.3f} F1500 ; restore header retraction")
    if model != "MK3S":
        lines.append("M486 S0 ; begin bend test object")
    for i, y in enumerate(y_positions):
        start_x, end_x = (first_x, last_x) if i % 2 == 0 else (last_x, first_x)
        if i:
            extrude(start_x, y, restrictive_width, DEFAULT_PRINT_SPEED_MM_S, 0)
        extrude(end_x, y, restrictive_width, DEFAULT_PRINT_SPEED_MM_S, 0)
    lines.extend(["G1 E-0.8 F2100 ; retract for second layer", "M73 P50 R0"])

    # Second layer: short Y strokes, with the requested center spacing.
    fan = int(settings["fan_after_layer_1"])
    lines.extend([f"M106 S{fan}", ";LAYER_CHANGE", ";Z:0.40", ";HEIGHT:0.20", ";TYPE:Active layer"])
    first_y = y0 + active_width / 2
    last_y = y1 - active_width / 2
    lines.append("G0 Z0.800 F720")
    travel(x_positions[0], first_y)
    lines.extend(["G0 Z0.400 F720", "G1 E0.800 F1500 ; restore layer retraction"])
    for i, x in enumerate(x_positions):
        start_y, end_y = (first_y, last_y) if i % 2 == 0 else (last_y, first_y)
        if i:
            extrude(x, start_y, active_width, active_speed, 1)
        extrude(x, end_y, active_width, active_speed, 1)
    lines.extend(["M73 P99 R0", "; End bend test toolpath", footer, ""])
    if len(y_positions) < 2 or extrusion_total[0] <= 0 or extrusion_total[1] <= 0:
        raise ValueError("Invalid generated path")
    return "\n".join(lines), len(y_positions), len(x_positions), active_speed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path, help="Prusa_MK3S.txt, Prusa_MK4.txt, or Prusa_XL.txt")
    parser.add_argument("--restrictive-width", type=float, default=RESTRICTIVE_WIDTH_MM, metavar="MM",
                        help="restrictive bead width (default: 0.42 mm)")
    parser.add_argument("--active-width", "--active-spacing", dest="active_width", type=float,
                        default=RESTRICTIVE_WIDTH_MM, metavar="MM",
                        help="active bead width, 0.20 to 0.80 mm (default: 0.42)")
    parser.add_argument("--output", type=Path, help="output ASCII .gcode path")
    args = parser.parse_args()
    profile = args.profile
    if not profile.is_file() and not profile.is_absolute():
        script_dir = Path(__file__).resolve().parent
        for candidate in (script_dir / profile, script_dir.parent / profile):
            if candidate.is_file():
                profile = candidate
                break
    try:
        settings, header, footer = read_profile(profile)
        gcode, restrictive_count, active_count, active_speed = generate(
            settings, header, footer, args.active_width, args.restrictive_width)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    restrictive_name = f"{args.restrictive_width:.2f}".replace(".", "p")
    active_name = f"{args.active_width:.2f}".replace(".", "p")
    output = args.output or Path(__file__).with_name(
        f"bend_test_{settings['model']}_restrictive_{restrictive_name}_active_{active_name}.gcode")
    if output.suffix.lower() != ".gcode":
        parser.error("Output filename must end in .gcode (ASCII G-code)")
    output.write_text(gcode, encoding="utf-8")
    print(f"Wrote {output}: {restrictive_count} restrictive lines, {active_count} active lines, active speed {active_speed:.2f} mm/s")


if __name__ == "__main__":
    main()
