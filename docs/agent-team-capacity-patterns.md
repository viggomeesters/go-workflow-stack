# Agent team capacity patterns

This note records public-safe pattern mining from
[`777genius/agent-teams-ai`](https://github.com/777genius/agent-teams-ai) for
the repo-local Go workflow stack. The source repository is AGPL-3.0; this stack
mines product and workflow patterns only. No source code is copied.

## What to adopt

The useful pattern is not "more agents." The useful pattern is explicit capacity
control around durable tasks, task-scoped logs, review states, and usage
attribution.

Adopt these semantics in this order:

1. **Capacity policy first** — default to solo or lead-plus-one-worker. Parallel
   builders are only safe when selected task modify scopes are disjoint, tasks
   can ship independently, or a human/CLI override records why parallelism is
   worth the collision risk.
2. **Review lifecycle second** — work completion is not approval. Model the task
   as work status plus review status so `completed` cannot silently become
   `approved`.
3. **Finish evidence third** — completion must carry task id, changed files or an
   explicit no-diff result, verification command/result, runtime/model/agent
   attribution when available, and reviewer/critic outcome or skip reason.
4. **Usage attribution fourth** — record runtime kind, billing mode, task/run
   owner, and token/context/API-equivalent estimates where available. Do not
   label subscription/free usage as invoice cost.
5. **Template rollout last** — after the stack semantics are stable, update the
   public starter template so new projects inherit the same defaults.

## Review lifecycle

Task completion and task approval are separate axes:

- `work_status`: `pending` → `in_progress` → `completed`.
- `review_status`: `none` → `review` → `approved`, with `needs_fix` as a return path.

A finished non-trivial task is completed work waiting for review; it is not approval by stealth. `go task review --status needs_fix` sends the task back to the owner while preserving `review_history`, and `--status approved` records the explicit approval transition.

## Operational defaults

| Situation | Default |
| --- | --- |
| One selected task | Solo builder plus critic/reviewer lane |
| Multiple tasks with overlapping or broad modify scopes | Serial builders, reviewer lane on |
| Multiple tasks with provably disjoint modify scopes | Parallel builders may be allowed |
| Dirty main worktree or generated-file churn | Prefer serial execution or isolated worktree |
| Task says done but lacks review/evidence | Not approved; return to review or needs-fix |

Worktree isolation is a tool, not a religion. Use it when two or more workers
may edit the same repository at once, broad generators/formatters are in play,
or the lead workspace has user changes that must be preserved. Keep it off for
read-only tasks, one-owner edits, and small serial work.

## Why this comes before multi-agent UI expansion

Without capacity policy and review-state separation, a visual agent board just
makes failure prettier. The bottleneck becomes harder to see: agents can appear
busy while they collide on the same files, burn subscription limits, or mark
work as complete without approval-quality evidence.

The Go stack therefore treats capacity, lifecycle, and evidence as engine-level
contracts before any broad multi-agent user interface or provider-orchestration
feature.
