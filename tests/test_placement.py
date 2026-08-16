"""Characterization tests for the placement pipeline.

These assert that placement has not *changed*, not that it is correct. That is
the point: the correct answers were never written down, so the only safe
baseline before extracting a shared placement engine is what the current code
already does. Regenerate with:

    python -m tests.regen_golden

and read the diff. Every changed row is either a fix you meant to make or a
regression you didn't.
"""
import json
from pathlib import Path

import pytest

from tests.placement_cases import OPEN_FINDINGS, case_ids, run_case

GOLDEN = Path(__file__).parent / "golden" / "placement.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    if not GOLDEN.exists():
        pytest.fail(f"{GOLDEN} is missing — run: python -m tests.regen_golden")
    return json.loads(GOLDEN.read_text())


@pytest.mark.parametrize("case_id", case_ids())
def test_placement_matches_golden(case_id: str, golden: dict, tmp_path: Path) -> None:
    assert case_id in golden, (
        f"{case_id} has no recorded answer — a fixture or scenario was added. "
        "Run: python -m tests.regen_golden"
    )
    assert run_case(case_id, tmp_path) == golden[case_id]


def test_golden_has_no_orphan_cases(golden: dict) -> None:
    """A recorded answer for a case that no longer exists means a fixture was
    renamed or deleted without regenerating — the row would then never be
    checked by anything again."""
    assert sorted(golden) == sorted(case_ids())


def test_open_findings_reference_real_fixtures() -> None:
    """Keeps OPEN_FINDINGS honest: a finding pointing at a fixture that no
    longer exists is a note nobody will act on."""
    known = {c.split("|")[0] for c in case_ids()}
    for finding, meta in OPEN_FINDINGS.items():
        missing = set(meta["fixtures"]) - known
        assert not missing, f"{finding} references unknown fixtures: {sorted(missing)}"
