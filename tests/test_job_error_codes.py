"""Every job failure carries a key the card can translate.

Web-facing HTTP errors have gone through main._coded's {code, params} pair for
a while, and the browser localizes them. A message written onto the job record
took a different path: state.update_job(status="failed", error="…") stored an
English sentence and the card rendered it verbatim with textContent, so no
language ever saw a translation — at the one moment a user most needs to read
it, a refused plot or a stranded job.

These tests pin the two halves that can drift apart silently: the codes the
backend emits, and the joberror.* keys the catalogs carry.
"""
import ast
import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
I18N = Path(__file__).resolve().parent.parent / "static" / "i18n"


def _en() -> dict:
    return json.loads((I18N / "en.json").read_text())


def _emitted_codes() -> set[str]:
    """Every error code the backend can write onto a job.

    Read out of the source rather than by exercising every failure path: most
    of them need a plotter that is unplugged, a full disk, or a document too
    complex to plan.
    """
    codes: set[str] = set()

    src = (APP / "plot_worker.py").read_text()
    # plot_worker._fail(job_id, message, code, ...) — the code is arg 3.
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_fail" and len(node.args) >= 3
                and isinstance(node.args[2], ast.Constant)):
            codes.add(node.args[2].value)
    # _STOPPED_CODES = {101: "plot_connect_failed", ...}, plus the fallback
    # _stopped_code() returns for a stop code with no entry.
    codes |= set(re.findall(r'"(\w+)"',
                            re.search(r"_STOPPED_CODES = \{(.*?)\}", src, re.S).group(1)))
    codes.add("plot_stopped_unexpectedly")

    # state.py's rehydration messages assign the field directly.
    codes |= set(re.findall(r'job\["error_code"\] = "(\w+)"',
                            (APP / "state.py").read_text()))
    return codes


def test_every_emitted_error_code_has_a_message():
    en = _en()
    missing = sorted(c for c in _emitted_codes() if f"joberror.{c}" not in en)
    assert not missing, (
        "these job error codes have no joberror.* message, so the card falls "
        f"back to English: {missing}")


def test_the_worker_writes_no_uncoded_failure():
    """A bare update_job(status="failed", error=…) is the shape this was: an
    English sentence with nothing for the browser to translate. Failures go
    through _fail so the code travels with the message."""
    src = (APP / "plot_worker.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update_job"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        err = kw.get("error")
        # error=None is a run clearing the last failure, not raising one.
        if err is None or (isinstance(err, ast.Constant) and err.value is None):
            continue
        if "error_code" not in kw:
            offenders.append(node.lineno)
    assert not offenders, (
        "plot_worker.py sets a job error without a code at line(s) "
        f"{offenders} — use _fail() so the card can translate it")


def test_stopped_codes_cover_the_messages_they_pair_with():
    """_STOPPED_MESSAGES and _STOPPED_CODES describe the same set of plotter
    stop codes; a message added to one and not the other reaches the card in
    English while every other stop reason is translated."""
    src = (APP / "plot_worker.py").read_text()
    msgs = set(re.findall(r"^\s*(\d+): ", re.search(
        r"_STOPPED_MESSAGES = \{(.*?)\n\}", src, re.S).group(1), re.M))
    codes = set(re.findall(r"(\d+): \"", re.search(
        r"_STOPPED_CODES = \{(.*?)\}", src, re.S).group(1)))
    assert msgs == codes, f"_STOPPED_MESSAGES {msgs} vs _STOPPED_CODES {codes}"


def test_a_new_job_carries_the_error_fields():
    """The card reads job.error_code off the record, so the field has to exist
    on every job rather than appearing only once one has failed."""
    from app import state
    job = state.add_job({"svg_id": "deadbeef", "filename": "drawing.svg"})
    try:
        assert "error_code" in job and "error_params" in job
        assert job["error_code"] is None and job["error_params"] is None
    finally:
        state.remove_job(job["job_id"])
