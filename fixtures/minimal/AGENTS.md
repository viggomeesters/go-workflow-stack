<!-- go-workflow:agents-gateway:v1:start -->
## Repository-local Go workflow gateway

This repository uses `.go/` as its project workflow source of truth. Keep repository-specific instructions outside this managed block; they remain binding.

When the user invokes `Go`, `Go plan`, `Go <task-id>`, `Go loop`, or asks to continue autonomously:

1. Run the immutable stack-freshness preflight required by the pinned `.go/project.json` before routing or product edits.
2. Read `.go/vision.json`, `.go/architecture-principles.json`, `.go/hierarchy.json`, and the selected task.
3. Run the repository-local `validate`, `status`, and `router` commands, then announce `Route: <selected_route>`.
4. Create or repair a concrete `.go` task before changing product files. Execute one task at a time and stay within its `scope.modify` boundary.
5. Record content-bound verification evidence, run the required critic/recheck, repair blocking findings, and satisfy finish plus any required release evidence.
6. Continue through remaining in-scope work until the goal is met, a declared budget is exhausted, or a real repository gate blocks progress. Never call an empty queue `done` without auditing the original outcomes.

Do not redirect repository workflow state to a hidden central queue or retired vault. Nested `AGENTS.md` files may add directory-specific obligations, but they do not replace the root gateway or `.go` source of truth.
<!-- go-workflow:agents-gateway:v1:end -->
