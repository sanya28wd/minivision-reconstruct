---
status: accepted
date: 2026-10-12
---

# The AI extracts cited facts; a deterministic Dubai-law rule engine decides fault, violations and fines

The vision model never assigns fault status, violations or fines directly. Instead:
1. It extracts **cited facts per party**: who did what, when, and in which view. For example, "silver sedan entered the junction at 00:14; the signal for its direction was red, visible at 00:12".
2. A **deterministic rule engine** maps those facts to Dubai Police fault status (pink, green or white), MR 178/2017 violations with their item numbers, fines, and Decree-Law 14/2024 referrals. Example rules: priority under Art. 6, pedestrian liability under Art. 7(4).

**Why:** benchmarks show that vision models describe crash scenes well but assign responsibility badly.
- CAViAR: lighting about 98.6%, but at-fault only 1.2–2.3 out of 5, rule violation under 1 out of 5, and plausible rules attached to the wrong party.
- CrashSight: zero-shot fault about 70% versus 95% for humans.
- The newest liability systems use the same two-stage shape (TrafficRAG, AITP).

A rule engine makes fault explainable and quota-free to test. Fixing a fine becomes a table edit, not prompt surgery. It also enforces ADR 0006: every outcome traces to a cited fact and an official rule.

## Consequences
- Any situation the rules do not cover produces Cannot Determine, never a model guess.
- The rule table is tested on its own, separately from the model, as a guard against Hayden AI-style configuration errors.
- Sources: docs/research/COMPARABLE_SYSTEMS.md and UAE_TRAFFIC_FINES_VERIFIED.md.
