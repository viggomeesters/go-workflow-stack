
.PHONY: check
check:
	python3 -m py_compile cli/go.py
	python3 cli/go.py validate ../go-project-template
	python3 cli/go.py readback ../go-project-template
