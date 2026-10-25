#!/usr/bin/env python3
"""Put several two-layer bend tests into one printer G-code job.

Default: XL, four tests at active widths 0.20, 0.30, 0.40, 0.50 mm,
arranged as two columns by two rows with 10 mm between rectangles.
Each 60 x 20 mm rectangle keeps its long side along X. The entire batch is
centered on the bed. All restrictive layers are printed at Z0.20 before
any active layers are printed at Z0.40. Restrictive bead width defaults to
0.42 mm. The requested active width also sets the target center spacing.

Examples, from the 4dp folder:
    python3 bend_test/generate_bend_test_batch.py
    python3 bend_test/generate_bend_test_batch.py Prusa_XL.txt --active-widths 0.2 0.4 0.5 --columns 2
    python3 bend_test/generate_bend_test_batch.py Prusa_MK4.txt --active-widths 0.3 0.5 --columns 1
"""

import argparse
import math
from pathlib import Path

from generate_bend_test import (
    BED_TEMP_C,
    DEFAULT_PRINT_SPEED_MM_S,
    FILAMENT_MM,
    LAYER_HEIGHT_MM,
    MAX_ACTIVE_WIDTH_MM,
    MIN_ACTIVE_WIDTH_MM,
    NOZZLE_TEMP_C,
    RECTANGLE_X_MM,
    RECTANGLE_Y_MM,
    RESTRICTIVE_WIDTH_MM,
    adapt_header,
    line_area,
    line_centers,
    read_profile,
)

BED_XY_MM = {"MK3S": (250.0, 210.0), "MK4": (250.0, 210.0), "XL": (360.0, 360.0)}
BED_MARGIN_MM = 5.0


def resolve_profile(path):
    if path.is_file():
        return path
    if not path.is_absolute():
        script_dir = Path(__file__).resolve().parent
        for candidate in (script_dir / path, script_dir.parent / path):
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(f"Profile TXT file not found: {path}")


def place_samples(settings, active_widths, columns, gap):
    if not active_widths or columns < 1 or gap < 5:
        raise ValueError("Need at least one active width, at least one column, and at least 5 mm gap")
    if not all(math.isfinite(w) and MIN_ACTIVE_WIDTH_MM <= w <= MAX_ACTIVE_WIDTH_MM
               for w in active_widths):
        raise ValueError("Each active width must be from 0.20 to 0.80 mm")
    columns = min(columns, len(active_widths))
    rows = math.ceil(len(active_widths) / columns)
    cx = float(settings["x_min"]) + float(settings["cube_mm"]) / 2
    cy = float(settings["y_min"]) + float(settings["cube_mm"]) / 2
    total_height = rows * RECTANGLE_Y_MM + (rows - 1) * gap
    samples = []
    for row in range(rows):
        count = min(columns, len(active_widths) - row * columns)
        row_width = count * RECTANGLE_X_MM + (count - 1) * gap
        row_x_min = cx - row_width / 2
        y_min = cy - total_height / 2 + row * (RECTANGLE_Y_MM + gap)
        for column in range(count):
            index = row * columns + column
            x_min = row_x_min + column * (RECTANGLE_X_MM + gap)
            samples.append({"number": index + 1, "width": active_widths[index], "row": row + 1,
                            "column": column + 1, "x_min": x_min, "y_min": y_min,
                            "cx": x_min + RECTANGLE_X_MM / 2,
                            "cy": y_min + RECTANGLE_Y_MM / 2})
    x_min = min(s["x_min"] for s in samples)
    y_min = min(s["y_min"] for s in samples)
    x_max = max(s["x_min"] + RECTANGLE_X_MM for s in samples)
    y_max = max(s["y_min"] + RECTANGLE_Y_MM for s in samples)
    bed_x, bed_y = BED_XY_MM[settings["model"]]
    if (x_min < BED_MARGIN_MM or y_min < BED_MARGIN_MM or
            x_max > bed_x - BED_MARGIN_MM or y_max > bed_y - BED_MARGIN_MM):
        raise ValueError(f"Batch bounds X{x_min:g}..{x_max:g}, Y{y_min:g}..{y_max:g} exceed the safe bed area")
    return samples, (x_min, y_min, x_max, y_max)


def batch_header(header, settings, bounds):
    x_min, y_min, x_max, y_max = bounds
    width, height = x_max - x_min, y_max - y_min
    result = adapt_header(header, settings, x_min, y_min)
    lines = []
    changed = 0
    for line in result.splitlines():
        if settings["model"] == "MK3S" and line.startswith("G80 X"):
            line = f"G80 X{x_min - 1:g} Y{y_min - 1:g} W{width + 2:g} H{height + 2:g} ; mesh over batch"
            changed += 1
        elif settings["model"] in {"MK4", "XL"} and line.startswith("M555 X"):
            line = f"M555 X{x_min:g} Y{y_min:g} W{width:g} H{height:g} ; batch print area"
            changed += 1
        elif line == "M486 Abend_test":
            line = "M486 Abend_test_batch"
        lines.append(line)
    if changed != 1:
        raise ValueError("Could not set the full batch print area")
    return "\n".join(lines)


def generate(settings, header, footer, samples, bounds, restrictive_width=RESTRICTIVE_WIDTH_MM):
    if not (math.isfinite(restrictive_width) and 0.20 <= restrictive_width <= 0.80):
        raise ValueError("Restrictive width must be from 0.20 to 0.80 mm")
    model = settings["model"]
    x_min, y_min, x_max, y_max = bounds
    header = batch_header(header, settings, bounds)
    travel_feed = int(settings["travel_feed_mm_min"])
    filament_area = math.pi * (FILAMENT_MM / 2) ** 2
    restrictive_area = line_area(restrictive_width)
    lines = [
        "; Generated by generate_bend_test_batch.py",
        f"; Printer {model}; {len(samples)} samples; 60 x 20 mm each; 0.20 mm per layer",
        f"; Batch bounds X{x_min:g}..{x_max:g} Y{y_min:g}..{y_max:g}",
        "; First all restrictive X snakes at Z0.20, then all active Y snakes at Z0.40",
        f"; Restrictive bead width {restrictive_width:.3f} mm; speed {DEFAULT_PRINT_SPEED_MM_S:.3f} mm/s",
        "; Widths below the 0.4 mm nozzle diameter are nominal volume targets",
        f"; Wood filament: nozzle {NOZZLE_TEMP_C} C, bed {BED_TEMP_C} C",
    ]
    for sample in samples:
        lines.append(f"; Sample {sample['number']}: active bead width {sample['width']:.3f} mm; "
                     f"row {sample['row']} column {sample['column']}; "
                     f"center X{sample['cx']:g} Y{sample['cy']:g}")
    lines.extend([header, ""])
    current = None
    retracted = float(settings["initial_retracted_mm"])
    printed_segments = [0, 0]

    def travel(x, y):
        nonlocal current
        lines.append(f"G0 X{x:.3f} Y{y:.3f} F{travel_feed}")
        current = (x, y)

    def extrude(x, y, width, speed, layer_index):
        nonlocal current
        distance = math.dist(current, (x, y))
        amount = distance * line_area(width) / filament_area
        if amount <= 0:
            raise ValueError("A snake path contains a zero-length extrusion")
        lines.append(f"G1 X{x:.3f} Y{y:.3f} E{amount:.5f} F{speed * 60:.0f}")
        current = (x, y)
        printed_segments[layer_index] += 1

    def start_sample(sample, z, start_x, start_y):
        nonlocal retracted
        lines.append(f"G0 Z{z + 0.4:.3f} F720")
        travel(start_x, start_y)
        lines.append(f"G0 Z{z:.3f} F720")
        if retracted:
            lines.append(f"G1 E{retracted:.3f} F1500 ; restore retraction")
            retracted = 0.0

    def finish_sample(is_last, progress):
        nonlocal retracted
        if not is_last:
            lines.append("G1 E-0.800 F2100 ; retract between samples")
            retracted = 0.8
        lines.append(f"M73 P{min(99, progress)} R0")

    if model != "MK3S":
        lines.append("M486 S0 ; begin bend test batch")
    lines.extend([";LAYER_CHANGE", ";Z:0.20", ";HEIGHT:0.20", ";TYPE:Restrictive layer"])
    for i, sample in enumerate(samples):
        lines.append(f"; SAMPLE {sample['number']} restrictive, active width {sample['width']:.3f} mm")
        cx, cy = sample["cx"], sample["cy"]
        y_positions = line_centers(cy, RECTANGLE_Y_MM, restrictive_width)
        first_x = sample["x_min"] + restrictive_width / 2
        last_x = sample["x_min"] + RECTANGLE_X_MM - restrictive_width / 2
        start_sample(sample, LAYER_HEIGHT_MM, first_x, y_positions[0])
        for j, y in enumerate(y_positions):
            start_x, end_x = (first_x, last_x) if j % 2 == 0 else (last_x, first_x)
            if j:
                extrude(start_x, y, restrictive_width, DEFAULT_PRINT_SPEED_MM_S, 0)
            extrude(end_x, y, restrictive_width, DEFAULT_PRINT_SPEED_MM_S, 0)
        finish_sample(False, int((i + 1) * 50 / len(samples)))

    fan = int(settings["fan_after_layer_1"])
    lines.extend([f"M106 S{fan}", ";LAYER_CHANGE", ";Z:0.40", ";HEIGHT:0.20", ";TYPE:Active layer"])
    for i, sample in enumerate(samples):
        width = sample["width"]
        active_area = line_area(width)
        speed = DEFAULT_PRINT_SPEED_MM_S * restrictive_area / active_area
        x_positions = line_centers(sample["cx"], RECTANGLE_X_MM, width)
        actual_pitch = x_positions[1] - x_positions[0]
        lines.append(f"; SAMPLE {sample['number']} active, bead width {width:.3f} mm, "
                     f"center spacing {actual_pitch:.4f} mm, speed {speed:.3f} mm/s")
        first_y = sample["y_min"] + width / 2
        last_y = sample["y_min"] + RECTANGLE_Y_MM - width / 2
        start_sample(sample, 2 * LAYER_HEIGHT_MM, x_positions[0], first_y)
        for j, x in enumerate(x_positions):
            start_y, end_y = (first_y, last_y) if j % 2 == 0 else (last_y, first_y)
            if j:
                extrude(x, start_y, width, speed, 1)
            extrude(x, end_y, width, speed, 1)
        is_last = i == len(samples) - 1
        finish_sample(is_last, 50 + int((i + 1) * 49 / len(samples)))
    lines.extend(["; End bend test batch", footer, ""])
    if any(count <= 0 for count in printed_segments):
        raise ValueError("Missing extrusions in one of the layers")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", type=Path, default=Path("Prusa_XL.txt"),
                        help="one of the three Prusa TXT profiles (default: Prusa_XL.txt)")
    parser.add_argument("--restrictive-width", type=float, default=RESTRICTIVE_WIDTH_MM, metavar="MM",
                        help="restrictive bead width (default: 0.42 mm)")
    parser.add_argument("--active-widths", "--spacings", dest="active_widths", nargs="+", type=float,
                        default=[0.2, 0.3, 0.4, 0.5], metavar="MM",
                        help="active bead widths, 0.20 to 0.80 mm (default: 0.2 0.3 0.4 0.5)")
    parser.add_argument("--columns", type=int, default=2, help="number of columns (default: 2)")
    parser.add_argument("--gap", type=float, default=10.0, metavar="MM",
                        help="clear gap between rectangles, at least 5 mm (default: 10)")
    parser.add_argument("--output", type=Path, help="combined ASCII .gcode path")
    args = parser.parse_args()
    if len(args.active_widths) < 2:
        parser.error("Provide at least two active widths for a batch")
    if not math.isfinite(args.gap):
        parser.error("Gap must be finite")
    try:
        settings, header, footer = read_profile(resolve_profile(args.profile))
        samples, bounds = place_samples(settings, args.active_widths, args.columns, args.gap)
        gcode = generate(settings, header, footer, samples, bounds, args.restrictive_width)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    restrictive_name = f"{args.restrictive_width:.2f}".replace(".", "p")
    suffix = "_".join(f"{w:.2f}".replace(".", "p") for w in args.active_widths)
    output = args.output or Path(__file__).with_name(
        f"bend_test_{settings['model']}_batch_restrictive_{restrictive_name}_active_{suffix}.gcode")
    if output.suffix.lower() != ".gcode":
        parser.error("Output filename must end in .gcode")
    output.write_text(gcode, encoding="utf-8")
    print(f"Wrote {output} with {len(samples)} samples at X{bounds[0]:g}..{bounds[2]:g}, Y{bounds[1]:g}..{bounds[3]:g}")


if __name__ == "__main__":
    main()
