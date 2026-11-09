---
status: accepted
date: 2026-10-12
---

# Every claim is traceable to its source; nothing is invented (project-wide constraint)

Officers use the report to fine people, and AI output is unpredictable. So every claim in code, data and the report traces to a source, or is visibly marked as not verified.

- **Video findings.** Each Finding cites its view and timestamp, and the L0 citation validator rejects any finding without one (ADR 0005).
- **Legal figures.** Every fine, black-point count, impound period, article or rule carries its official source and item number. Today that is Ministerial Resolution 178/2017 as amended, and Federal Decree-Law 14/2024 for criminal Referral. A figure confirmed only by secondary sources is shown as **provisional**. A figure with no source is not used.
- **Categories and wording** follow ADR 0004 (RTA-native).
- **Thresholds and parameters** (behaviour timings, sampling rates, confidence cut-offs) cite the research or measurement they come from, or are labelled "project assumption, to be validated".
- **When evidence or a source is missing,** the output is Cannot Determine or provisional, never a plausible-looking value.

This applies to every agent and every model, in specs, tickets, code, tests and docs. A ticket that adds an unsourced figure fails review.
