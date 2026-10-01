# Knowledge workspace

Typed, linked, versioned project knowledge for labzilla agents. See
[docs/KNOWLEDGE.md](../docs/KNOWLEDGE.md).

| Path | What |
|---|---|
| `workspace.yaml` | repos in the workspace, registry, events repo, agent permissions |
| `registry/<package>/<version>/` | published knowledge packages (immutable) |
| `repos/local-intelligence-fabric/` | the dogfood project: LIF's own decisions, assumptions, evidence, work |
| `../../private/knowledge/lif-operations/` | private operational history (git-ignored, optional) |
| `.knowledge/` | derived index (git-ignored) |
