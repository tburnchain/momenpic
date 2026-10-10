"""Re-export: product facts live in the shared policy package so the gate (M7) uses the identical
definition of 'what counts as evidence' without importing this module."""

from acvm.policy.facts import ProductFacts, build_facts, short_name

__all__ = ["ProductFacts", "build_facts", "short_name"]
