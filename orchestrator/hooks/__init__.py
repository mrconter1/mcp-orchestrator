"""Hook scripts Claude Code runs inside a session.

These are the only place where a session's pid and its transcript id are both
visible, which is what makes the session registry and the restart choreography
possible at all.

Every hook here obeys one rule: never fail the session. A hook that raises,
hangs or writes noise to stdout degrades every turn, and none of this is worth
that. They swallow their own errors and exit 0.
"""
