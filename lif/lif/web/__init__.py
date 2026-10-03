"""Live information for local models: freshness detection, exact feeds, web search, grounding.

    question → freshness (rules) → query (dates resolved in the user's timezone)
             → feeds (exact, e.g. sports scores) ∥ search (SearXNG) → ranked evidence
             → grounded prompt for a local model

Only the built query leaves the box, never the conversation (privacy.web_search in lif.yaml).
Docs: docs/WEB_GROUNDING.md.
"""
