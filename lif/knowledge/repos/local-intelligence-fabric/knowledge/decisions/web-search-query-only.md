---
type: decision
title: Live questions are looked up on the web; only the built search query leaves the box
question: How should local models answer questions that need information newer than their training?
status: accepted
selected: 'Gateway grounding (lif/web): the needs-live-data decision, then exact feeds (ESPN, Open-Meteo) and
  self-hosted SearXNG, then a local model answers from the evidence with citations. Only the built query
  (≤ 200 chars) is sent, under every privacy setting except RESTRICTED. No paid API.'
alternatives:
- answer from training data (the "as of my last update" failure)
- paid search or LLM API (e.g. OpenAI web search)
- lookups only for prompts declared PUBLIC
- a per-message "allow web" toggle
date: 2026-10-03
evidence:
- '[[src-web-grounding-doc]]'
assumptions:
- '[[private-data-stays-local]]'
affects:
- '[[logical-model-aliases]]'
---
Owner decisions (2026-10-03, in session):

- Lookups are allowed under "Local only", because only the query is sent [[src-web-grounding-doc^p3]].
- The backend is self-hosted SearXNG.
- Prefer a stronger open-weight local model over paid LLM APIs.

This narrows [[private-data-stays-local]]. A query derived from a CONFIDENTIAL prompt may now reach public search engines. The conversation itself still never leaves [[src-web-grounding-doc^p1]], and the detectors and the RESTRICTED class still block [[src-web-grounding-doc^p2]].
