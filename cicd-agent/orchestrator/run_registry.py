"""
Persistent run registry — prevents infinite patch loops.

Stores run metadata in run_registry.json (never committed to git).

Responsibilities:
- is_duplicate(run_id) → bool: was this run already processed?
- get_attempt_count(error_hash) → int: how many patch attempts for this error pattern?
- increment_attempt(error_hash): called before each patch
- mark_escalated(error_hash): stops all future auto-patching for this error
- cleanup(): removes entries older than 7 days

error_hash = sha256(repo + workflow_name + error_type + file + line_number)
This means: the same type of error in the same file counts across runs.
"""
