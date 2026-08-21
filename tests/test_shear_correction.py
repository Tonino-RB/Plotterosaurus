"""Axis-skew correction for a machine whose two axes aren't square.

The property under test is end-to-end and physical: take the artwork through
transform_to_paper, then push the result through a *model of the machine's own
defect*, and check that what the pen would actually lay down is the square we
asked for. Asserting the emitted transform string instead would only restate
the implementation — the whole point of the correction is what survives the
machine, not what the SVG says.

The machine model here is the standard one for a non-perpendicular gantry
(the same shape as an FDM printer's bed-skew compensation): travelling down
the page drags the carriage sideways, so commanded (x, y) is drawn at
(x + y*tan(skew), y). That is exactly the error the correction inverts.
"""
import math

import pytest
from lxml import etree

from app import svg_utils

SVG_NS = svg_utils.SVG_NS

# The square we ask for, on its page. Deliberately not centred and not
# touching the origin: a correction that shears about the wrong point still
# gets a centred square right, and would go unnoticed.
SIDE = 100.0
CORNERS = [(50.0, 50.0), (150.0, 50.0), (150.0, 150.0), (50.0, 150.0)]

PAGE = dict(
    paper_width_mm=200.0, paper_height_mm=200.0,
    margin_top_mm=0.0, margin_right_mm=0.0,
    margin_bottom_mm=0.0, margin_left_mm=0.0,
    fit_content=False,
)


def _square_svg(path):
    path.write_text(
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'width="200mm" height="200mm" viewBox="0 0 200 200">'
        '<g inkscape:groupmode="layer" inkscape:label="sq">'
        f'<rect x="{CORNERS[0][0]}" y="{CORNERS[0][1]}" '
        f'width="{SIDE}" height="{SIDE}" fill="none" stroke="black"/>'
        "</g></svg>"
    )
    return path


def _mul(p, q):
    a1, b1, c1, d1, e1, f1 = p
    a2, b2, c2, d2, e2, f2 = q
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _parse_transform(text):
    """Reduce an SVG transform list to one affine matrix."""
    import re

    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(\w+)\(([^)]*)\)", text):
        v = [float(x) for x in args.replace(",", " ").split()]
        if name == "translate":
            m = _mul(m, (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0))
        elif name == "scale":
            m = _mul(m, (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0))
        elif name == "rotate":
            r = math.radians(v[0])
            m = _mul(m, (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0))
        elif name == "skewX":
            m = _mul(m, (1, 0, math.tan(math.radians(v[0])), 1, 0, 0))
        elif name == "matrix":
            m = _mul(m, tuple(v))
    return m


def _commanded(tmp_path, shear_deg):
    """The four corner positions the output SVG sends to the machine."""
    src = _square_svg(tmp_path / "src.svg")
    out = tmp_path / "out.svg"
    svg_utils.transform_to_paper(src, out, **PAGE, shear_deg=shear_deg)

    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    node = etree.parse(str(out)).getroot()
    while (g := node.find(f"{{{SVG_NS}}}g")) is not None:
        if g.get("transform"):
            m = _mul(m, _parse_transform(g.get("transform")))
        node = g
    a, b, c, d, e, f = m
    return [(a * x + c * y + e, b * x + d * y + f) for x, y in CORNERS]


def _drawn(points, machine_skew_deg):
    """What the machine actually puts on paper, given its own skew."""
    t = math.tan(math.radians(machine_skew_deg))
    return [(x + y * t, y) for x, y in points]


def _close(v):
    return pytest.approx(v, abs=1e-9)


def _diagonals(pts):
    return math.dist(pts[0], pts[2]), math.dist(pts[1], pts[3])


def _edges(pts):
    return [math.dist(pts[i], pts[(i + 1) % 4]) for i in range(4)]


# The correction is invisible until it is asked for ------------------------

def test_zero_shear_is_byte_identical(tmp_path):
    """An uncalibrated machine must produce exactly the SVG it always did —
    this is what keeps the golden placement corpus meaningful."""
    src = _square_svg(tmp_path / "src.svg")
    without = tmp_path / "without.svg"
    zero = tmp_path / "zero.svg"
    svg_utils.transform_to_paper(src, without, **PAGE)
    svg_utils.transform_to_paper(src, zero, **PAGE, shear_deg=0.0)
    assert without.read_bytes() == zero.read_bytes()


def test_zero_shear_adds_no_wrapper(tmp_path):
    """The placement group stays the first <g> under the root, where
    tests/placement_cases.py and anything else reading the output expects it."""
    out = tmp_path / "out.svg"
    svg_utils.transform_to_paper(_square_svg(tmp_path / "src.svg"), out,
                                 **PAGE, shear_deg=0.0)
    first = etree.parse(str(out)).getroot().find(f"{{{SVG_NS}}}g")
    assert first.get("transform").startswith("translate(")


# What the machine actually draws -----------------------------------------

def test_uncorrected_skew_shows_up_as_unequal_diagonals(tmp_path):
    """The symptom being fixed: a real square, drawn by a skewed machine,
    comes off the bed as a parallelogram."""
    drawn = _drawn(_commanded(tmp_path, 0.0), 0.4)
    d1, d2 = _diagonals(drawn)
    assert abs(d1 - d2) > 0.5


def test_correction_makes_the_drawn_square_square(tmp_path):
    """Same machine, same defect, correction on: equal diagonals and four
    equal sides — the user's own acceptance test, in code."""
    skew = 0.4
    drawn = _drawn(_commanded(tmp_path, skew), skew)
    d1, d2 = _diagonals(drawn)
    assert d1 == _close(d2)
    assert max(_edges(drawn)) == _close(min(_edges(drawn)))


def test_correction_keeps_the_artwork_where_the_preview_promised(tmp_path):
    """Shearing about the paper origin — the carriage's own zero — cancels the
    machine's error in position as well as in shape. A correction applied
    about any other point would come out square but shifted, and the plot
    would no longer match the on-screen placement it was never allowed to
    change."""
    skew = 0.4
    drawn = _drawn(_commanded(tmp_path, skew), skew)
    for got, want in zip(drawn, CORNERS):
        assert math.dist(got, want) == _close(0.0)


def test_opposite_edges_stay_parallel(tmp_path):
    """A shear is affine, so parallel lines survive it. Worth stating: it is
    the property that lets this be a whole-document correction rather than
    something that has to understand the artwork."""
    drawn = _drawn(_commanded(tmp_path, 0.4), 0.4)

    def direction(p, q):
        dx, dy = q[0] - p[0], q[1] - p[1]
        n = math.hypot(dx, dy)
        return (dx / n, dy / n)

    top = direction(drawn[0], drawn[1])
    bottom = direction(drawn[3], drawn[2])
    left = direction(drawn[0], drawn[3])
    right = direction(drawn[1], drawn[2])
    assert top[0] == _close(bottom[0]) and top[1] == _close(bottom[1])
    assert left[0] == _close(right[0]) and left[1] == _close(right[1])


# The angle the settings helper computes ----------------------------------

def test_diagonals_recover_the_skew_angle(tmp_path):
    """The formula behind the "work it out from a test square" box in
    Settings: tan(skew) = (d1^2 - d2^2) / 4L^2, with d1 the top-left to
    bottom-right diagonal. If this drifts from the UI, a user calibrating
    from a plotted square gets a wrong angle and no way to tell."""
    for skew in (0.15, 0.4, -0.6, 1.25):
        d1, d2 = _diagonals(_drawn(_commanded(tmp_path, 0.0), skew))
        recovered = math.degrees(math.atan((d1 * d1 - d2 * d2) / (4 * SIDE * SIDE)))
        assert recovered == _close(skew)
