# knowledge-work

Context selection for the Knowledge Work layer reuses
[`agent-core/context-relevance`](../agent-core/context-relevance/) with knowledge objects as
`item` (spec §87, §94): graph traversal and deterministic filters run first, then Jev judges
relevance in parallel, and only the small relevant set reaches a generative model.
