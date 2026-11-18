"""Read-only operator dashboard for the moomoo paper challenge.

The dashboard reads the artefacts the pipeline already writes (account snapshot,
cycle reports, guard event log, approvals) and records human approvals. It never
imports the broker adapter and never submits, modifies or cancels an order.
Cloud Agents sessions/events are read server-side with a PAT from .env; the token
is never sent to the browser.
"""

__all__ = ["state", "server", "cloud"]
