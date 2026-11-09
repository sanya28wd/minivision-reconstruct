---
status: accepted
date: 2026-10-12
---

# Two passes per view: an overview pass, then a dense pass around each collision moment

Each view is analysed twice:
1. **Overview pass.** The whole view goes in as native video with audio at a low frame rate. It finds the parties and the candidate collision moments.
2. **Dense pass.** A short window around each candidate moment is re-examined at 15–30 fps. It settles contact, striker versus struck, and the order of events.

**Why:**
- Collision detection is the weakest VLM task. In Nexar's benchmark, Gemini 2.0 Flash recalled 15% of collisions.
- Impacts are often visible for under 0.5 s.
- Accuracy rose from 40.9% at 1 fps to 76.3% at 30 fps (VLM-AutoDrive).
- Native video plus audio beat frame sampling.

Keeping the dense rate to a few seconds of footage keeps latency and cost near the under-30 s goal.

## Consequences
- The exact frame rates and window lengths are project assumptions to be validated on the benchmark (ADR 0006). Latency is measured, never assumed.
- If the overview misses a collision, the dense pass never runs. Overview collision recall is therefore tracked as its own metric.
