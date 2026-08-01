# Advice-to-outcome continuity

The public contract removes the relay between a useful recommendation and
verified repository work:

```text
question -> advice -> pending recommendation -> Go/Sent as goal
         -> semantic .go tasks -> execute -> outcome evidence -> done
```

## Authority

`router` reports authority separately from route selection:

- questions are `advice`: no product implementation, but compact planning state
  may be persisted;
- `alleen advies`, `read-only`, `niet wegschrijven`, and equivalent wording are
  `read_only`: neither implementation nor planning-state writes are authorized;
- imperatives, canonical `Go`, and **Sent as goal** are `execute`.

An agent should never persist the full exploration response. It selects one
approach and bounded work units in `go-workflow.execution-brief.v1`, then uses
the internal command:

```bash
go-workflow recommendation create . \
  --brief /tmp/execution-brief.json \
  --authority advice \
  --authority-source question
```

The validated record lives at `.go/recommendations/pending.json`. Its source
hash and optional `source_ref` retain provenance without copying chat prose.
Use `--read-only` to validate a proposed brief without writing state.

## Promotion

A later bare `Go` needs no chat transcript or brief path:

```bash
go-workflow go . --execute
```

The runtime validates the pending record, creates one task per semantic work
unit, archives the recommendation under `.go/recommendations/applied/`, and
continues execution in the same invocation. Repeating `Go` does not recreate
the tasks.

Each acceptance item becomes a tracked R# outcome. Manual finish and autonomous
finish use the same closure gate. Successful verification plus critic records
`verified` evidence automatically; `blocked` and `rejected` dispositions require
explicit non-empty evidence.

## Local proof

GitHub Actions are not part of this contract. Verify locally:

```bash
python3 -m pytest tests/test_smoke.py -q
bash scripts/check-linux.sh
python3 cli/go.py template-check ../go-project-template --json
```

`check-linux.sh` is the Linux/WSL-compatible proof path. A fresh project uses
the paired template launcher and the same repo-local recommendation state.
