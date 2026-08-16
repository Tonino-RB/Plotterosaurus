"""The suite, pointed at your own drawings.

Skipped unless $PLOTTEROSAURUS_REAL_SVGS names a directory of SVGs:

    PLOTTEROSAURUS_REAL_SVGS=~/plots venv/bin/python -m pytest tests/test_real_svgs.py -v

Everything in `fixtures/` was written by someone who already knew which
property they were testing, which is exactly why those files keep missing
things. Real exports carry what no fixture author thinks to type: CSS classes
and <style> blocks, <use> and <defs>, clip paths, nested transforms three deep,
embedded rasters, `width="100%"`, sixteen digits of coordinate precision,
1.4-million-character path data, layers that vpype merges because their labels
collide on a number.

These assert properties rather than values — nothing here knows what your
drawing looks like, only what must be true of any placement of it. Add a file
here whenever one surprises the app, and it becomes a permanent guard without
anything private entering the repository.
"""
import time

import pytest

from app import config, main, svg_utils
# main.UPLOAD_DIR is read through the module, never from-imported: the
# sandbox fixture in conftest.py rebinds it (see _sandbox_server_state).

from .conftest import REAL_SVG_ENV, real_svgs

REAL = real_svgs()

pytestmark = [
    pytest.mark.real,
    pytest.mark.skipif(
        not REAL,
        reason=f"put SVGs in tests/real/ (or set ${REAL_SVG_ENV}) to run these"),
]

A4 = {"paper_width_mm": 210.0, "paper_height_mm": 297.0}


@pytest.fixture(params=REAL, ids=lambda p: p.name)
def real_job(request, tmp_path, job_from_svg):
    """One queued job per real drawing, with its layers parsed the way an
    upload parses them rather than assumed.

    Normalized *before* the job exists, not after: the endpoint falls back to
    the job's stored selections, so layers discovered later than the record
    never reach it. Getting that wrong made this suite compare all four layers
    against the one the server was actually asked about.
    """
    source = request.param
    staged = tmp_path / source.name
    staged.write_bytes(source.read_bytes())
    # Mirror the upload flow: uploads are normalized before their layers are
    # read, and on real files that is not a no-op — it rescues content sitting
    # outside any layer and splits groups vpype would otherwise merge.
    svg_utils.normalize_layer_structure(staged)
    info = svg_utils.parse_layers(staged)
    layers = [{"index": layer["index"], "label": layer["label"], "selected": True}
              for layer in info["layers"]]
    return job_from_svg(staged, layers=layers)


def test_placement_is_answerable(client, real_job):
    """Nothing exotic in the markup should stop the server describing where
    the artwork lands. A 500 here is a parser this file has never seen."""
    res = client.post(f"/jobs/{real_job['job_id']}/placement", json=A4)
    assert res.status_code == 200, res.text
    body = res.json()
    for field in ("layout_width_mm", "layout_height_mm",
                  "footprint_width_mm", "footprint_height_mm"):
        assert body[field] > 0, f"{field} is {body[field]}"
    assert body["fit_scale"] > 0


def test_the_preview_never_waits_on_vpype(client, real_job):
    """The regression, restated against the files that exposed it. Budget is
    1s for the first request — the ones that broke the app took seven."""
    start = time.perf_counter()
    client.post(f"/jobs/{real_job['job_id']}/placement", json=A4)
    cold = time.perf_counter() - start
    assert cold < 1.0, f"first placement took {cold:.2f}s"

    start = time.perf_counter()
    client.post(f"/jobs/{real_job['job_id']}/placement",
                json={**A4, "transform_scale": 0.9})
    warm = time.perf_counter() - start
    assert warm < 0.100, f"subsequent placement took {warm * 1000:.0f}ms"


@pytest.mark.parametrize("overrides", [
    {},
    {"fit_content": True},
    {"fit_content": True, "transform_rotation_deg": 30.0},
    {"transform_scale": 0.4, "transform_offset_x_mm": 12.0},
])
def test_preview_agrees_with_the_plot(client, real_job, overrides):
    """The reason the server owns placement at all: the browser can only
    measure with getBBox(), which counts live text and raster images the pen
    never draws. This asserts the endpoint's answer is the same one the plot
    pipeline computes — on real markup, where the two have room to disagree.
    """
    query = {**A4, **overrides, "include_ink": True}
    body = client.post(f"/jobs/{real_job['job_id']}/placement", json=query).json()

    indices = [layer["index"] for layer in real_job["layer_selections"]]
    expected = svg_utils.ink_bounds_mm(
        main.UPLOAD_DIR / f"{real_job['svg_id']}.svg", indices,
        query["paper_width_mm"], query["paper_height_mm"], 0.0, 0.0, 0.0, 0.0,
        query.get("fit_content", False),
        transform_scale=query.get("transform_scale", 1.0),
        transform_rotation_deg=query.get("transform_rotation_deg", 0.0),
        transform_offset_x_mm=query.get("transform_offset_x_mm", 0.0),
        transform_offset_y_mm=query.get("transform_offset_y_mm", 0.0),
        machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
    )
    if expected is None:
        assert body["ink"] is None       # nothing plottable; both agree on that
        return

    ink = body["ink"]
    assert ink is not None, "the plot finds ink here and the preview does not"
    got = (ink["left_mm"], ink["top_mm"],
           ink["left_mm"] + ink["width_mm"], ink["top_mm"] + ink["height_mm"])
    assert got == pytest.approx(tuple(float(v) for v in expected))


def test_layers_survive_a_round_trip(client, real_job):
    """A6/A7 territory. Layers that vpype merges, or content that lands in no
    layer at all, show up here as a selection the plot cannot honour."""
    indices = [layer["index"] for layer in real_job["layer_selections"]]
    assert indices == sorted(set(indices)), "duplicate or unordered layer indices"

    whole = client.post(f"/jobs/{real_job['job_id']}/placement",
                        json={**A4, "include_ink": True}).json()["ink"]
    if whole is None:
        pytest.skip("nothing plottable in this document")

    # Every layer that reports ink must fit inside the ink of all of them.
    for index in indices:
        one = client.post(f"/jobs/{real_job['job_id']}/placement",
                          json={**A4, "include_ink": True,
                                "layer_indices": [index]}).json()["ink"]
        if one is None:
            continue
        assert one["left_mm"] >= whole["left_mm"] - 0.01
        assert one["top_mm"] >= whole["top_mm"] - 0.01
        assert (one["left_mm"] + one["width_mm"]
                <= whole["left_mm"] + whole["width_mm"] + 0.01)
        assert (one["top_mm"] + one["height_mm"]
                <= whole["top_mm"] + whole["height_mm"] + 0.01)
