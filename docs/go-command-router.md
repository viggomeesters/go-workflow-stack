# Go Command Router

This is the practical command layer for Viggo/Bertus/Hermes.

The user-facing trigger is deliberately sloppy. These should all route to the same command router:

```text
go
GO
Go
GOO
gOo
```

Router rule:

```text
/^go+$/i  ->  repo-local go router
```

The router then inspects the target repo and chooses what should happen next.

## Quick use

Ask the router what it would do:

```bash
python3 ~/github/go-workflow-stack/cli/go.py router <repo> --command GOO --intent "marktplaats inbox bot" --json
```

Start a new project from rough intent:

```bash
python3 ~/github/go-workflow-stack/cli/go.py spike ~/github/marktplaats-bot \
  --project-id marktplaats-bot \
  --name "Marktplaats Bot" \
  --brief "Inbox monitor that checks Marktplaats messages and alerts Viggo" \
  --epic "inbox-monitor|Inbox Monitor" \
  --task "design-monitor|Design the inbox monitor" \
  --task "build-poller|Build the polling loop"
```

Hand off control for autonomous execution:

```bash
python3 ~/github/go-workflow-stack/cli/go.py auto ~/github/marktplaats-bot --max-tasks 3 --json
```

Escalate to the stronger loop contract when `go auto` needs to keep driving beyond the initial batch:

```bash
python3 ~/github/go-workflow-stack/cli/go.py loop ~/github/marktplaats-bot --max-tasks 10 --json
```

## Router decision matrix

| Repo state | Router decision | Why |
|---|---|---|
| No repo directory | `spike` | Create repo, Git, repo-complete basics, `.go` contract, first tasks |
| Repo exists, no `.go/project.json` | `spike` | Retrofit repo-local state without broad migration |
| `.go` exists but vision/principles/hierarchy invalid or missing | `spike` repair path | Complete the repo-local contract first |
| Valid `.go`, open tasks exist | `auto` | Hand off control for bounded autonomous execution |
| `auto` finds follow-up work, failed review, or first green is not trustworthy | `loop` | Continue autonomously through repair/self-reflect until blocker |
| Valid `.go`, no open tasks | `task create` / feedback ingestion | Convert new Viggo input into tasks/decisions |
| Dirty owned scope / conflict / secret-looking path | block | Human/repair gate before autonomous changes |

## What `go spike` creates

`go spike` is for ideas/designs that may or may not already have a repo.

It does this:

1. Resolve target repo path.
2. If missing, create the directory and run `git init`.
3. Scaffold repo-complete basics without overwriting existing files:
   - `README.md`
   - `.gitignore`
   - `LICENSE`
   - `SECURITY.md`
   - `CONTRIBUTING.md`
   - `CHANGELOG.md`
   - `Makefile`
   - `scripts/check.sh`
4. Create/repair `.go/`:
   - `.go/project.json`
   - `.go/vision.json`
   - `.go/architecture-principles.json`
   - `.go/hierarchy.json`
   - `.go/tasks/open/*.json`
   - `.go/evidence/events.jsonl`
   - `.go/decisions/events.jsonl`
   - `.go/runs/events.jsonl`
5. Append an ADR-lite decision event: this repo uses `go spike` / `go auto`.
6. Validate the contract.
7. Print the next open task.

## What `go auto` means for Hermes/Bertus

`go auto` is not “print a task list”. It means **Viggo hands over control** inside the repo-local safety boundary. The CLI does not run an LLM; it emits the execution contract Hermes must follow immediately with tool calls unless a stop condition is already present:

```text
status -> next -> claim -> execute -> verify -> recheck -> devil -> finish -> self-reflect -> summarize -> continue-or-escalate
```

Operationally:

1. Read repo state via `.go`, not vault task state.
2. Claim one task.
3. Edit only inside task scope.
4. Run the task verification commands.
5. Recheck/devil/harden when code, docs, contracts, UI, automation, or public behavior changed.
6. Finish only with evidence.
7. Self-reflect: should vision, principles, hierarchy, or tasks be improved?
8. Summarize compactly for Viggo.
9. Convert Viggo's next feedback into new tasks/decisions, then repeat on the next `go auto`.

`go auto` may invoke `go loop` when:

- self-reflect creates obvious follow-up tasks in the same repo/scope;
- verification/recheck/devil fails and repair attempts are needed;
- first green is too weak to trust;
- there are still open tasks and no safety blocker;
- the user phrased the command as full control handoff, e.g. “ga door”, “werk tot groen”, “loop”, “controle afgeven”.

## What `go loop` means

`go loop` is the stronger autonomous control-handoff contract:

```text
control handed off -> keep selecting/claiming/repairing tasks until done, budget exhausted, or blocker
```

It can continue beyond the initial task batch by materializing follow-up tasks from self-reflect/recheck/devil findings, as long as they stay inside repo scope and do not cross a safety gate.

## Example conversation mapping

```text
Viggo: go marktplaats bot die inbox blijft checken
Router: no repo / no .go -> go spike
Agent: creates repo + .go vision/principles/tasks

Viggo: go auto
Router: valid .go + open tasks -> go auto
Agent: executes tasks one by one with verification + evidence

Viggo: ziet er goed uit maar voeg telegram alerts toe
Router: valid .go + feedback -> task create / decision create
Agent: writes new task(s), then next go auto continues
```

## Stop conditions

Do not auto-continue when any of these apply:

- missing credentials;
- public/destructive/payment/impersonation action;
- recipient ambiguity;
- merge conflict;
- dirty owned scope not created by this run;
- secret-looking path or data;
- invalid generated workflow state.
