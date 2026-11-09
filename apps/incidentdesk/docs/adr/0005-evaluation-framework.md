---
status: accepted
date: 2026-10-11
---

# Evaluation: our own pytest harness, scoring the rendered report, with one shared run log

We build a small pytest harness of our own instead of adopting an eval framework.

**Why not a framework.** Our scorers are custom (Dubai fault colours, fines, per-party findings). Video has to go through our own upload code. And the two requirements that matter most must be our own code anyway:
- score the report the officer actually sees
- log officer uploads and benchmark runs identically

The old project failed on both. In the frameworks checked, video support was limited (Inspect AI: Gemini only; promptfoo: inline size limits; DeepEval: images only).

## Shape
- **One shared entry point.** A single run wrapper, given the views and an Origin (app upload, benchmark, or replay), is used by both the app and the harness. It logs every Run the same way: input hashes, model and prompt version, configuration, raw model output, final report, per-stage latency, retries and 429s.
- **Score the report itself.** Scoring reads only the final report data object that the page and the PDF are rendered from.
- **Three layers:**
  - **L0:** deterministic checks (schema, fines table, cross-field consistency, guardrail validators).
  - **L1:** replay of recorded model outputs through the report logic. No quota is used, and it runs on every change.
  - **L2:** live benchmark. A gate set is run 3–5 times per clip, with per-field pass rate, pass^k and a Wilson interval.
- **Exact match where possible.** Structured findings are matched exactly. Only the summary and timeline are graded by a judge, which is:
  - a different model family from the generator
  - using binary per-criterion rubrics
  - calibrated against owner-labelled items, with TPR and TNR reported
- **Regression rule:** it is a regression when an accuracy lower bound falls below the Baseline, or p50/p95 latency falls outside a tolerance band. Latency is measured warm and cold separately and excludes 429 retry time.
- **Error analysis first.** Real reports are read against truth and failure modes are tallied before metrics are tuned.

Sources: Anthropic, "Demystifying evals for AI agents"; Zheng et al. 2023 (LLM-as-judge biases); Inspect AI and promptfoo documentation (video support).

## Additions, 2026-10-12 (from docs/research/ADVANCED_EVAL_AND_GUARDRAILS.md)
The design above is sound for offline testing, but outputs that an officer acts on also need these:
- **Runtime distrust.** Schema-constrained output only guarantees valid JSON, not true facts. An L0 citation validator runs on every run: each cited timestamp is inside its view, the cited view exists, and every violation and fault finding carries a citation. The repair rate is tracked.
- **Agreement as an abstain signal.** Across repeated runs, a field the runs agree on 3/3 is trusted, 2/3 is flagged to the officer, and a split becomes Cannot Determine. Abstention is graded as its own outcome, and results are reported as a risk-coverage curve. A conformal guarantee is deferred until several hundred labelled items exist.
- **Metamorphic robustness suite on the gate clips.** Re-encode, brightness change, trim offset, view-order swap and mirror (where it is legally neutral). The extracted facts must stay invariant.
- **Paired true-vs-false probes**, modelled on VideoHallucer, EventHallusion, VidHalluc and CrashSight: an absent party, a false premise, a temporal trap, and a case whose right answer is Cannot Determine.
- **Statistics fit for a small benchmark.**
  - The clip is the unit of analysis.
  - Paired McNemar test plus a clustered bootstrap for comparing changes.
  - Every result states its smallest detectable effect. About 16 clips can only detect drops of roughly 20 points, and 15/15 passing gives a 95% Wilson lower bound of only about 0.80. A larger benchmark directly buys sensitivity.
- **Slice reporting.** Every metric is reported per slice (night, rain, CCTV vs dashcam, number of views, interior present) from tags in the clip manifest.
- **Production loop.**
  - Officer overrides are logged as labels, with the input hash.
  - Disputed clips are promoted into the gate set.
  - The exact model ID is pinned, with a written shadow/canary procedure for any prompt or model change.
- **Optional, behind a flag.** A second-pass verifier re-checks each cited fact on a ±1 s window around its timestamp (roughly 5–10% extra tokens). It is adopted only after its error rates are measured against owner labels.
