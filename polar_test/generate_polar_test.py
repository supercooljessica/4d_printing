#!/usr/bin/env python3
"""Generate a two-layer, radius-60-mm polar bend test in Prusa G-code.

The restrictive layer is one outward Archimedean spiral at 0.50 mm pitch.
The active layer has a small connecting hub and 12 radial rays. Each later
round bisects every angular gap and begins far enough out to touch the two
older rays with a small specified bead overlap. Both layers are 0.20 mm high.
The active bead width sets extrusion volume; speed falls as width rises to
keep volumetric flow at the same level as the restrictive layer.

Examples (from the 4dp folder):
  python3 polar_test/generate_polar_test.py
  python3 polar_test/generate_polar_test.py Prusa_XL.txt --active-width 0.7
  python3 polar_test/generate_polar_test.py Prusa_MK4.txt --active-width 0.6
  python3 polar_test/generate_polar_test.py Prusa_XL.txt --material pla --tool 1 --active-width 0.5
"""

import argparse
import math
import re
import sys
from pathlib import Path

# Reuse the same verified profile handling and extrusion model as bend_test.
BEND_TEST_DIR = Path(__file__).resolve().parent.parent / "bend_test"
sys.path.insert(0, str(BEND_TEST_DIR))
from generate_bend_test import (  # noqa: E402
    BED_TEMP_C,
    DEFAULT_PITCH_MM,
    DEFAULT_PRINT_SPEED_MM_S,
    FILAMENT_MM,
    LAYER_HEIGHT_MM,
    NOZZLE_TEMP_C,
    adapt_header,
    line_area,
    read_profile,
)

RADIUS_MM = 60.0
BED_XY_MM = {"MK3S": (250.0, 210.0), "MK4": (250.0, 210.0), "XL": (360.0, 360.0)}
BED_MARGIN_MM = 5.0
BASE_RAYS = 12
DEFAULT_OVERLAP_MM = 0.05
SPIRAL_MAX_SEGMENT_MM = 0.8
MATERIAL_TEMPERATURES_C = {"wood": (NOZZLE_TEMP_C, BED_TEMP_C), "pla": (215, 60)}


def resolve_profile(path):
    if path.is_file():
        return path
    if not path.is_absolute():
        folder = Path(__file__).resolve().parent
        for candidate in (folder / path, folder.parent / path):
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(f"Printer profile not found: {path}")


def printer_center_and_bounds(settings):
    cx = float(settings["x_min"]) + float(settings["cube_mm"]) / 2
    cy = float(settings["y_min"]) + float(settings["cube_mm"]) / 2
    x_min, y_min = cx - RADIUS_MM, cy - RADIUS_MM
    x_max, y_max = cx + RADIUS_MM, cy + RADIUS_MM
    bed_x, bed_y = BED_XY_MM[settings["model"]]
    if (x_min < BED_MARGIN_MM or y_min < BED_MARGIN_MM or
            x_max > bed_x - BED_MARGIN_MM or y_max > bed_y - BED_MARGIN_MM):
        raise ValueError("The 120 mm diameter test does not fit safely on this bed")
    return cx, cy, (x_min, y_min, x_max, y_max)


def polar_header(header, settings, bounds, tool, nozzle_temp, bed_temp):
    x_min, y_min, x_max, y_max = bounds
    width, height = x_max - x_min, y_max - y_min
    adapted = adapt_header(header, settings, x_min, y_min)
    lines = []
    area_count = 0
    for line in adapted.splitlines():
        if settings["model"] == "MK3S" and line.startswith("G80 X"):
            line = (f"G80 X{x_min - 1:g} Y{y_min - 1:g} "
                    f"W{width + 2:g} H{height + 2:g} ; mesh over polar test")
            area_count += 1
        elif settings["model"] in {"MK4", "XL"} and line.startswith("M555 X"):
            line = f"M555 X{x_min:g} Y{y_min:g} W{width:g} H{height:g} ; polar print area"
            area_count += 1
        elif line == "M486 Abend_test":
            line = "M486 Apolar_test"
        lines.append(line)
    if area_count != 1:
        raise ValueError("Could not set the polar print area in startup G-code")
    # The original profile prints with T0. The T1 changes below match a
    # T1-only XL job exported by PrusaSlicer 2.9.6 from the same profile.
    result = []
    changed = {"bed": 0, "nozzle": 0, "nozzle_check": 0, "unused_heater": 0,
               "preheat": 0, "tool_select": 0, "purge": 0, "purge_probe": 0}
    purge_moves = {
        "G0 X30 Y-7 Z10 F24000": "G0 X150 Y-7 Z10 F24000",
        "G0 E30 X40 Z0.2 F170": "G0 E30 X140 Z0.2 F170",
        "G0 X70 E9 F800": "G0 X110 E9 F800",
        "G0 X73 Z0.05 F8000": "G0 X107 Z0.05 F8000",
        "G0 X76 Z0.2 F8000": "G0 X104 Z0.2 F8000",
    }
    for line in lines:
        if line.startswith(("M140 ", "M190 ")) and re.search(r"\bS45\b", line):
            line = re.sub(r"\bS45\b", f"S{bed_temp}", line, count=1)
            changed["bed"] += 1
        elif line.startswith(("M104 ", "M109 ")) and re.search(r"\bS195\b", line):
            line = re.sub(r"\bS195\b", f"S{nozzle_temp}", line, count=1)
            changed["nozzle"] += 1
        if tool == 1:
            if line == "M862.1 T0 P0.4 A0 F0":
                line = "M862.1 T1 P0.4 A0 F0"
                changed["nozzle_check"] += 1
            elif line == "M104 T1 S0":
                line = "M104 T0 S0"
                changed["unused_heater"] += 1
            elif line.startswith(("M104 T0 S", "M109 T0 S")):
                line = line.replace("T0", "T1", 1)
                changed["preheat"] += 1
            elif line == "T0 S1 L0 D0":
                line = "T1 S1 L0 D0"
                changed["tool_select"] += 1
            elif line == "G29 P1 X30 Y0 W50 H20 C":
                line = "G29 P1 X30 Y0 W130 H20 C"
                changed["purge_probe"] += 1
            elif line in purge_moves:
                line = purge_moves[line]
                changed["purge"] += 1
            if line == "M572 S0.03":
                result.extend(["; Select the printing tool as in a T1-only PrusaSlicer job",
                               "G1 F21000", "T1 S1 L0 D0"])
            result.append(line)
            if line == "M142 S36":
                result.append(f"M109 S{nozzle_temp} T1 ; confirm print temperature")
        else:
            result.append(line)
    if changed["bed"] != 2 or changed["nozzle"] != 2:
        raise ValueError(f"Could not set print temperatures in XL startup: {changed}")
    if tool == 1 and (changed["nozzle_check"] != 1 or changed["unused_heater"] != 1 or
                      changed["preheat"] != 6 or changed["tool_select"] != 2 or
                      changed["purge"] != 5 or changed["purge_probe"] != 1):
        raise ValueError(f"Could not configure XL tool 1 startup: {changed}")
    return "\n".join(result)


def polar_footer(footer, settings, tool):
    if tool == 0:
        return footer
    if settings["model"] != "XL" or footer.count("M104 T0 S0") != 1:
        raise ValueError("Tool 1 shutdown requires the XL profile's T0 footer")
    return footer.replace("M104 T0 S0", "M104 T1 S0", 1)


def radial_rays(width, overlap):
    """Return stage lists of (angle, inner radius) radial paths.

    A new ray's inner endpoint is width-overlap from each adjacent older
    centerline, measured perpendicular to that ray. This gives the new bead
    an overlap of approximately `overlap` with both older beads at its root.
    """
    outer = RADIUS_MM - width / 2
    gap = 2 * math.pi / BASE_RAYS
    hub_radius = (width - overlap) / (2 * math.sin(gap / 2))
    stages = [[(i * gap, hub_radius) for i in range(BASE_RAYS)]]
    existing_count = BASE_RAYS
    while True:
        gap = 2 * math.pi / existing_count
        inner = (width - overlap) / math.sin(gap / 2)
        if inner >= outer:
            break
        stages.append([((i + 0.5) * gap, inner) for i in range(existing_count)])
        existing_count *= 2
    return hub_radius, stages


def generate(settings, header, footer, active_width, overlap, material="wood", tool=0):
    if not math.isfinite(active_width) or not DEFAULT_PITCH_MM <= active_width <= 0.80:
        raise ValueError("Active width must be between 0.50 and 0.80 mm")
    if not math.isfinite(overlap) or not 0 < overlap <= active_width / 4:
        raise ValueError("Overlap must be positive and no more than one quarter of active width")
    if material not in MATERIAL_TEMPERATURES_C:
        raise ValueError("Material must be wood or PLA")
    if tool not in (0, 1) or (tool == 1 and settings["model"] != "XL"):
        raise ValueError("Tool 1 is supported only for the two-head XL profile")
    nozzle_temp, bed_temp = MATERIAL_TEMPERATURES_C[material]
    cx, cy, bounds = printer_center_and_bounds(settings)
    header = polar_header(header, settings, bounds, tool, nozzle_temp, bed_temp)
    footer = polar_footer(footer, settings, tool)
    hub_radius, stages = radial_rays(active_width, overlap)
    ray_count = sum(len(stage) for stage in stages)
    travel_feed = int(settings["travel_feed_mm_min"])
    filament_area = math.pi * (FILAMENT_MM / 2) ** 2
    active_speed = DEFAULT_PRINT_SPEED_MM_S * line_area(DEFAULT_PITCH_MM) / line_area(active_width)
    lines = [
        "; Generated by generate_polar_test.py",
        f"; Printer {settings['model']}; tool T{tool}; radius {RADIUS_MM:g} mm; center X{cx:g} Y{cy:g}",
        "; Restrictive layer: outward spiral, pitch 0.500 mm, Z0.200 mm",
        f"; Active layer: hub and {ray_count} radial rays in {len(stages)} rounds, Z0.400 mm",
        f"; Active width {active_width:.3f} mm, branching overlap {overlap:.3f} mm",
        f"; Restrictive speed {DEFAULT_PRINT_SPEED_MM_S:.3f} mm/s; active speed {active_speed:.3f} mm/s",
        f"; {material.upper()} filament: nozzle {nozzle_temp} C, bed {bed_temp} C",
        "; Absolute XYZ, relative E; all extrusion stays inside radius 60 mm",
        header,
        "",
    ]
    position = None
    retracted = float(settings["initial_retracted_mm"])
    extrusion_total = [0.0, 0.0]

    def point(radius, angle):
        return cx + radius * math.cos(angle), cy + radius * math.sin(angle)

    def start_path(x, y, z):
        nonlocal position, retracted
        lines.append(f"G0 Z{z + 0.4:.3f} F720")
        lines.append(f"G0 X{x:.3f} Y{y:.3f} F{travel_feed}")
        position = (round(x, 3), round(y, 3))
        lines.append(f"G0 Z{z:.3f} F720")
        if retracted:
            lines.append(f"G1 E{retracted:.3f} F1500 ; restore retraction")
            retracted = 0.0

    def extrude(x, y, bead_width, speed, layer):
        nonlocal position
        target = (round(x, 3), round(y, 3))
        distance = math.dist(position, target)
        if distance < 0.001:
            return
        amount = distance * line_area(bead_width) / filament_area
        lines.append(f"G1 X{target[0]:.3f} Y{target[1]:.3f} E{amount:.5f} F{speed * 60:.0f}")
        position = target
        extrusion_total[layer] += amount

    def retract():
        nonlocal retracted
        lines.append("G1 E-0.800 F2100 ; retract before travel")
        retracted = 0.8

    if settings["model"] != "MK3S":
        lines.append("M486 S0 ; begin polar test object")
    lines.extend([";LAYER_CHANGE", ";Z:0.20", ";HEIGHT:0.20", ";TYPE:Restrictive spiral"])

    spiral_inner = DEFAULT_PITCH_MM / 2
    spiral_outer = RADIUS_MM - DEFAULT_PITCH_MM / 2
    slope = DEFAULT_PITCH_MM / (2 * math.pi)
    end_angle = (spiral_outer - spiral_inner) / slope
    start_x, start_y = point(spiral_inner, 0.0)
    start_path(start_x, start_y, LAYER_HEIGHT_MM)
    angle = 0.0
    segments = 0
    while angle < end_angle:
        radius = spiral_inner + slope * angle
        step = min(0.35, SPIRAL_MAX_SEGMENT_MM / math.hypot(radius, slope))
        angle = min(end_angle, angle + step)
        radius = spiral_inner + slope * angle
        x, y = point(radius, angle)
        extrude(x, y, DEFAULT_PITCH_MM, DEFAULT_PRINT_SPEED_MM_S, 0)
        segments += 1
    retract()
    lines.append("M73 P50 R0")

    lines.extend([f"M106 S{int(settings['fan_after_layer_1'])}",
                  ";LAYER_CHANGE", ";Z:0.40", ";HEIGHT:0.20", ";TYPE:Active radial layer"])
    # The narrow circular hub bonds all twelve initial rays into one path network.
    lines.append(f"; Connecting hub, centerline radius {hub_radius:.3f} mm")
    hub_x, hub_y = point(hub_radius, 0.0)
    start_path(hub_x, hub_y, 2 * LAYER_HEIGHT_MM)
    hub_segments = max(48, math.ceil(2 * math.pi * hub_radius / 0.5))
    for i in range(1, hub_segments + 1):
        x, y = point(hub_radius, 2 * math.pi * i / hub_segments)
        extrude(x, y, active_width, active_speed, 1)
    retract()

    completed = 0
    outer = RADIUS_MM - active_width / 2
    for stage_index, rays in enumerate(stages):
        inner = rays[0][1]
        lines.append(f"; RADIAL ROUND {stage_index}: {len(rays)} rays, inner radius {inner:.3f} mm")
        for index, (ray_angle, inner) in enumerate(rays):
            # Alternate directions to reduce travel between neighboring rays.
            from_radius, to_radius = (inner, outer) if index % 2 == 0 else (outer, inner)
            x0, y0 = point(from_radius, ray_angle)
            x1, y1 = point(to_radius, ray_angle)
            lines.append(f"; RAY round={stage_index} angle={math.degrees(ray_angle):.4f} inner_r={inner:.3f}")
            start_path(x0, y0, 2 * LAYER_HEIGHT_MM)
            extrude(x1, y1, active_width, active_speed, 1)
            completed += 1
            if completed < ray_count:
                retract()
        lines.append(f"M73 P{min(99, 50 + int(49 * completed / ray_count))} R0")

    lines.extend(["; End polar test toolpath", footer, ""])
    if segments < 1000 or ray_count < BASE_RAYS or min(extrusion_total) <= 0:
        raise ValueError("Incomplete polar toolpath")
    return "\n".join(lines), len(stages), ray_count, segments, active_speed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", type=Path, default=Path("Prusa_XL.txt"),
                        help="Prusa printer profile TXT (default: Prusa_XL.txt)")
    parser.add_argument("--active-width", "--active-spacing", dest="active_width", type=float,
                        default=DEFAULT_PITCH_MM, metavar="MM",
                        help="radial bead width, 0.50 to 0.80 mm (default: 0.50)")
    parser.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP_MM, metavar="MM",
                        help="approximate overlap where a new ray joins older rays (default: 0.05 mm)")
    parser.add_argument("--material", choices=("wood", "pla"), default="wood",
                        help="filament preset: wood 195/45 C or PLA 215/60 C (default: wood)")
    parser.add_argument("--tool", type=int, choices=(0, 1), default=0,
                        help="XL tool: 0 first extruder or 1 second extruder (default: 0)")
    parser.add_argument("--output", type=Path, help="output ASCII .gcode file")
    args = parser.parse_args()
    try:
        settings, header, footer = read_profile(resolve_profile(args.profile))
        gcode, rounds, rays, segments, active_speed = generate(
            settings, header, footer, args.active_width, args.overlap, args.material, args.tool)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    width_name = f"{args.active_width:.2f}".replace(".", "p")
    if args.material == "wood" and args.tool == 0:
        name = f"polar_test_{settings['model']}_active_{width_name}.gcode"
    else:
        nozzle_temp, bed_temp = MATERIAL_TEMPERATURES_C[args.material]
        name = (f"polar_test_{settings['model']}_T{args.tool}_{args.material.upper()}_"
                f"{nozzle_temp}C_{bed_temp}C_active_{width_name}.gcode")
    output = args.output or Path(__file__).with_name(name)
    if output.suffix.lower() != ".gcode":
        parser.error("Output filename must end in .gcode")
    output.write_text(gcode, encoding="utf-8")
    print(f"Wrote {output}: {segments} spiral segments, {rays} rays in {rounds} rounds; "
          f"active speed {active_speed:.2f} mm/s")


if __name__ == "__main__":
    main()
