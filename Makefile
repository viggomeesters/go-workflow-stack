
.PHONY: check
check:
	python3 -m py_compile cli/go.py
	TEMPLATE=$$(if [ -d ../go-project-template/.go ]; then printf '%s' ../go-project-template; else printf '%s' fixtures/minimal; fi); \
	  python3 cli/go.py validate $$TEMPLATE; \
	  python3 cli/go.py readback $$TEMPLATE; \
	  python3 cli/go.py status $$TEMPLATE --json >/tmp/go-project-template-status.json; \
	  if [ -d ../go-project-template/.go ]; then python3 cli/go.py template-check ../go-project-template --json >/tmp/go-template-check.json; fi
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
	  python3 cli/go.py spike $$TMP/spike-smoke --project-id spike-smoke --name "Spike Smoke" --brief "Smoke go spike" --epic delivery\|Delivery --task first\|First --task second\|Second --verification "python3 -c 'print(42)'"; \
	  python3 cli/go.py validate $$TMP/spike-smoke; \
	  python3 cli/go.py auto $$TMP/spike-smoke --max-tasks 2 --json >/tmp/go-auto-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); ep=p["execution_policy"]; assert p["control_handoff"] and p["can_escalate_to"] == ["go-loop"] and "continue-or-escalate" in p["loop"] and ep["ask_policy"] == "do-not-ask-when-safe-default-exists" and ep["may_create_follow_up_tasks"]' /tmp/go-auto-smoke.json; \
	  python3 cli/go.py loop $$TMP/spike-smoke --max-tasks 2 --json >/tmp/go-loop-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["mode"] == "go-loop" and p["continues_beyond_initial_tasks"]' /tmp/go-loop-smoke.json; \
	  python3 cli/go.py go-loop $$TMP/spike-smoke --max-tasks 2 --json >/tmp/go-loop-alias-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["mode"] == "go-loop" and p["next_tasks"] == ["first", "second"]' /tmp/go-loop-alias-smoke.json; \
	  git -C $$TMP/spike-smoke add .go README.md .gitignore LICENSE SECURITY.md CONTRIBUTING.md CHANGELOG.md Makefile scripts/check.sh; \
	  git -C $$TMP/spike-smoke -c user.name=Make -c user.email=make@example.com commit -m "seed spike smoke" -q; \
	  python3 cli/go.py auto $$TMP/spike-smoke --max-tasks 1 --execute --agent make-check --json >/tmp/go-auto-execute-smoke.json; \
	  python3 -c 'import json,sys,pathlib; p=json.load(open(sys.argv[1])); r=pathlib.Path(sys.argv[2]); assert p["status"] == "done" and p["completed_tasks"] == ["first"] and (r/".go/tasks/done/first.json").is_file() and (r/".go/reflections/events.jsonl").is_file()' /tmp/go-auto-execute-smoke.json $$TMP/spike-smoke; \
	  python3 cli/go.py router $$TMP/spike-smoke --command gOo --intent "ga verder" --json >/tmp/go-router-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["normalized_command"] == "go" and p["recommended"]["command"] == "auto"' /tmp/go-router-smoke.json; \
	  python3 cli/go.py router $$TMP/spike-smoke --command GOO --intent "controle afgeven werk tot groen" --json >/tmp/go-router-loop-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["recommended"]["command"] == "go-loop"' /tmp/go-router-loop-smoke.json; \
	  python3 cli/go.py router $$TMP/spike-smoke --command go-loop --json >/tmp/go-router-direct-loop-smoke.json; \
	  python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["normalized_command"] == "go-loop" and p["recommended"]["command"] == "go-loop"' /tmp/go-router-direct-loop-smoke.json
