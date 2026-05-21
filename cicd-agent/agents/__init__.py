"""
Agents package. Each agent is an async function that takes structured input
and returns a typed dataclass. Agents never call each other — the orchestrator
calls them in sequence.
"""
