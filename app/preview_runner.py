"""Subprocess entry point for running a pyaxidraw preview.

Invoked by plot_worker so the preview can be SIGTERM'd if the user cancels
during the planning phase. Prints a single JSON line on success; exits
non-zero on error (stderr carries the exception message).

Takes the SVG path and a JSON blob of driver options (see
plot_worker._run_preview). The blob exists so this stays a dumb executor: it
runs as a bare script with no package context, so it can't import config or
resolve the model-to-travel-param mapping itself, and duplicating either here
is exactly how the estimate drifted away from the plot in the first place.
"""
import json
import sys

from pyaxidraw import axidraw


def main() -> int:
    svg_path = sys.argv[1]
    options = json.loads(sys.argv[2])

    ad = axidraw.AxiDraw()
    ad.plot_setup(svg_path)
    ad.options.mode = "plot"
    ad.options.preview = True
    ad.options.model = options["model"]
    # Mirror plot_worker._run_stage exactly. An estimate simulated under
    # different settings than the plot measures a different plot:
    #
    #  - no_rotate: pyaxidraw auto-rotates any document taller than wide.
    #    _run_stage disables that (transform_to_paper has already baked in the
    #    final orientation), so leaving it on here simulates a sideways plot
    #    and misreports travel between home and the artwork.
    #  - travel params: the driver clips pen-down moves to the selected
    #    model's bounds. _run_stage overwrites those with the active machine's
    #    real bed, so without the same override the simulation clips against a
    #    machine the user doesn't own — on a bed smaller than the model, that
    #    counted ink the plotter will never lay down.
    ad.options.no_rotate = True
    x_attr, y_attr = options["travel_params"]
    bed_x_in, bed_y_in = options["travel_in"]
    setattr(ad.params, x_attr, bed_x_in)
    setattr(ad.params, y_attr, bed_y_in)
    ad.options.speed_pendown = options["speed_pendown"]
    ad.options.speed_penup = options["speed_penup"]
    ad.options.accel = options["acceleration"]
    ad.plot_run()

    pen_lifts = 0
    if hasattr(ad, "pen") and hasattr(ad.pen, "status") and hasattr(ad.pen.status, "lifts"):
        pen_lifts = int(ad.pen.status.lifts)

    result = {
        "estimated_total_seconds": float(getattr(ad, "time_estimate", 0.0)),
        "distance_pendown_m": float(getattr(ad, "distance_pendown", 0.0)),
        "distance_total_m": float(getattr(ad, "distance_total", 0.0)),
        "pen_lifts": pen_lifts,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
