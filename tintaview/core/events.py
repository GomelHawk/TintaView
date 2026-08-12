"""The event vocabulary shared by the hook script, the HTTP server and the agent adapters.

These names are part of the on-disk contract: they appear in the URLs baked into every
agent's hook configuration, so renaming one breaks every already-installed hook. Add
new events rather than repurposing existing ones.
"""

from __future__ import annotations

SESSION_START = "session-start"
SESSION_END = "session-end"
WORKING = "working"
IDLE = "idle"
CONFIRM = "confirm"
TOOL_START = "tool-start"
TOOL_END = "tool-end"

EVENTS = (
    SESSION_START,
    SESSION_END,
    WORKING,
    IDLE,
    CONFIRM,
    TOOL_START,
    TOOL_END,
)

# Agent-less event names: the same events, but addressed as a bare `/idle`, `/working`,
# ... with no `agent=` in the query. Serving them as aliases (defaulting to agent=claude)
# keeps hand-written or third-party hook scripts working without a `?agent=` parameter.
LEGACY_EVENTS = (SESSION_START, SESSION_END, WORKING, IDLE, CONFIRM)

# Effective status values reported by /state.
STATUS_IDLE = "idle"
STATUS_WORKING = "working"
STATUS_CONFIRM = "confirm"
STATUS_NONE = "none"

#: Priority order used to fold many sessions into one colour. First match wins.
STATUS_PRIORITY = (STATUS_CONFIRM, STATUS_WORKING, STATUS_IDLE)
