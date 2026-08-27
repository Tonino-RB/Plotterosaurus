"""The calibration/ folder as a job source, decoupled from the uploads library.

Selecting a calibration file promotes it into the uploads folder under a
fresh svg_id (see main._promote_calibration_file), so the normal job pipeline
can plot it exactly like any other drawing. The interesting cases are the
ones that check the promise made to the user: the original file in
calibration/ is never written to, and it stays out of /library/clean's reach
regardless of what happens to the promoted copy.
"""
import pytest

from app import config, main, state

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" '
       b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
       b'width="50mm" height="50mm" viewBox="0 0 50 50">'
       b'<g inkscape:groupmode="layer" inkscape:label="marks">'
       b'<path fill="none" stroke="#000" d="M5,5 L45,45"/>'
       b'</g></svg>')


@pytest.fixture
def calibration_file(tmp_path, monkeypatch):
    """One real calibration SVG in a throwaway directory — never the user's
    real calibration/ folder."""
    monkeypatch.setattr(config, "CALIBRATION_DIR", tmp_path)

    def make(name="test-calibration.svg"):
        (tmp_path / name).write_bytes(SVG)
        return name

    yield make

    # Any promoted copy this test made is tagged "calibration:<filename>" —
    # sweep those out so they don't leak into other tests' library listings.
    for svg_id, info in list(state.all_upload_meta().items()):
        if str(info.get("derived_from") or "").startswith("calibration:"):
            for p in main.UPLOAD_DIR.glob(f"{svg_id}.*"):
                p.unlink(missing_ok=True)
            state.drop_upload_meta(svg_id)


def _entry(client, svg_id):
    entries = client.get("/library").json()["entries"]
    return next((e for e in entries if e["key"] == svg_id), None)


def test_select_returns_a_queueable_payload(client, calibration_file):
    name = calibration_file()
    res = client.post("/calibration/select", json={"filename": name})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [l["label"] for l in body["layers"]] == ["marks"]
    assert body["width_mm"] == 50.0
    assert body["pre_optimized"] is False


def test_selection_is_reused_not_repeated(client, calibration_file):
    """Repeatedly picking the same calibration file for a quick test-and-adjust
    loop must cost one copy, not one per click."""
    name = calibration_file()
    first = client.post("/calibration/select", json={"filename": name}).json()["id"]
    for _ in range(4):
        again = client.post("/calibration/select", json={"filename": name}).json()["id"]
        assert again == first


def test_the_promoted_copy_is_a_normal_library_row(client, calibration_file):
    """It has to look like any other upload to the rest of the pipeline — the
    frontend is what hides it from the Library tab (its derived_from is
    truthy), not the API."""
    name = calibration_file()
    svg_id = client.post("/calibration/select", json={"filename": name}).json()["id"]
    entry = _entry(client, svg_id)
    assert entry is not None
    assert entry["derived_from"] == f"calibration:{name}"


def test_the_original_file_survives_selection_and_clean(client, calibration_file):
    name = calibration_file()
    client.post("/calibration/select", json={"filename": name})
    assert (config.CALIBRATION_DIR / name).read_bytes() == SVG

    res = client.post("/library/clean")
    assert res.status_code == 200, res.text
    # The promoted copy was unused (no job referenced it), so clean reclaims
    # it — but the file it was copied from is untouched either way.
    assert (config.CALIBRATION_DIR / name).read_bytes() == SVG


def test_a_job_protects_the_promoted_copy_from_clean(client, calibration_file):
    name = calibration_file()
    svg = client.post("/calibration/select", json={"filename": name}).json()
    res = client.post("/jobs", json={
        "svg_id": svg["id"], "filename": svg["filename"],
        "layer_selections": [{"index": 0, "label": "marks"}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    })
    assert res.status_code == 200, res.text
    job_id = res.json()["job_id"]
    try:
        client.post("/library/clean")
        assert (main.UPLOAD_DIR / f"{svg['id']}.svg").is_file()
        assert (config.CALIBRATION_DIR / name).is_file()
    finally:
        state.remove_job(job_id)


def test_unknown_file_is_404(client, calibration_file):
    calibration_file()  # sandboxes CALIBRATION_DIR for this test
    res = client.post("/calibration/select", json={"filename": "nope.svg"})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "calibration_file_not_found"


def test_path_traversal_filename_is_rejected(client, calibration_file):
    calibration_file()
    res = client.post("/calibration/select", json={"filename": "../evil.svg"})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_calibration_filename"
