# Attempt 1 — autonomy-managed-verification-isolation

Strategy: `direct_fix`

## Project contract

North star: Viggo can say only `go`; the agent resolves loose vs repo-local work, repairs the .go contract if needed, then autonomously works toward the defined goal until done, blocked, or budget-exhausted.

Success metrics:
- a bare `go` routes correctly between loose work and repo-local .go work
- valid .go contract is repaired before execution
- open work is split into executable tasks with acceptance and verification
- loop continues across tasks without Viggo re-triggering each phase
- recheck/devil/self-reflect findings create repairs or follow-up tasks before final done
- final report includes commits, verification, and open/blocker state

Architecture principles:
- `repo-local-state`: Project workflow state lives in .go/ next to code.
- `json-first`: Current state is JSON and append-only history is JSONL.
- `goal-contract-before-execution`: Every autonomous go run must define or repair the end goal, acceptance, verification, and task boundary before implementation.
- `command-train-not-command-spam`: A bare go on a valid .go repo starts the agent command train instead of returning micro-commands for Viggo to run.
- `first-green-is-not-done`: A task is not done at first passing verification; recheck/devil/critic and repair/self-reflect must run for non-trivial work.
- `capacity-before-parallelism`: The go loop defaults to solo or lead-plus-one-worker; parallel builders require disjoint modify scopes and a reviewer lane, while overlapping modify scopes remain serial.
- `abc-release-evidence`: Planned: de runner beheert de volledige release en sluit de taak pas na het vereiste bewijs.
- `abc-explicit-model-contract`: Planned: model, effort en effectieve runtime-instellingen zijn expliciet en blijven behouden bij hervatten.
- `abc-durable-worker-context`: Planned: voortgang en verifieerbare contextsnapshots leven in .go; workers kunnen op veilige grenzen worden vervangen.
- `abc-task-workspace`: Planned: één branch/worktree per taak met gevalideerde identiteit, scope en exclusief schrijverschap; modelwissels behouden de werkplek.
- `abc-final-candidate-proof`: Planned: fasebewijs is getypeerd en aan geteste inhoud gebonden; integratie of versievoorbereiding invalideert getroffen checks.
- `abc-selective-legacy-reuse`: Planned: vertaal relevante fasecontracten en regressies; laad skills naar taak/risico en behoud de verplichte lifecycle.

Hierarchy epics: autonomous-go-runtime, workflow, advice-to-outcome-continuity, complete-advice-to-outcome-model, shareable-output, delivery-release-operations, architecture-lane, autonomy-first

## Task

Summary: Isolate managed verification without weakening worker guards

Run repository-wide verification for a managed task against an evidence-identical disposable checkout so fixture mutations do not inherit the registered worker boundary. Preserve the worker guard and bind proof to the exact candidate content.

## Acceptance

- Managed verification of make check can execute temporary fixture repositories without treating them as the registered task worker.
- The verification source is byte-identical for every scoped candidate change and proof remains bound to the managed candidate digest.
- The registered workspace mutation guard remains unchanged and still rejects direct control-state mutation.

## Verification

- `uv run --no-project --python 3.12 --with 'pytest>=8,<9' --with 'jsonschema>=4.23' python -m pytest tests/test_abc_resume.py tests/test_abc_completion.py tests/test_abc_worktrees.py -q -p no:cacheprovider`
- `make check`
- `python3 cli/go.py validate .`
- `git diff --check`
