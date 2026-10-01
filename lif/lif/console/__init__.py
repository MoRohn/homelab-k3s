"""Labzilla Console: the human front door to LIF (PWA + BFF).

FastAPI backend-for-frontend that serves the Preact PWA (apps/console) and one stable /api surface.
It owns users, sessions, device pairing, CSRF and audit; it calls the controller, gateway, batch and
Prometheus over an allowlist with server-side keys, and fans live state out over one SSE hub.
Design and coverage: docs/CONSOLE.md. Wire shapes: contracts.py (mirrored to TypeScript by gen_ts.py).
"""
