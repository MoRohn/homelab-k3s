"""Persistent knowledge layer: typed Markdown agent repos compiled into a queryable graph.

Canonical knowledge is text in Git (agent repos of typed Markdown + YAML type definitions).
The SQLite index under `.knowledge/` is derived and can always be rebuilt (`knowledge rebuild`).
See docs/KNOWLEDGE.md.
"""
