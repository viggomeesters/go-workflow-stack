# Repository intelligence

`.go` can describe both the work around an application and the application
itself. Repository intelligence adds a small, durable semantic map and a
detailed, regenerable source graph without turning generated analysis into a
new source of truth.

## Two layers, different authority

The optional `.go/repository-map.json` is committed. It gives important
application areas stable IDs, path globs, intended dependencies, owning test
suites, and small human-authored overlays. Tasks and architecture records may
refer to those stable IDs.

The detailed `.go/cache/repository-graph.json` is local and ignored. It is a
materialized view of current Git-visible source. A build records content
hashes, generic file/component/test nodes, Python symbols and resolvable Python
imports. It deliberately excludes `.go`, VCS metadata, dependency directories,
and build output.

Authority remains explicit:

- source code says what the application currently does;
- vision, principles, briefs, and decisions say what it should become;
- tasks say what work is authorized;
- the repository graph helps agents navigate and verify impact.

The generated graph must therefore never silently rewrite architecture or task
state. Dynamic dependencies and language-specific relationships that cannot be
proven statically remain documented limitations.

## Contract

A minimal map looks like this:

```json
{
  "schema": "go-workflow.repository-map.v1",
  "kind": "repository_map",
  "project": "example",
  "nodes": [
    {
      "id": "api",
      "kind": "service",
      "summary": "Public HTTP API",
      "paths": ["src/api/**"],
      "depends_on": ["domain"],
      "tests": ["api-tests"]
    }
  ],
  "overlays": []
}
```

Node IDs are stable semantic handles. Paths are safe repository-relative
globs. `depends_on` and `tests` must point to other nodes in the same map.
Repositories without a map remain valid; once a map is present, `validate`
checks it strictly against the repository project identity.

## Commands

Build the local materialized view:

```bash
python3 cli/go.py index build . --json
```

Check whether source or map changes made it stale:

```bash
python3 cli/go.py index status . --json
```

Select at most ten matching nodes from a fresh graph:

```bash
python3 cli/go.py index query . "authentication session" --limit 10 --json
```

Queries are deterministic lexical routing aids. Results carry exact file,
symbol, and line provenance where extraction supports it. A missing or stale
graph fails with a rebuild instruction instead of returning outdated context.

## Task-bound context

A task can opt into repository intelligence with a strict
`repository_context` contract:

```json
{
  "repository_context": {
    "nodes": ["api", "domain"],
    "queries": ["session validation"],
    "max_nodes": 80,
    "max_edges": 160,
    "dependency_depth": 2,
    "include_tests": true,
    "impact_policy": "strict"
  }
}
```

`nodes` resolve only against committed repository-map IDs. `queries` add
bounded lexical discovery without storing generated prose in the task.
`max_nodes` must be large enough for every directly requested node; those nodes
are mandatory. The selector then follows intended dependencies, observed
imports and containment up to the declared depth, and adds linked test suites
when requested. Both nodes and edges stop at their declared budgets.

Build, critic and repair contexts receive only this selected subgraph, its
exact source hashes, map/source digests, freshness and authority statement.
They never receive the whole cached graph. Missing maps, unknown stable IDs,
missing graphs and stale graphs fail before worker dispatch with a rebuild or
contract-repair instruction. Tasks without `repository_context` preserve the
legacy execution context unchanged.

To author the contract through the CLI, store the object in a JSON file and
pass `task create ... --repository-context context.json`.

## Regeneration and portability

Commit `.go/repository-map.json` and `.go/cache/.gitignore`; do not commit the
generated graph. Any clone can rebuild it locally without an embedding service,
database, hosted index, or GitHub API. Repeated builds over unchanged inputs
are byte-stable.
