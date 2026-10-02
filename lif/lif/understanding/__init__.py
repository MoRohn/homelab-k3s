"""Adaptive Understanding Compiler (docs/UNDERSTANDING.md).

    question → knowledge model → ExplanationSpec (one canonical IR) → router → renderers → critic

The ExplanationSpec is the only place semantic content lives. Renderers are deterministic compilers
over it: they select, order and lay out what the spec says, and the renderer contract
(render/base.py) rejects any artifact that carries an ID or a fact the spec does not have.
"""
SCHEMA_VERSION = "ExplanationSpec/v1"
