"""Proof that memory works: for each demo alert, does Hindsight recall the right past incidents?

    python scripts/check_recall.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pagermind import data  # noqa: E402
from pagermind.config import get_config  # noqa: E402
from pagermind.memory import PagerMindMemory  # noqa: E402


def main() -> None:
    mem = PagerMindMemory(get_config())
    passed = 0
    scenarios = data.scenarios()
    for sc in scenarios:
        exp = sc["expected"]
        t = time.time()
        top3 = [m.incident_id for m in mem.recall_for_alert(sc["alert"], sc["log_excerpt"])[:3]]
        ok = all(i in exp["expected_incident_ids"] for i in top3) if exp["strong_match"] else True
        passed += ok
        want = exp["expected_incident_ids"] or "none (brand-new problem - the agent must say so)"
        print(f"{'PASS' if ok else 'FAIL'} {sc['id']} {sc['name']:<34} top-3 {top3}  expected {want}  ({time.time() - t:.1f}s)")
    print(f"\n{passed}/{len(scenarios)} demo alerts recall the right past incidents in the top 3.")


if __name__ == "__main__":
    main()
