"""Rewrite tests/golden/placement.json from the current behaviour of the code.

Run after a deliberate change to placement, then read the diff: every changed
row is either the fix you meant to make or a regression you didn't. Never run
it to make a red test go green without reading what moved.
"""
import json
import tempfile
from pathlib import Path

from tests.placement_cases import case_ids, run_case

GOLDEN = Path(__file__).parent / "golden" / "placement.json"


def main() -> None:
    out = {}
    ids = case_ids()
    for n, case_id in enumerate(ids, 1):
        with tempfile.TemporaryDirectory() as tmp:
            out[case_id] = run_case(case_id, Path(tmp))
        print(f"  [{n:3d}/{len(ids)}] {case_id}")
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {len(out)} cases to {GOLDEN}")


if __name__ == "__main__":
    main()
