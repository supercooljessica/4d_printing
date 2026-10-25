#!/usr/bin/env python3
"""Generate a two-layer regular-hexagon bend test in Prusa G-code.

The hexagon is centered on the bed. Its center-to-vertex radius and the
restrictive and active bead widths can be set independently. Layer 1 is a
continuous outward hexagonal spiral, with normal pitch equal to its bead
width. Layer 2 is a connected hub with branching radial rays and a small
overlap where new rays meet older rays. Both layers stay 0.20 mm high.
Active speed changes with bead area to match restrictive volumetric flow.

Example, from the 4d_printing folder:
    python3 hex_test/generate_hex_test.py Prusa_XL.txt --radius 30 \
        --restrictive-width 0.42 --active-width 0.45 --tool 0
"""

import argparse
import math
import sys
from pathlib import Path

POLAR_DIR = Path(__file__).resolve().parent.parent / "polar_test"
sys.path.insert(0, str(POLAR_DIR))
from generate_polar_test import (  # noqa: E402
    BASE_RAYS,
    BED_MARGIN_MM,
    BED_XY_MM,
    DEFAULT_OVERLAP_MM,
    MATERIAL_TEMPERATURES_C,
    RADIUS_MM,
    polar_footer,
    polar_header,
    resolve_profile,
)

BEND_DIR = Path(__file__).resolve().parent.parent / "bend_test"
sys.path.insert(0, str(BEND_DIR))
from generate_bend_test import (  # noqa: E402
    DEFAULT_PITCH_MM,
    DEFAULT_PRINT_SPEED_MM_S,
    FILAMENT_MM,
    LAYER_HEIGHT_MM,
    line_area,
    read_profile,
)

SIDE_ANGLE = math.pi / 3
DEFAULT_RADIUS_MM = RADIUS_MM
DEFAULT_RESTRICTIVE_WIDTH_MM = DEFAULT_PITCH_MM
MIN_WIDTH_MM = 0.40
MAX_WIDTH_MM = 0.80
MAX_SEGMENT_MM = 0.8


def hex_radius(apothem, angle):
    """Radius to the regular hexagon whose sides are `apothem` from center."""
    offset_from_side_normal = (angle % SIDE_ANGLE) - SIDE_ANGLE / 2
    return apothem / math.cos(offset_from_side_normal)


def hex_center_and_bounds(settings, radius):
    cx = float(settings["x_min"]) + float(settings["cube_mm"]) / 2
    cy = float(settings["y_min"]) + float(settings["cube_mm"]) / 2
    bounds = (cx - radius, cy - radius, cx + radius, cy + radius)
    bed_x, bed_y = BED_XY_MM[settings["model"]]
    if (bounds[0] < BED_MARGIN_MM or bounds[1] < BED_MARGIN_MM or
            bounds[2] > bed_x - BED_MARGIN_MM or bounds[3] > bed_y - BED_MARGIN_MM):
        raise ValueError("The hexagon does not fit safely on this bed")
    return cx, cy, bounds


def radial_rays(width, overlap, apothem):
    """Build bisection rounds, stopping before roots reach a hexagon side."""
    gap = 2 * math.pi / BASE_RAYS
    hub_radius = (width - overlap) / (2 * math.sin(gap / 2))
    stages = [[(i * gap, hub_radius) for i in range(BASE_RAYS)]]
    existing_count = BASE_RAYS
    shortest_ray_end = apothem - width / 2
    while True:
        gap = 2 * math.pi / existing_count
        inner = (width - overlap) / math.sin(gap / 2)
        if inner >= shortest_ray_end:
            break
        stages.append([((i + 0.5) * gap, inner) for i in range(existing_count)])
        existing_count *= 2
    return hub_radius, stages


def generate(settings, header, footer, active_width, overlap,
             radius=DEFAULT_RADIUS_MM, restrictive_width=DEFAULT_RESTRICTIVE_WIDTH_MM,
             material="wood", tool=0):
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("Hexagon radius must be positive")
    if not math.isfinite(restrictive_width) or not MIN_WIDTH_MM <= restrictive_width <= MAX_WIDTH_MM:
        raise ValueError("Restrictive width must be between 0.40 and 0.80 mm")
    if not math.isfinite(active_width) or not MIN_WIDTH_MM <= active_width <= MAX_WIDTH_MM:
        raise ValueError("Active width must be between 0.40 and 0.80 mm")
    if not math.isfinite(overlap) or not 0 < overlap <= active_width / 4:
        raise ValueError("Overlap must be positive and at most one quarter of active width")
    if material not in MATERIAL_TEMPERATURES_C:
        raise ValueError("Material must be wood or PLA")
    if tool not in (0, 1) or (tool == 1 and settings["model"] != "XL"):
        raise ValueError("Tool 1 is supported only for the two-head XL profile")
    nozzle_temp, bed_temp = MATERIAL_TEMPERATURES_C[material]
    apothem = radius * math.cos(math.pi / 6)
    if apothem <= max(restrictive_width, active_width):
        raise ValueError("Hexagon is too small for the selected bead widths")
    cx, cy, bounds = hex_center_and_bounds(settings, radius)
    header = polar_header(header, settings, bounds, tool, nozzle_temp, bed_temp)
    header = header.replace("M486 Apolar_test", "M486 Ahex_test")
    header = header.replace("; polar print area", "; hex print area")
    if settings["model"] != "MK3S" and "M486 Ahex_test" not in header:
        raise ValueError("Could not name the hex object in the printer header")
    footer = polar_footer(footer, settings, tool)
    hub_radius, stages = radial_rays(active_width, overlap, apothem)
    ray_count = sum(len(stage) for stage in stages)
    travel_feed = int(settings["travel_feed_mm_min"])
    filament_area = math.pi * (FILAMENT_MM / 2) ** 2
    active_speed = DEFAULT_PRINT_SPEED_MM_S * line_area(restrictive_width) / line_area(active_width)

    lines = [
        "; Generated by generate_hex_test.py",
        f"; Printer {settings['model']}; tool T{tool}; regular hexagon, center X{cx:g} Y{cy:g}",
        f"; Center-to-vertex radius {radius:g} mm; vertex-to-vertex width {2 * radius:g} mm; apothem {apothem:.4f} mm",
        f"; Restrictive layer: outward hexagonal spiral, {restrictive_width:.3f} mm bead and normal pitch, Z0.200 mm",
        f"; Active layer: hub and {ray_count} radial rays in {len(stages)} rounds, Z0.400 mm",
        f"; Active width {active_width:.3f} mm, branching overlap {overlap:.3f} mm",
        f"; Restrictive speed {DEFAULT_PRINT_SPEED_MM_S:.3f} mm/s; active speed {active_speed:.3f} mm/s",
        f"; {material.upper()} filament: nozzle {nozzle_temp} C, bed {bed_temp} C",
        "; Absolute XYZ, relative E; centerlines inset by half their bead width",
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
        lines.append("M486 S0 ; begin hex test object")
    lines.extend([";LAYER_CHANGE", ";Z:0.20", ";HEIGHT:0.20", ";TYPE:Restrictive hexagonal spiral"])

    # Each revolution adds one restrictive bead width to the apothem, so
    # adjacent sides have that center spacing measured perpendicular to them.
    inner_apothem = restrictive_width / 2
    outer_apothem = apothem - restrictive_width / 2
    slope = restrictive_width / (2 * math.pi)
    end_angle = (outer_apothem - inner_apothem) / slope
    start_radius = hex_radius(inner_apothem, 0.0)
    x0, y0 = point(start_radius, 0.0)
    start_path(x0, y0, LAYER_HEIGHT_MM)
    angle = 0.0
    segments = 0
    while angle < end_angle - 1e-10:
        current_apothem = inner_apothem + slope * angle
        current_radius = hex_radius(current_apothem, angle)
        # Explicitly land at each corner; a segment must not cut across one.
        next_corner = (math.floor(angle / SIDE_ANGLE + 1e-9) + 1) * SIDE_ANGLE
        step = min(0.2, MAX_SEGMENT_MM / (1.35 * current_radius + 2 * slope))
        angle = min(end_angle, next_corner, angle + step)
        current_apothem = inner_apothem + slope * angle
        x, y = point(hex_radius(current_apothem, angle), angle)
        extrude(x, y, restrictive_width, DEFAULT_PRINT_SPEED_MM_S, 0)
        segments += 1
    retract()
    lines.append("M73 P50 R0")

    lines.extend([f"M106 S{int(settings['fan_after_layer_1'])}",
                  ";LAYER_CHANGE", ";Z:0.40", ";HEIGHT:0.20", ";TYPE:Active radial layer"])
    # The hub joins the twelve initial rays. Use a hexagonal hub so this
    # layer follows the same basic geometry as the restrictive layer.
    lines.append(f"; Connecting hex hub, circumradius {hub_radius:.3f} mm")
    hub_x, hub_y = point(hub_radius, 0.0)
    start_path(hub_x, hub_y, 2 * LAYER_HEIGHT_MM)
    for i in range(1, 7):
        x, y = point(hub_radius, i * SIDE_ANGLE)
        extrude(x, y, active_width, active_speed, 1)
    retract()

    completed = 0
    for stage_index, rays in enumerate(stages):
        inner = rays[0][1]
        lines.append(f"; RADIAL ROUND {stage_index}: {len(rays)} rays, inner radius {inner:.3f} mm")
        for index, (ray_angle, inner) in enumerate(rays):
            outer = hex_radius(apothem - active_width / 2, ray_angle)
            if inner >= outer:
                raise ValueError("A radial ray has no space within the hexagon")
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

    lines.extend(["; End hex test toolpath", footer, ""])
    if segments < 1000 or ray_count < BASE_RAYS or min(extrusion_total) <= 0:
        raise ValueError("Incomplete hex toolpath")
    return "\n".join(lines), len(stages), ray_count, segments, active_speed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", type=Path, default=Path("Prusa_XL.txt"),
                        help="Prusa printer profile TXT (default: Prusa_XL.txt)")
    parser.add_argument("--radius", type=float, default=DEFAULT_RADIUS_MM, metavar="MM",
                        help="center-to-vertex radius (default: 60 mm, width 120 mm)")
    parser.add_argument("--restrictive-width", type=float, default=DEFAULT_RESTRICTIVE_WIDTH_MM,
                        metavar="MM", help="hex spiral bead width and pitch, 0.40 to 0.80 mm (default: 0.50)")
    parser.add_argument("--active-width", type=float, default=DEFAULT_PITCH_MM, metavar="MM",
                        help="radial bead width, 0.40 to 0.80 mm (default: 0.50)")
    parser.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP_MM, metavar="MM",
                        help="branch overlap (default: 0.05 mm)")
    parser.add_argument("--material", choices=("wood", "pla"), default="wood",
                        help="wood 195/45 C or PLA 215/60 C (default: wood)")
    parser.add_argument("--tool", type=int, choices=(0, 1), default=0,
                        help="XL tool: 0 first extruder or 1 second extruder (default: T0)")
    parser.add_argument("--output", type=Path, help="output ASCII .gcode file")
    args = parser.parse_args()
    try:
        settings, header, footer = read_profile(resolve_profile(args.profile))
        tool = args.tool
        gcode, rounds, rays, segments, active_speed = generate(
            settings, header, footer, args.active_width, args.overlap,
            radius=args.radius, restrictive_width=args.restrictive_width,
            material=args.material, tool=tool)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    active_name = f"{args.active_width:.2f}".replace(".", "p")
    restrictive_name = f"{args.restrictive_width:.2f}".replace(".", "p")
    width_name = f"{2 * args.radius:g}".replace(".", "p")
    nozzle_temp, bed_temp = MATERIAL_TEMPERATURES_C[args.material]
    name = (f"hex_test_{settings['model']}_T{tool}_{args.material.upper()}_"
            f"{nozzle_temp}C_{bed_temp}C_width_{width_name}_"
            f"restrictive_{restrictive_name}_active_{active_name}.gcode")
    output = args.output or Path(__file__).with_name(name)
    if output.suffix.lower() != ".gcode":
        parser.error("Output filename must end in .gcode")
    output.write_text(gcode, encoding="utf-8")
    print(f"Wrote {output}: {segments} hex spiral segments, {rays} rays in {rounds} rounds; "
          f"active speed {active_speed:.2f} mm/s")


if __name__ == "__main__":
    main()
