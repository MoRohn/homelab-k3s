# understanding

Bounded judgments for the Adaptive Understanding Compiler's representation router
(`lif/lif/understanding/router.py`, docs: `lif/docs/UNDERSTANDING.md`).

| Decision | Primitive | Labels | Effect in the router |
|---|---|---|---|
| `understanding-prose-sufficient` | noul | `yes`, `no` | `yes` scales every non-prose renderer's utility by 0.75 |
| `understanding-interaction-worthwhile` | noul | `yes`, `no` | `no` scales interactive renderers' utility by 0.6 |
| `understanding-diagram-family` | choice | `causal`, `process`, `dependency`, `timeline`, `none` | picks the Mermaid/Excalidraw layout |

All three see only `features`: derived counts, structure strengths (0 to 1) and the question type.
They never see the question text, the explanation's claims or cluster state, so the state is PUBLIC.
The router's code scores work without them. An answer that is not `actionable` is ignored.
Deterministic fallbacks are in `lif/lif/decision/rules.py`.
