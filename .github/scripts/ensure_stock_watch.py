#!/usr/bin/env python3
"""Restart the stock workflow if it is enabled and no run is active or queued."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone

WORKFLOW_PATH = ".github/workflows/stock-watch.yml"
MIN_START_INTERVAL = 600  # a broken setup must not create a rapid restart loop


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def ensure_running(repo, now=None):
    now = now or datetime.now(timezone.utc)
    workflows = json.loads(gh("api", f"repos/{repo}/actions/workflows"))["workflows"]
    workflow = next((w for w in workflows if w.get("path") == WORKFLOW_PATH), None)
    if workflow is None or workflow.get("state") != "active":
        print("Stock Watch is disabled or missing; leaving it stopped.")
        return 0
    runs = json.loads(gh("api", f"repos/{repo}/actions/workflows/{workflow['id']}/runs?branch=main&per_page=50"))["workflow_runs"]
    if any(run.get("status") != "completed" for run in runs):
        print("Stock Watch has an active or queued run; no additional run is needed.")
        return 0
    starts = [datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in runs]
    if starts and (now - max(starts)).total_seconds() < MIN_START_INTERVAL:
        print("The last run started less than 10 minutes ago; the scheduled watchdog will retry.")
        return 0
    for attempt in range(3):
        try:
            gh("workflow", "run", "stock-watch.yml", "--repo", repo, "--ref", "main")
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
        else:
            print("Started Stock Watch on main.")
            return 0


if __name__ == "__main__":
    raise SystemExit(ensure_running(os.environ["GH_REPO"]))
