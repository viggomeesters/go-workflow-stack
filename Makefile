
.PHONY: check
check:
	python3 -m py_compile cli/go.py
	python3 cli/go.py validate ../go-project-template
	python3 cli/go.py readback ../go-project-template
	python3 cli/go.py status ../go-project-template --json >/tmp/go-project-template-status.json
	TMP=$$(mktemp -d); \
	  git init -q $$TMP/adopt-smoke; \
	  python3 cli/go.py adopt $$TMP/adopt-smoke --project-id adopt-smoke --name "Adopt Smoke" --feature-group workflow\|Workflow --feature workflow\|repo-local\|Repo-local --verification "git diff --check"; \
	  python3 cli/go.py task create $$TMP/adopt-smoke --id smoke-task --summary "Smoke task" --feature workflow.repo-local --acceptance "Task exists" --verification "git diff --check"; \
	  python3 cli/go.py validate $$TMP/adopt-smoke; \
	  python3 cli/go.py next $$TMP/adopt-smoke; \
	  python3 cli/go.py status $$TMP/adopt-smoke --json >/tmp/go-adopt-smoke-status.json
