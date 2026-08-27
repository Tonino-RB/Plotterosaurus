"""The uploads folder as an addressable library.

Covers the two things that made the folder a liability: nothing could see what
was in it, and nothing could remove anything from it. The interesting cases are
the ones where a row is not free to delete (a job still points at it) and where
selecting a row has to produce something the rest of the pipeline already knows
how to plot.
"""
from pathlib import Path

import pytest

from app import main, state

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" '
       b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
       b'width="100mm" height="100mm" viewBox="0 0 100 100">'
       b'<g inkscape:groupmode="layer" inkscape:label="art">'
       b'<path fill="none" stroke="#000" d="M10,10 L90,90"/>'
       b'</g></svg>')


@pytest.fixture
def uploaded(client):
    """One real upload, cleaned up afterwards along with anything derived."""
    created: list[str] = []

    def make(name="drawing.svg"):
        res = client.post("/upload", files={"file": (name, SVG, "image/svg+xml")})
        assert res.status_code == 200, res.text
        svg_id = res.json()["id"]
        created.append(svg_id)
        return svg_id

    yield make

    for svg_id in created:
        state.drop_upload_meta(svg_id)
    # Promotions land under an id the test never saw; clear the whole sandbox.
    for leftover in main.UPLOAD_DIR.glob("*.svg"):
        leftover.unlink(missing_ok=True)
    for svg_id in list(state.all_upload_meta()):
        state.drop_upload_meta(svg_id)


def _entry(client, key):
    entries = client.get("/library").json()["entries"]
    return next((e for e in entries if e["key"] == key), None)


# Listing ------------------------------------------------------------------

def test_upload_appears_with_its_real_filename(client, uploaded):
    svg_id = uploaded("Sunset Study.svg")
    entry = _entry(client, svg_id)
    assert entry is not None
    # The point of persisting metadata at all: the file on disk is named after
    # a uuid fragment, so without this the row is unreadable.
    assert entry["filename"] == "Sunset Study.svg"
    assert entry["variant"] == "source"
    assert entry["size_bytes"] > 0
    assert entry["in_use"] is False


def test_a_file_with_no_metadata_still_lists(client, uploaded):
    """Files predating the library have no recorded name. They must still be
    listed and deletable, or the folder they are filling stays unmanageable."""
    orphan = main.UPLOAD_DIR / "deadbeef.svg"
    orphan.write_bytes(SVG)
    try:
        entry = _entry(client, "deadbeef")
        assert entry is not None
        assert entry["filename"] == "deadbeef.svg"   # honest fallback, not invented
    finally:
        orphan.unlink(missing_ok=True)


def test_size_counts_derivatives(client, uploaded):
    """A source row owns its derivatives, so its size has to include them —
    that is what the row reclaims when deleted."""
    svg_id = uploaded()
    (main.UPLOAD_DIR / f"{svg_id}.preview.svg").write_bytes(SVG * 3)
    entry = _entry(client, svg_id)
    assert entry["size_bytes"] > len(SVG) * 3


# Selecting ----------------------------------------------------------------

def test_select_source_returns_a_queueable_payload(client, uploaded):
    svg_id = uploaded()
    res = client.post("/library/select", json={"svg_id": svg_id, "variant": "source"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == svg_id
    assert body["pre_optimized"] is False
    assert [l["label"] for l in body["layers"]] == ["art"]
    assert body["width_mm"] == 100.0


def test_selecting_an_optimized_row_promotes_it_to_its_own_source(client, uploaded):
    svg_id = uploaded()
    (main.UPLOAD_DIR / f"{svg_id}.opt.svg").write_bytes(SVG)

    res = client.post("/library/select", json={"svg_id": svg_id, "variant": "optimized"})
    assert res.status_code == 200, res.text
    body = res.json()

    # A new id, backed by a real file, so nothing downstream needs to know a
    # variant existed.
    assert body["id"] != svg_id
    assert (main.UPLOAD_DIR / f"{body['id']}.svg").is_file()
    assert body["pre_optimized"] is True
    assert "(optimized)" in body["filename"]


def test_promotion_is_reused_not_repeated(client, uploaded):
    """Five replots of an optimized drawing must cost one copy, not five —
    otherwise the feature built to stop the folder growing grows it."""
    svg_id = uploaded()
    (main.UPLOAD_DIR / f"{svg_id}.opt.svg").write_bytes(SVG)

    first = client.post("/library/select",
                        json={"svg_id": svg_id, "variant": "optimized"}).json()["id"]
    for _ in range(4):
        again = client.post("/library/select",
                            json={"svg_id": svg_id, "variant": "optimized"}).json()["id"]
        assert again == first

    promoted = [p for p in main.UPLOAD_DIR.glob("*.svg")
                if "." not in p.name[:-4] and p.stem != svg_id]
    assert len(promoted) == 1


def test_a_pre_optimized_job_cannot_be_optimized_again(client, uploaded):
    """Enforced server-side, so an API client can't queue the double-run the
    UI is careful not to offer."""
    svg_id = uploaded()
    (main.UPLOAD_DIR / f"{svg_id}.opt.svg").write_bytes(SVG)
    promoted = client.post("/library/select",
                           json={"svg_id": svg_id, "variant": "optimized"}).json()

    res = client.post("/jobs", json={
        "svg_id": promoted["id"], "filename": promoted["filename"],
        "pre_optimized": True,
        "optimize_svg": True,          # asking for it anyway
        "layer_selections": [{"index": 0, "label": "art"}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    })
    assert res.status_code == 200, res.text
    job = res.json()
    assert job["optimize_svg"] is False

    patched = client.patch(f"/jobs/{job['job_id']}", json={"optimize_svg": True})
    assert patched.status_code == 200
    assert patched.json()["optimize_svg"] is False
    state.remove_job(job["job_id"])


# Deleting -----------------------------------------------------------------

def test_delete_removes_the_source_and_its_derivatives(client, uploaded):
    svg_id = uploaded()
    (main.UPLOAD_DIR / f"{svg_id}.preview.svg").write_bytes(SVG)

    res = client.request("DELETE", f"/library/{svg_id}")
    assert res.status_code == 200, res.text
    assert not (main.UPLOAD_DIR / f"{svg_id}.svg").exists()
    assert not (main.UPLOAD_DIR / f"{svg_id}.preview.svg").exists()
    assert _entry(client, svg_id) is None


def test_delete_is_refused_while_a_job_uses_it(client, uploaded):
    """Tidying the folder must not be able to destroy a ready job as a side
    effect."""
    svg_id = uploaded()
    job = state.add_job({
        "svg_id": svg_id, "filename": "drawing.svg",
        "layer_selections": [{"index": 0, "label": "art"}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    })
    try:
        res = client.request("DELETE", f"/library/{svg_id}")
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "library_in_use"
        assert (main.UPLOAD_DIR / f"{svg_id}.svg").is_file()
        assert _entry(client, svg_id)["in_use"] is True
    finally:
        state.remove_job(job["job_id"])


def test_deleting_the_optimized_row_leaves_the_source(client, uploaded):
    svg_id = uploaded()
    (main.UPLOAD_DIR / f"{svg_id}.opt.svg").write_bytes(SVG)

    res = client.request("DELETE", f"/library/{svg_id}?variant=optimized")
    assert res.status_code == 200, res.text
    assert not (main.UPLOAD_DIR / f"{svg_id}.opt.svg").exists()
    assert (main.UPLOAD_DIR / f"{svg_id}.svg").is_file()


# Cleaning -----------------------------------------------------------------

def test_clean_removes_the_unused_and_keeps_the_rest(client, uploaded):
    keep = uploaded("keep.svg")
    drop_a = uploaded("drop-a.svg")
    drop_b = uploaded("drop-b.svg")
    job = state.add_job({
        "svg_id": keep, "filename": "keep.svg",
        "layer_selections": [{"index": 0, "label": "art"}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    })
    try:
        res = client.post("/library/clean")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["removed"] == 2
        assert body["kept"] == 1
        assert body["freed_bytes"] > 0

        assert (main.UPLOAD_DIR / f"{keep}.svg").is_file()
        assert not (main.UPLOAD_DIR / f"{drop_a}.svg").exists()
        assert not (main.UPLOAD_DIR / f"{drop_b}.svg").exists()
    finally:
        state.remove_job(job["job_id"])


def test_clean_on_an_empty_library_is_harmless(client, uploaded):
    res = client.post("/library/clean")
    assert res.status_code == 200
    assert res.json()["removed"] >= 0


def test_scratch_files_are_not_library_rows(client, uploaded):
    """A half-written optimize output is transient and belongs to no drawing.
    Splitting its name yields an empty svg_id, which globs as `.*` — that would
    put every hidden file in the folder behind one delete button."""
    svg_id = uploaded()
    scratch = main.UPLOAD_DIR / ".deadbeef.partial.svg"
    scratch.write_bytes(SVG)
    try:
        keys = {e["key"] for e in client.get("/library").json()["entries"]}
        assert keys == {svg_id}
        assert scratch.is_file(), "clean must not treat scratch as a row"
    finally:
        scratch.unlink(missing_ok=True)


# Ready status -------------------------------------------------------------
#
# There is no queue to join and nothing to commit: a job is either `ready` —
# uploaded, fully editable, and plottable the moment it reaches the top of the
# list — or it is running, or it is finished. Plot takes the topmost ready job
# and the run ends there, so the list is an ordering, not a batch.

def _job_payload(svg_id, **over):
    return {"svg_id": svg_id, "filename": "drawing.svg",
            "layer_selections": [{"index": 0, "label": "art"}],
            "paper_width_mm": 210.0, "paper_height_mm": 297.0, **over}


def test_a_new_job_is_ready(client, uploaded):
    svg_id = uploaded()
    job = client.post("/jobs", json=_job_payload(svg_id)).json()
    try:
        assert job["status"] == "ready"
        assert state.next_ready_job()["job_id"] == job["job_id"]
    finally:
        state.remove_job(job["job_id"])


def test_editing_a_job_leaves_it_ready(client, uploaded):
    """Editing is what a ready job is for, so it must not knock the job out of
    the status that makes it plottable."""
    svg_id = uploaded()
    job = client.post("/jobs", json=_job_payload(svg_id)).json()
    try:
        res = client.patch(f"/jobs/{job['job_id']}", json={"margin_top_mm": 12.0})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "ready"
        assert res.json()["margin_top_mm"] == 12.0
    finally:
        state.remove_job(job["job_id"])


def test_editing_a_finished_job_makes_it_ready_again(client, uploaded):
    """The convenience that survives from the old re-queue-on-edit rule: a
    completed job picked back up is runnable again with no extra step."""
    svg_id = uploaded()
    job = client.post("/jobs", json=_job_payload(svg_id)).json()
    try:
        state.update_job(job["job_id"], status="plotting")
        state.update_job(job["job_id"], status="completed")
        res = client.patch(f"/jobs/{job['job_id']}", json={"margin_top_mm": 4.0})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "ready"
    finally:
        state.remove_job(job["job_id"])


def test_the_top_ready_job_is_the_one_that_would_plot(client, uploaded):
    """The whole selection rule: list order, nothing else."""
    first = client.post("/jobs", json=_job_payload(uploaded("a.svg"))).json()
    second = client.post("/jobs", json=_job_payload(uploaded("b.svg"))).json()
    try:
        assert state.next_ready_job()["job_id"] == first["job_id"]
        # Moving the second one to the top makes it the candidate instead.
        client.post(f"/jobs/{second['job_id']}/move", json={"new_index": 0})
        assert state.next_ready_job()["job_id"] == second["job_id"]
        # A job that is not ready is passed over wherever it sits.
        state.update_job(second["job_id"], status="plotting")
        state.update_job(second["job_id"], status="completed")
        assert state.next_ready_job()["job_id"] == first["job_id"]
    finally:
        state.remove_job(first["job_id"])
        state.remove_job(second["job_id"])


def test_requeue_leaves_a_ready_job_alone(client, uploaded):
    """A ready job has nothing to reset — and resetting would throw away the
    estimate the plan queue computed while it sat."""
    svg_id = uploaded()
    job = client.post("/jobs", json=_job_payload(svg_id)).json()
    try:
        state.update_job(job["job_id"], estimated_total_seconds=123.0)
        res = client.post(f"/jobs/{job['job_id']}/requeue")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "ready"
        assert res.json()["estimated_total_seconds"] == 123.0
    finally:
        state.remove_job(job["job_id"])


def test_the_api_creates_ready_jobs_too(client, uploaded):
    """The web upload and an external client now produce the same thing."""
    from app import config
    res = client.post("/api/v1/jobs",
                      files={"file": ("api.svg", SVG, "image/svg+xml")},
                      headers={"X-API-Key": config.API_KEY})
    assert res.status_code == 200, res.text
    job = res.json()
    try:
        assert job["status"] == "ready"
    finally:
        state.remove_job(job["job_id"])


def test_upload_to_plot_ready_end_to_end(client, uploaded):
    """The whole path a drawing takes: dropped, listed, built into a job.

    Written as one test because the value is in the seam between the steps —
    each half passed on its own while the upload was still auto-queueing.
    """
    res = client.post("/upload", files={"file": ("Study.svg", SVG, "image/svg+xml")})
    assert res.status_code == 200, res.text
    svg = res.json()

    # 1. Uploading a drawing on its own creates no job.
    assert state.next_ready_job() is None

    # 2. It is in the library, under the name the user knows it by.
    entry = _entry(client, svg["id"])
    assert entry is not None and entry["filename"] == "Study.svg"
    assert entry["in_use"] is False

    # 3. Building a job from it makes it plottable, and claims the file.
    job = client.post("/jobs", json=_job_payload(svg["id"], filename="Study.svg")).json()
    try:
        assert job["status"] == "ready"
        assert state.next_ready_job()["job_id"] == job["job_id"]
        assert _entry(client, svg["id"])["in_use"] is True
        # ...which is what protects it from being tidied away.
        assert client.request("DELETE", f"/library/{svg['id']}").status_code == 409
        assert client.post("/library/clean").json()["removed"] == 0
    finally:
        state.remove_job(job["job_id"])


def test_removing_a_job_keeps_the_drawing(client, uploaded):
    """The coupling that made the library useless. Deleting a job used to take
    its SVG with it, so a drawing could only be reselected while a job still
    referenced it — tidying the queue silently emptied the library."""
    svg_id = uploaded("keeper.svg")
    job = client.post("/jobs", json=_job_payload(svg_id)).json()

    assert client.request("DELETE", f"/jobs/{job['job_id']}").status_code == 200
    assert (main.UPLOAD_DIR / f"{svg_id}.svg").is_file()

    entry = _entry(client, svg_id)
    assert entry is not None and entry["in_use"] is False
    # And it is genuinely reusable, not just listed.
    again = client.post("/library/select", json={"svg_id": svg_id, "variant": "source"})
    assert again.status_code == 200, again.text
    assert again.json()["filename"] == "keeper.svg"


def test_clean_reclaims_what_job_deletion_now_leaves(client, uploaded):
    """The other half of the trade: disk is reclaimed deliberately, via the
    library, rather than as a side effect of tidying the queue."""
    svg_id = uploaded()
    job = client.post("/jobs", json=_job_payload(svg_id)).json()
    client.request("DELETE", f"/jobs/{job['job_id']}")

    body = client.post("/library/clean").json()
    assert body["removed"] == 1
    assert body["freed_bytes"] > 0
    assert not (main.UPLOAD_DIR / f"{svg_id}.svg").exists()


# Renaming a job renames its drawing ------------------------------------------

def test_renaming_a_job_renames_the_library_row(client, uploaded):
    """A job's title and its source drawing's library name are meant to stay in
    step — renaming the job writes through to the upload metadata."""
    svg_id = uploaded("sketch.svg")
    job = client.post("/jobs", json=_job_payload(svg_id)).json()
    try:
        res = client.patch(f"/jobs/{job['job_id']}", json={"name": "Harbour at dusk"})
        assert res.status_code == 200, res.text
        assert res.json()["name"] == "Harbour at dusk"
        assert _entry(client, svg_id)["filename"] == "Harbour at dusk"
        # And a fresh selection of that row now offers the new name.
        again = client.post("/library/select", json={"svg_id": svg_id})
        assert again.json()["filename"] == "Harbour at dusk"
    finally:
        state.remove_job(job["job_id"])


# layer_mode ---------------------------------------------------------------

WRAPPED_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    b'width="160mm" height="90mm" viewBox="0 0 160 90">'
    b'<g id="wrap" transform="translate(6,3)">'
    b'<g inkscape:label="ridges"><path fill="none" stroke="#111" d="M0,0 L120,0"/></g>'
    b'<g inkscape:label="valleys"><path fill="none" stroke="#111" d="M0,20 L120,20"/></g>'
    b'<g inkscape:label="horizon"><path fill="none" stroke="#111" d="M0,40 L120,40"/></g>'
    b'</g></svg>')


@pytest.fixture
def wrapped(client):
    res = client.post("/upload", files={"file": ("scene.svg", WRAPPED_SVG, "image/svg+xml")})
    assert res.status_code == 200, res.text
    svg_id = res.json()["id"]
    yield svg_id
    for leftover in main.UPLOAD_DIR.glob("*.svg"):
        leftover.unlink(missing_ok=True)
    for sid in list(state.all_upload_meta()):
        state.drop_upload_meta(sid)


def test_group_mode_job_points_at_a_hidden_derived_copy(client, wrapped):
    payload = _job_payload(wrapped, layer_mode="group",
                           layer_selections=[{"index": 0, "label": "wrap"}])
    job = client.post("/jobs", json=payload).json()
    try:
        assert job["layer_mode"] == "group"
        assert job["source_svg_id"] == wrapped
        assert job["svg_id"] != wrapped
        assert [s["label"] for s in job["layer_selections"]] == \
            ["ridges", "valleys", "horizon"]
        meta = state.get_upload_meta(job["svg_id"])
        assert meta["derived_from"] == f"{wrapped}:mode:group"
    finally:
        state.remove_job(job["job_id"])


def test_switching_mode_back_to_layer_restores_the_source(client, wrapped):
    job = client.post("/jobs", json=_job_payload(
        wrapped, layer_mode="group",
        layer_selections=[{"index": 0, "label": "wrap"}])).json()
    try:
        derived = job["svg_id"]
        assert derived != wrapped
        back = client.patch(f"/jobs/{job['job_id']}", json={"layer_mode": "layer"}).json()
        assert back["svg_id"] == wrapped
        assert len(back["layer_selections"]) == 1
        # Flipping back to group reuses the copy rather than making a new one.
        again = client.patch(f"/jobs/{job['job_id']}", json={"layer_mode": "group"}).json()
        assert again["svg_id"] == derived
    finally:
        state.remove_job(job["job_id"])
