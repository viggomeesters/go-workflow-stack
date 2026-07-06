
.PHONY: check
check:
	python3 -m py_compile cli/go.py
	TEMPLATE=$$(if [ -d ../go-project-template/.go ]; then printf '%s' ../go-project-template; else printf '%s' fixtures/minimal; fi); \
	  python3 cli/go.py validate $$TEMPLATE; \
	  python3 cli/go.py readback $$TEMPLATE; \
	  python3 cli/go.py status $$TEMPLATE --json >/tmp/go-project-template-status.json
	TMP=$$(mktemp -d); \
	  git init -q $$TMP/adopt-smoke; \
	  python3 cli/go.py adopt $$TMP/adopt-smoke --project-id adopt-smoke --name "Adopt Smoke" --feature-group workflow\|Workflow --feature workflow\|repo-local\|Repo-local --verification "git diff --check"; \
	  python3 cli/go.py task create $$TMP/adopt-smoke --id smoke-task --summary "Smoke task" --feature workflow.repo-local --acceptance "Task exists" --verification "git diff --check"; \
	  python3 -c 'import json,sys; h=json.load(open(sys.argv[1])); assert h["epics"][0]["features"][0]["tasks"] == ["smoke-task"]' $$TMP/adopt-smoke/.go/hierarchy.json; \
	  if python3 cli/go.py task create $$TMP/adopt-smoke --id smoke-task --summary "Duplicate" >/tmp/go-duplicate.out 2>/tmp/go-duplicate.err; then echo "duplicate task create should fail"; exit 1; fi; \
	  if python3 cli/go.py task create $$TMP/adopt-smoke --id bad-feature --summary "Bad feature" --feature workflow.missing >/tmp/go-bad-feature.out 2>/tmp/go-bad-feature.err; then echo "bad feature task create should fail"; exit 1; fi; \
	  test ! -e $$TMP/adopt-smoke/.go/tasks/open/bad-feature.json; \
	  python3 cli/go.py validate $$TMP/adopt-smoke; \
	  python3 cli/go.py next $$TMP/adopt-smoke; \
	  python3 cli/go.py status $$TMP/adopt-smoke --json >/tmp/go-adopt-smoke-status.json; \
	  python3 cli/go.py bundle export $$TMP/adopt-smoke --output $$TMP/adopt-smoke-bundle.json; \
	  python3 -m json.tool $$TMP/adopt-smoke-bundle.json >/tmp/go-adopt-smoke-bundle.pretty.json; \
	  git init -q $$TMP/import-target; \
	  python3 cli/go.py adopt $$TMP/import-target --project-id import-target --name "Import Target" --feature-group workflow\|Workflow --feature workflow\|repo-local\|Repo-local --verification "git diff --check"; \
	  python3 cli/go.py bundle import $$TMP/import-target $$TMP/adopt-smoke-bundle.json >/tmp/go-import-dry-run.json; \
	  python3 -c 'import json,sys; plan=json.load(open(sys.argv[1])); assert plan["mode"] == "dry_run" and plan["target_path"].startswith(".go/imports/")' /tmp/go-import-dry-run.json; \
	  python3 cli/go.py bundle import $$TMP/import-target $$TMP/adopt-smoke-bundle.json --write --agent make-check --task-id import-smoke; \
	  python3 cli/go.py validate $$TMP/import-target; \
	  test -n "$$(ls $$TMP/import-target/.go/imports/*.json)"; \
	  python3 cli/go.py spike $$TMP/spike-smoke --project-id spike-smoke --name "Spike Smoke" --brief "Smoke go spike" --epic delivery\|Delivery --task first\|First --task second\|Second; \
	  python3 cli/go.py validate $$TMP/spike-smoke; \
	  python3 cli/go.py auto $$TMP/spike-smoke --max-tasks 2 --json >/tmp/go-auto-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["control_handoff"] and p["can_escalate_to"] == ["go-loop"] and "continue-or-escalate" in p["loop"]' /tmp/go-auto-smoke.json; \
	  python3 cli/go.py loop $$TMP/spike-smoke --max-tasks 2 --json >/tmp/go-loop-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["mode"] == "go-loop" and p["continues_beyond_initial_tasks"]' /tmp/go-loop-smoke.json; \
	  python3 cli/go.py router $$TMP/spike-smoke --command gOo --intent "ga verder" --json >/tmp/go-router-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["normalized_command"] == "go" and p["recommended"]["command"] == "auto"' /tmp/go-router-smoke.json; \
	  python3 cli/go.py router $$TMP/spike-smoke --command GOO --intent "controle afgeven werk tot groen" --json >/tmp/go-router-loop-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["recommended"]["command"] == "loop"' /tmp/go-router-loop-smoke.json
