"""The placement corpus: fixtures x scenarios, and how one case is run.

Every case drives the same sequence the app drives on a real upload-and-plot
(normalize -> parse -> filter -> transform -> measure), so the recorded answer
is the one the machine would actually get, not the output of a function called
in isolation.

The recorded answer is deliberately the *whole* placement result — page size,
the <g transform> string, and the ink rectangle — because those are the three
things the engine extraction has to keep identical. See tests/README.md.
"""
import shutil
from pathlib import Path

from lxml import etree

from app import svg_utils

FIXTURES = Path(__file__).parent / "fixtures"

# Rounding for the snapshot. 6 decimals on the transform keeps every digit
# that could move a pen; 3 on millimetre bounds is a micron, which is far
# below anything a plotter can resolve. Both exist to stop a last-bit float
# difference from failing a test that found no real change.
_TRANSFORM_DP = 6
_MM_DP = 3


# Scenarios ----------------------------------------------------------------
#
# Curated rather than a full cross-product: each one turns on a different part
# of the placement math, and the set stays small enough that the golden file
# is reviewable as a table.

SCENARIOS: list[dict] = [
    {"id": "a4p-plain",
     "paper": (210, 297)},
    {"id": "a4p-margins",
     "paper": (210, 297), "margins": (10, 15, 20, 5)},
    {"id": "a4p-fit",
     "paper": (210, 297), "fit": True},
    {"id": "a4l-fit",
     "paper": (297, 210), "fit": True},
    {"id": "a4p-rot90",
     "paper": (210, 297), "rot": 90.0},
    {"id": "a4p-rot45-fit",
     "paper": (210, 297), "rot": 45.0, "fit": True},
    {"id": "a4p-scale-offset",
     "paper": (210, 297), "scale": 0.5, "offset": (20, -10)},
    {"id": "a5p-fit-margins",
     "paper": (148, 210), "margins": (8, 8, 8, 8), "fit": True},
    # Auto-rotate policies. The paper is handed in already swapped by the
    # caller in the real app; what these pin down is the extra artwork
    # rotation the policy adds on top.
    {"id": "portraitbed-autorotate",
     "paper": (297, 420), "auto_rotate": "portrait"},
    {"id": "landscapebed-autorotate",
     "paper": (420, 297), "auto_rotate": "landscape"},
]


def _scenario(s: dict) -> dict:
    """Fill a scenario's defaults so cases read as just their differences."""
    return {
        "paper": s["paper"],
        "margins": s.get("margins", (0.0, 0.0, 0.0, 0.0)),
        "fit": s.get("fit", False),
        "scale": s.get("scale", 1.0),
        "rot": s.get("rot", 0.0),
        "offset": s.get("offset", (0.0, 0.0)),
        "auto_rotate": s.get("auto_rotate", "off"),
    }


def case_ids() -> list[str]:
    fixtures = sorted(p.name for p in FIXTURES.glob("*.svg"))
    return [f"{f[:-4]}|{s['id']}" for f in fixtures for s in SCENARIOS]


def _round_numbers(text: str, dp: int) -> str:
    """Round every number in a transform string, so an insignificant float
    difference doesn't read as a placement change."""
    import re

    def fix(m: "re.Match[str]") -> str:
        return f"{round(float(m.group()), dp):g}"

    return re.sub(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", fix, text)


def run_case(case_id: str, workdir: Path) -> dict:
    """Run one fixture through one scenario and return the recorded answer."""
    fixture_name, scenario_id = case_id.split("|")
    scenario = _scenario(next(s for s in SCENARIOS if s["id"] == scenario_id))

    # Copy first: normalize_layer_structure rewrites its input in place, and
    # the fixtures are the one thing in here that must never change.
    src = workdir / f"{fixture_name}.svg"
    shutil.copy(FIXTURES / f"{fixture_name}.svg", src)

    normalized = svg_utils.normalize_layer_structure(src)
    info = svg_utils.parse_layers(src)
    indices = [layer["index"] for layer in info["layers"]]

    paper_w, paper_h = scenario["paper"]
    mt, mr, mb, ml = scenario["margins"]
    off_x, off_y = scenario["offset"]
    placement = dict(
        paper_width_mm=paper_w, paper_height_mm=paper_h,
        margin_top_mm=mt, margin_right_mm=mr,
        margin_bottom_mm=mb, margin_left_mm=ml,
        fit_content=scenario["fit"],
        transform_scale=scenario["scale"],
        transform_rotation_deg=scenario["rot"],
        transform_offset_x_mm=off_x, transform_offset_y_mm=off_y,
        machine_custom_enabled=True,
        machine_auto_rotate=scenario["auto_rotate"],
    )

    result: dict = {
        "layers": [layer["label"] for layer in info["layers"]],
        "normalized": normalized,
        "size_mm": [
            None if info["width_mm"] is None else round(info["width_mm"], _MM_DP),
            None if info["height_mm"] is None else round(info["height_mm"], _MM_DP),
        ],
    }

    filtered = workdir / "filtered.svg"
    placed = workdir / "placed.svg"
    svg_utils.filter_to_layers(src, indices, filtered)
    svg_utils.transform_to_paper(filtered, placed, **placement)

    root = etree.parse(str(placed)).getroot()
    result["page"] = [root.get("width"), root.get("height"), root.get("viewBox")]
    group = root.find(f"{{{svg_utils.SVG_NS}}}g")
    result["transform"] = (
        None if group is None else _round_numbers(group.get("transform", ""), _TRANSFORM_DP)
    )

    bounds = svg_utils.ink_bounds_mm(src, indices, **placement)
    result["ink_bounds_mm"] = (
        None if bounds is None else [round(float(v), _MM_DP) for v in bounds]
    )
    return result


# Open findings this corpus demonstrates -----------------------------------
#
# Not asserted — documentation for whoever reviews the next golden diff. When
# a finding is fixed, the listed fixtures are the rows expected to change; a
# diff touching anything else means the fix reached further than intended.

OPEN_FINDINGS: dict[str, dict] = {
    "A6": {
        "summary": "canvas crop is only applied when Optimize SVG happens to be on; "
                   "ink_bounds_mm always measures cropped, transform_to_paper never crops",
        "fixtures": ["ink-overflows-canvas"],
    },
    "A7": {
        "summary": "a document with nothing plottable is accepted and plots nothing; "
                   "ink_bounds_mm returns None and no warning is raised",
        "fixtures": ["text-only"],
    },
    "A9": {
        "summary": "square and near-square artwork takes a pointless 90-degree "
                   "auto-rotation, because content_landscape is a strict >",
        "fixtures": ["square", "near-square"],
    },
}
