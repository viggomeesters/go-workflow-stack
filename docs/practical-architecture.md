# Practical Architecture

This repository pair is a practical architecture for repo-local agentic engineering.

## The split

| Layer | Repository / location | Owns | Does not own |
|---|---|---|---|
| Workflow stack | [`go-workflow-stack`](https://github.com/viggomeesters/go-workflow-stack) | CLI, schemas, validators, fixtures, reusable workflow rules | Project-specific execution state |
| Project template | [`go-project-template`](https://github.com/viggomeesters/go-project-template) | Copyable `.go/` starter structure | Shared tooling implementation |
| Real project repo | Any repo created from/adapted from the template | Its own `.go/` state, tasks, evidence, decisions | Hidden central task queue |
| Vault / Life OS | Private operator memory/index | Reflection, routing, long-term memory, optional indexing | Canonical repo execution state |

## Mental model

```text
go-workflow-stack
  provides: cli/go.py + schemas + validation rules
          │
          ▼
go-project-template
  provides: a minimal, copyable .go/ structure
          │
          ▼
real project repository
  owns: project.json, vision.json, hierarchy.json, tasks, evidence, decisions
```

The stack is the toolbelt. The template is the empty workshop layout. A project repo is the actual workshop with its own work orders and evidence.

## Practical workflow

### 1. Start a new project from the template

Use GitHub's **Use this template** flow from `go-project-template`, or clone/copy it:

```bash
git clone https://github.com/viggomeesters/go-project-template.git my-project
```

### 2. Put the stack next to it

```bash
git clone https://github.com/viggomeesters/go-workflow-stack.git
cd go-workflow-stack
```

### 3. Customize the project's `.go/` state

In `my-project` edit:

- `.go/project.json` — identity and default verification;
- `.go/architecture-principles.json` — hard project rules such as native JavaScript, size limits, visual constraints, privacy boundaries;
- `.go/vision.json` — what the project should become;
- `.go/hierarchy.json` — feature groups, features, and task links;
- `.go/tasks/open/*.json` — claimable work.

### 4. Validate from the stack

```bash
python3 cli/go.py validate ../my-project
python3 cli/go.py readback ../my-project
python3 cli/go.py next ../my-project
```

### 5. Execute repo-local work

```bash
python3 cli/go.py claim <task-id> --repo ../my-project --agent hermes
# edit only the task scope
python3 cli/go.py finish <task-id> --repo ../my-project --agent hermes --evidence "<proof>"
```

Evidence lands in the project repo's `.go/` JSON/JSONL state. A future agent can clone the project and continue without chat history or a private vault task database.

## Why this architecture exists

The goal is to remove agent-workflow friction:

- no hidden central execution state;
- no vault lock drama as a prerequisite for repo work;
- no chat-only plan context;
- no Markdown-as-canonical workflow database;
- no clean-repo absolutism when unrelated dirt exists.

Instead, the project repo carries its own operational contract.

## Guardrails

- JSON is canonical for current state.
- JSONL is canonical for lifecycle, evidence, and decision streams.
- Markdown explains; it does not own state.
- The stack can evolve independently from projects.
- The template can stay small and copyable.
- Real projects should commit their `.go/` state with the code it governs.

## What to do next

- Improve the stack when commands/schemas need to change.
- Improve the template when the starter `.go/` shape should change.
- Improve a real project's `.go/` files when that project direction/tasks/evidence changes.
