"""Automated red-teaming: an attacker LLM writes new adversarial support scenarios against the fixed store, a gate
checks each one is valid and solvable, and an attack step keeps the scenarios that break the agents.

Generated scenarios never touch the finalized benchmark (``data/tasks.json``). They live in ``data/redteam/``, and
``evaluator.get_task`` falls back to them, so the normal runner and scorer work on them unchanged.
"""
