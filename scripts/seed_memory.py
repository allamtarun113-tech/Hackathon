"""Load the 61 past incidents from data/incidents_history.json into Hindsight.

    python scripts/seed_memory.py            # create the bank and retain missing incidents
    python scripts/seed_memory.py --reset    # delete the bank first, then reseed
    python scripts/seed_memory.py --preview  # print one memory, send nothing

Safe to re-run: incidents already in the bank are skipped.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pagermind import data  # noqa: E402
from pagermind.config import get_config  # noqa: E402
from pagermind.memory import PagerMindMemory, incident_to_memory  # noqa: E402


def bank_background() -> str:
    c = data.catalog()
    services = "; ".join(f"{n} ({s['runtime']}): {s['description']}" for n, s in c["services"].items())
    jobs = "; ".join(f"{n} runs {j['schedule']}" for n, j in c["scheduled_jobs"].items())
    return f"{c['about']} Services: {services}. Scheduled jobs: {jobs}."


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()
    incidents = data.history()

    if args.preview:
        print(incident_to_memory(incidents[0])["content"])
        return

    mem = PagerMindMemory(get_config())
    if args.reset:
        from hindsight_client import Hindsight
        with Hindsight(base_url=mem.cfg.hindsight_base_url, api_key=mem.cfg.hindsight_api_key) as c:
            try:
                c.delete_bank(mem.bank)
                print(f"Deleted bank {mem.bank}")
            except Exception:
                pass
    mem.setup_bank(bank_background())
    stored = mem.stored_incident_ids()
    todo = [i for i in incidents if i["id"] not in stored]
    print(f"Bank {mem.bank}: {len(stored)} documents stored, {len(todo)} incidents to retain")
    for i in range(0, len(todo), 4):
        batch = todo[i:i + 4]
        mem.retain_incidents(batch)
        print(f"  retained {', '.join(b['id'] for b in batch)}")
    print("Done.")


if __name__ == "__main__":
    main()
