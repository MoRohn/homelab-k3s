# Explanation packages

Reusable, versioned ExplanationSpecs and domain vocabulary for the Understanding Compiler
(`docs/UNDERSTANDING.md`, spec §74–§75). A package entry is an ExplanationSpec/v1 document plus
`match:` patterns. When a question matches, the compiler starts from that spec instead of
building one from scratch. This works offline and needs no model.

| Package | Entries | Domain |
|---|---|---|
| `kubernetes/` | `what-is-kubernetes` | Definitions |
| `gpu-scheduling/` | `pods-not-reaching-gpus`, `reservation-throughput` | Kubernetes GPU placement, memory reservation |
| `labzilla/` | `jev-cascade` | How Labzilla routes judgments |

Rules:

- Content here is generic and public. Specs built from live cluster state go in
  `private/lif/understanding/` *(private, local only)*.
- Never edit an entry in place once another spec names it as `metadata.parent`. Add `-v2` instead.
- `local-ai explain --lint-packages` must report no errors.
