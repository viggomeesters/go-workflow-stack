#!/usr/bin/env python3
"""Run deterministic diverse-repository pilots and compare recorded metrics."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli" / "go.py"
SPECS = ROOT / "fixtures" / "pilots"
RECORDED = ROOT / "docs" / "pilot-metrics.json"


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)


def pilot(spec_path: Path, workspace: Path) -> dict[str, object]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    repo = workspace / spec["id"]
    repo.mkdir()
    seed = repo / spec["seed_file"]
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text(spec["seed_content"], encoding="utf-8")
    run("git", "init", "-q", "-b", "main", str(repo))
    run("git", "add", ".", cwd=repo)
    seeded = run("git", "-c", "user.name=Pilot", "-c", "user.email=pilot@example.com", "commit", "-m", "seed existing project", "-q", cwd=repo)
    if seeded.returncode != 0:
        raise RuntimeError(seeded.stderr)
    before_head = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    command = [
        sys.executable, str(CLI), "spike", str(repo),
        "--project-id", spec["id"], "--name", spec["name"], "--brief", spec["brief"],
        "--execution-mode", "mechanical", "--verification", "git diff --check", "--skip-repo-complete",
    ]
    for task in spec["tasks"]:
        command.extend(["--task", task])
    spiked = run(*command)
    validated = run(sys.executable, str(CLI), "validate", str(repo))
    route = run(sys.executable, str(CLI), "route", str(repo), "--json")
    status = run(sys.executable, str(CLI), "status", str(repo), "--json")
    if any(result.returncode != 0 for result in (spiked, validated, route, status)):
        raise RuntimeError("\n".join(result.stderr + result.stdout for result in (spiked, validated, route, status)))
    route_data = json.loads(route.stdout)
    status_data = json.loads(status.stdout)
    return {
        "id": spec["id"],
        "style": spec["style"],
        "contract_valid": route_data["valid"],
        "route_mode": route_data["mode"],
        "open_task_count": status_data["tasks"]["open"],
        "seed_file_preserved": seed.read_text(encoding="utf-8") == spec["seed_content"],
        "seed_commit_preserved": run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip() == before_head,
        "setup_required": status_data["setup_required"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="replace the reviewed recorded metrics")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="go-pilots-") as temporary:
        results = [pilot(path, Path(temporary)) for path in sorted(SPECS.glob("*.json"))]
    payload = {"schema": "go-workflow.pilot-metrics.v1", "pilots": results}
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.write:
        RECORDED.write_text(rendered, encoding="utf-8")
    elif not RECORDED.is_file() or RECORDED.read_text(encoding="utf-8") != rendered:
        print("pilot metrics differ; review and run scripts/run-pilots.py --write", file=sys.stderr)
        print(rendered, file=sys.stderr)
        return 1
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
