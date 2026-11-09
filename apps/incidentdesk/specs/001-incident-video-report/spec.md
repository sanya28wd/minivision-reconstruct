# Spec 001: Incident Video Analysis Report (MVP)

**Status:** ready-for-agent. The owner approved What You Get on 2026-10-12.
**Date:** 2026-10-12. **Owner:** Joseph S Jose. **Coordinator:** Claude Opus 5.5.
**Governing decisions:** ADR 0001 and 0003–0011 (ADR 0002 is superseded by ADR 0009), GLOSSARY.md, the PRD (`.claude/traffic-incident-agent.prd.md`) and the professor's requirements (docs/context).

## What You Get

### What the officer experiences
1. **They open one web link.** There is no sign-in; the share link is enough, and it can be forwarded. A banner says: *prototype, advisory only, the officer decides.* A notice says videos are processed by an external AI service under a zero-retention setup.
2. **They create an incident and upload videos** into two drop boxes:
   - **Interior** (driver-facing cameras) and **Exterior** (dashcam, other vehicle, CCTV).
   - Several files per box, of any length.
   - Each file gets an optional label ("front", "CCTV junction") and a note on how it was obtained ("DVR export", "phone recording of a screen").
   - Each file is fingerprinted the moment it arrives. The original is kept untouched.
3. **They get one report for the whole incident**, aiming for under 30 seconds (measured, never assumed). It reads like the documents Dubai officers already use, with Arabic and English labels:
   - **Summary:** 3–4 plain sentences.
   - **Parties:** each vehicle or person described in words ("white Toyota Camry taxi"), each with a **pink / green / white** fault status.
   - **Timeline:** every event with its time. Clicking an event plays that exact moment in the right video.
   - **Collisions:** every contact event, including several in one incident. Each shows its accident outcome (simple / with injuries / fatal), accident type and official cause, in Dubai Police and MOI terms.
   - **Violations and fines:** each one tied to its official item number, with the AED amount, black points, impound period and a running total. Figures confirmed only by the press are marked "provisional". Hit-and-run with injury, and any death, are flagged for **Referral** instead of a fine.
   - **Driver behaviour** (from interior views): phone use classed as **entertainment** (handheld, smiling, tapping) or **GPS** (brief glances at a mounted phone); drowsiness events; seatbelt. All with timestamps, and all labelled project assumptions until validated.
   - **Location and conditions:** signboard text read from the video, road type, weather and lighting.
   - **Contradictions** between views, and **Cannot Determine** items, each with its reason. The report never guesses.
   - Findings the AI was unsure of are visibly flagged for the officer.
4. **They approve, or override** any finding with a reason. The AI's original stays visible beside the override. Approval **locks** the report; any later change makes version 2.
5. **They print or save a PDF** made from the same data as the screen. The layout is a swappable template, ready for the RTA's official one.
6. **A "Sample incidents" tab** shows ready-made reports, so reviewers always see a working result even if the AI service is down.
7. **A Feedback button on every screen.** Anyone can type what they'd improve or didn't like. "Feedback on this report" attaches the incident automatically.
8. **Last to be built: a question box.** "Did the taxi signal before turning?" gets an answer that cites timestamps.

### What the builder gets
- **One log of every run** (professor, officer, you, automated tests), labelled by origin, with fingerprints, model version, settings, timings and the final report. You can always trace which report someone saw and why.
- **An append-only, tamper-evident audit trail** of uploads, AI runs, overrides, approvals and exports.
- **A benchmark:** about 25 public research clips to start, growing toward 50 or more, with Gemini-drafted truth you review and override. It includes clearly marked synthetic interior+exterior pairs.
- **Three test layers:**
  - instant logic tests (fines, fault rules, report assembly)
  - free replays of saved AI answers
  - a live benchmark with repeats and honest statistics
- **Automatic guardrails on every report:** citations must point at real moments, every fine has its legal source, no invisible injuries, no unsourced figures.
- **Robustness and trick tests:** distorted copies of clips must give the same facts; trick questions must get "Cannot Determine" or "not present".
- **A regression alarm.** It names exactly which clip and field got worse, or got slower, against your best baseline. Results are broken down by condition (night, rain, CCTV and so on).
- **Officer overrides captured as new labels**, so the benchmark learns from real use.
- **A Feedback inbox** in the admin area, with statuses (new, seen, fixed, won't fix). One `feedback pull` command brings new Feedback into the repo for triage and fixing.
- **Share-link control:** switch links off or rotate them, see visitor sessions, and block one.

### What it won't have yet, by design
- **Processing in the UAE.** No Gemini model runs in a UAE region. Production location waits on the RTA's residency answer (ADR 0009).
- **The official RTA report template.** The professor doesn't have it yet; the layout is swappable.
- **A confirmed new fines schedule.** The schedule in force is MR 178/2017 until a new Cabinet schedule is found. Dubai-only fees are provisional.
- **Proven multi-source fusion accuracy.** No real paired crash footage exists yet; synthetic pairs only show the workflow works.
- **Authenticity or deepfake detection, dispatch, and reconstruction physics.** Out of scope; reconstruction is your collaborator's project.
- **Real-time or in-vehicle use.** This reviews recorded incidents only.

## Problem Statement
RTA and Dubai Taxi officers review incident footage by hand. It is slow, inconsistent, and sometimes wrong, and a wrong call can cost a driver a fine or a job. Footage now arrives from several cameras per vehicle (interior and exterior), from other vehicles, and from CCTV, often longer than 30–40 seconds and sometimes with several collisions in one incident. Officers need to know quickly:
- what happened
- who is at fault under Dubai rules
- which violations and fines apply
- whether driver behaviour (phone use, drowsiness) played a part

They also need every conclusion to be verifiable in seconds. A previous attempt failed because its tests scored internal outputs the officer never saw, its runs were not logged consistently, and its AI invented contact, phantom vehicles and injuries.

## Solution
A web tool where an officer uploads all of an incident's views and receives one detailed, RTA-native Report. Each view is judged on its own first, then the findings are compared; disagreements are shown as Contradictions (ADR 0003). The vision model extracts only **cited facts** per party, in two passes per view: a low-rate overview, then a dense look around each collision moment (ADR 0008). A deterministic Dubai-law **rule engine** turns those facts into fault status, violations, fines and Referrals (ADR 0007). Every claim traces to a view and timestamp or to an official legal source, or is marked provisional or Cannot Determine (ADR 0006). The officer stays the decision-maker: approval, overrides and versioning are recorded with evidence fingerprints. Every run, from the app or from the benchmark, passes through one entry point and one log, and is evaluated with the layered framework in ADR 0005.

## User Stories

### Access and intake
1. As an Officer or Evaluator, I want to open the tool from one forwardable share link with no sign-in, so that I can use it without installing anything and without the builder present.
2. As an Officer, I want a clear banner that the tool is a prototype and advisory, so that I understand I remain the decision-maker.
3. As an Officer, I want a notice that videos are processed by an external AI service under zero retention, so that I know how footage is handled.
4. As an Officer, I want to start a new incident session, so that all the videos I upload belong to one report.
5. As an Officer, I want an Interior drop box and an Exterior drop box, so that I can say which camera faces the driver and which faces the road.
6. As an Officer, I want to drop several files into each box, so that I can submit all cameras from one vehicle plus CCTV or other vehicles.
7. As an Officer, I want to upload long videos (longer than 30–40 seconds), so that the context around the incident is preserved.
8. As an Officer, I want to give each file an optional short label, so that the report refers to cameras in words I recognise.
9. As an Officer, I want to record how each file was obtained, so that the report's evidence is defensible if its authenticity is questioned.
10. As an Officer, I want to type optional context or a question in a separate box, so that I can ask about the incident without it biasing the video analysis.
11. As an Officer, I want upload progress and clear error messages for unsupported or corrupt files, so that I know what was accepted.
12. As an Officer, I want each accepted file to show its fingerprint, so that I can prove later which exact file was analysed.

### Analysis and report
13. As an Officer, I want one Report per incident combining all views, so that I review a single document.
14. As an Officer, I want a 3–4 sentence summary at the top, so that I understand the incident at a glance.
15. As an Officer, I want each Party described by colour, make/model and body type, so that I can identify vehicles without technical IDs.
16. As an Officer, I want each Party's Fault Status shown as pink, green or white, so that the result matches the Dubai Police scheme I know.
17. As an Officer, I want a timeline of events with timestamps, so that I can follow the sequence.
18. As an Officer, I want to click any timestamp and see that moment in the right video, so that I can verify a claim in seconds.
19. As an Officer, I want every Collision in the incident listed, including minor secondary ones, so that nothing is dropped.
20. As an Officer, I want each Collision's Accident Outcome, Accident Type and Cause in official Dubai Police and MOI terms, in Arabic and English, so that the report uses familiar language.
21. As an Officer, I want injuries reported only when visible, with Injury Grade otherwise Cannot Determine, so that no injury is invented.
22. As an Officer, I want each Violation listed against its Party, with the official item number, AED fine, black points and impound period, so that I can apply the penalty correctly.
23. As an Officer, I want a running total of fines and black points per Party and for the incident, so that accumulated penalties are clear.
24. As an Officer, I want figures confirmed only by secondary sources marked "provisional", so that I know which amounts still need confirming.
25. As an Officer, I want hit-and-run with injury, causing a death, and similar criminal matters flagged as Referral, so that they go to police or prosecution rather than receiving a fine.
26. As an Officer, I want Violations listed even when there was no Collision (e.g. phone use, a red-light run), so that offences are not missed.
27. As an Officer, I want "No Incident" only when there is neither a Collision nor a Violation, so that the label is trustworthy.
28. As an Officer, I want Contributing Factors (e.g. the taxi driver's eyes off the road) listed separately from Fault Status, so that the cause and the contributing behaviour are both recorded.
29. As an Officer, I want phone use classified as entertainment or GPS, with the observed evidence (handheld or mounted, durations, tapping, smiling), so that I can judge the classification myself.
30. As an Officer, I want drowsiness events (eye closure, yawning, nodding, drift) with timestamps, so that fatigue is documented as a Contributing Factor.
31. As an Officer, I want seatbelt status reported when visible, so that seatbelt violations are captured.
32. As an Officer, I want signboard text, road names and overlay text (date, time, speed) read and cited, so that location and context are evidenced.
33. As an Officer, I want a precise location only when evidence supports it, and a scene description otherwise, so that the location is never guessed.
34. As an Officer, I want weather, lighting and road conditions noted, so that conditions can be considered in fault.
35. As an Officer, I want Contradictions between views shown explicitly, with both views cited, so that I can resolve them myself.
36. As an Officer, I want every Cannot Determine item to state its reason and, where useful, what evidence would resolve it, so that I know what is missing.
37. As an Officer, I want findings the AI was unsure of (runs disagreeing) visibly flagged, so that I double-check them.
38. As an Officer, I want the report within about 30 seconds for a typical incident, so that the tool is faster than reviewing manually.
39. As an Officer, I want the report sections in a fixed order (summary, parties, timeline, collisions, violations and fines, behaviour, location and conditions, contradictions and Cannot Determine, review), so that every report reads the same way.

### Review, approval and output
40. As an Officer, I want to approve a report, so that it becomes my decision.
41. As an Officer, I want to override any Finding with a mandatory reason, so that I can correct the AI.
42. As an Officer, I want the AI's original Finding kept and shown beside my Override, so that the record shows both.
43. As an Officer, I want an approved report locked, with later changes creating a new version, so that approved decisions cannot be silently altered.
44. As an Officer, I want to print or save the report as a PDF from the same data as the screen, so that the printed and on-screen versions never differ.
45. As an Officer, I want the report layout to be swappable, so that the official RTA template can be adopted later.
46. As an Officer or reviewer, I want a "Sample incidents" tab with ready-made reports, so that I can see the tool working even if the AI service is unavailable.
47. As an Officer, I want to ask a free-text question about the incident and get an answer citing timestamps, so that I can resolve a specific doubt (built last).
48. As an Officer, I want to see past incidents I created, so that I can return to a report.

### Evidence integrity and data protection
49. As the Professor, I want every uploaded file hashed with SHA-256 on arrival and stored unmodified, so that evidence integrity can be demonstrated.
50. As the Professor, I want intake metadata recorded (time, uploader, filename, size, type, container metadata, acquisition method), so that each video's chain of custody is known.
51. As the Professor, I want an append-only, hash-chained audit trail of uploads, views, AI runs, overrides, approvals and exports, so that tampering would be detectable.
52. As the Professor, I want each AI run to record the model ID, prompt version, settings, input hashes and raw output, so that any finding can be traced to how it was produced.
53. As the owner, I want share links I can switch off or rotate, anonymous per-browser sessions I can see and block, and an admin area that needs my own secret, so that access stays controllable without sign-in friction (ADR 0010).
54. As the Professor, I want real footage processed only on the zero-retention Vertex AI setup, never on the free tier, so that NDA obligations are respected.
55. As the Professor, I want real footage blocked until the zero-retention setup is confirmed, so that nothing is sent to a non-compliant service by mistake.

### Feedback
73. As an Officer or Evaluator, I want a Feedback button on every screen, so that I can say what to improve or what I didn't like at any moment.
74. As an Officer or Evaluator, I want "Feedback on this report" to attach the incident, run and report version automatically, so that the owner sees exactly what I saw.
75. As an Officer or Evaluator, I want an optional category, an optional screenshot and an optional name, with only the text required, so that giving Feedback takes seconds.
76. As the owner, I want a Feedback inbox, newest first, filterable by category, with each entry linked to its incident and run, and a status I set (new, seen, fixed, won't fix), so that I can manage improvements.
77. As the owner, I want one command that pulls new Feedback into a local, gitignored, dated Markdown inbox with screenshots, so that it can go through triage and become tickets.
78. As the owner, I want Feedback kept out of incident analysis entirely, so that comments can never change a Report's findings.

### Builder, evaluation and operations
56. As the Builder, I want every run (officer upload, benchmark, replay) to go through one entry point and one log with its Origin, so that I can always trace what someone saw.
57. As the Builder, I want automatic checks on every report (citations point at real moments, legal figures carry sources, no invisible injuries, no fine without a Violation, no raw IDs, officer text kept out of the analysis), so that invalid reports are caught before an officer sees them.
58. As the Builder, I want the rule engine testable without the AI, so that Dubai-law rules and the fines table can be tested exhaustively for free.
59. As the Builder, I want to replay recorded AI answers through the report logic, so that I can test changes without spending quota.
60. As the Builder, I want a live benchmark that runs the gate set 3–5 times per clip and reports per-field pass rates with statistics, so that I know whether a change truly helped.
61. As the Builder, I want each benchmark result to state its smallest detectable effect and how much of it rests on unreviewed truth, so that I don't over-read results.
62. As the Builder, I want metrics broken down by slice (night, rain, CCTV vs dashcam, number of views, interior present), so that I see where the system is weak.
63. As the Builder, I want distorted copies of gate clips (re-encode, brightness, trim, view swap) to give the same facts, so that robustness is tested.
64. As the Builder, I want trick probes (absent party, false premise, temporal trap, cases whose answer is Cannot Determine), so that hallucination is tested.
65. As the Builder, I want a stored Baseline and an automatic comparison naming each clip and field that got worse, or each latency figure that got slower, so that regressions are caught.
66. As the Builder, I want the summary and timeline graded by a different model family, calibrated against my labels, so that narrative quality is checked without Gemini grading itself.
67. As the Builder, I want officer Overrides saved as labelled examples with input hashes, so that the benchmark learns from real use.
68. As the Builder, I want a tool that drafts dense-frame truth for a clip using the saved truth prompt, and a way to record my review and overrides, so that building truth is fast.
69. As the Builder, I want a dataset manifest recording each clip's source, licence, slices, synthetic-pair flag and review status, so that the benchmark is reproducible and honest.
70. As the Builder, I want the model pinned to an exact ID and swappable through one AI connection, so that model or provider changes are deliberate and testable.
71. As the Builder, I want latency recorded per stage, warm and cold, excluding quota retries, so that speed is measured honestly.
72. As the Builder, I want the app deployable to one hosted link with secrets kept server-side, so that the professor can share it with reviewers.

## Implementation Decisions

### Architecture and seams
- **Seam 1: incident entry point.** Given an incident's views (files plus Interior or Exterior role, optional label, acquisition method) and an Origin (app, benchmark or replay), it returns the finished Report data object and writes one Run record. The web app and the evaluation harness both use only this entry point. The Report data object is the single source for the screen page, the PDF and scoring.
- **Seam 2: AI connection.** One boundary through which all model calls pass. Implementations:
  - live Vertex AI (pinned model ID, zero-retention settings)
  - live Gemini API, for public research clips only
  - recorded/replay, which returns saved responses keyed by input hashes and prompt version

  Swapping providers touches only this boundary.
- **Seam 3: rule engine.** Pure and deterministic. It takes the cited facts per Party, Collision and View and returns Fault Status per Party, Violations with official item numbers and amounts, Contributing Factors, Referrals and Cannot Determine items, each linked to the facts and rules that produced it. It contains no model calls.

### Analysis pipeline (inside seam 1)
1. Intake: hash, store the original read-only, record metadata, make a working copy.
2. Per view, an overview pass: native video with audio at a low frame rate. It returns parties, events and candidate collision moments, all cited.
3. Per candidate moment, a dense pass at 15–30 fps on a short window (ADR 0008). The rates and window length are project assumptions to validate.
4. Facts are validated: schema, citations in range, view exists. Repeated runs give an agreement signal per field.
5. Cross-view comparison: findings about the same fact from different views are compared, and disagreements become Contradictions.
6. Rule engine.
7. Report assembly with RTA-native categories and bilingual labels.
8. Guardrail validation. A report that fails is marked invalid.
9. The Run is logged.

- Officer free text never enters steps 2–6. The question box answers separately from the finished Report and the video.

### Domain rules and data
- **Fault Status:** At Fault (pink), Not at Fault (green), Cannot Determine (white). No percentages (ADR 0004).
- **Classification:**
  - Accident Outcome: Simple / Injury / Fatal
  - Injury Grade: Serious / Moderate / Minor, only when visible
  - Accident Type: Collision between vehicles / Run-over / Rollover / Other
  - Cause: from the MOI cause list
- **Violations and fines:** come from a versioned rule and fines table built from the verified MR 178/2017 rows (item numbers), with the nine corrections from research applied. Each row carries its source and a verified or provisional flag (ADR 0006).
- **Referral:** comes from Decree-Law 14/2024 Arts. 38–40.
- **Fault rules:** include the Art. 6 priority rules (main road first; equal roads and roundabouts give way to traffic from the left) and Art. 7(4) pedestrian liability on roads over 80 km/h. Situations the rules don't cover produce Cannot Determine.
- **Behaviour classification:** uses the owner's entertainment cues (handheld, smiling, repeated tapping) and the GPS cue (brief glances at a mounted phone), with industry-referenced, configurable thresholds. Microsleep is about 1.5 s of eye closure, severe about 3 s, and distraction is look-away of about 3–4 s. All are labelled project assumptions. Drowsiness is never fined; it is a Contributing Factor or a cause.

### Storage and integrity
- **Records:** managed PostgreSQL (Cloud SQL; PostgreSQL in every environment, including CI and local) holds incidents, views, runs, findings, overrides, report versions, Feedback, sessions and the hash-chained audit log. Database triggers and permissions block updates and deletes on audit rows. Automated backups and point-in-time recovery are on, and a restore is tested (ADR 0011).
- **Evidence:** original videos, working copies and screenshots are stored in Cloud Storage, write-once and keyed by SHA-256. Nothing persistent is kept on the app server's disk, because Cloud Run's filesystem is in-memory and temporary.
- **Processing limits:** caps on upload size and video duration, a timeout per stage, and processing that never holds whole videos in instance memory.
- Originals are stored read-only; derived copies record their parent hash.
- Report versions: approval freezes a version, and later changes create a new one.
- HTTPS, secure session cookies and rate limits. Secrets are kept server-side. Access follows ADR 0010.

### Technology and deployment (project choices, revisitable)
- Python backend using Google's official Gen AI SDK, which covers both Vertex AI and the Gemini API.
- A lightweight web frontend, with the report page and a print/PDF view rendered from the Report data object. All model text is escaped before rendering.
- Deployed to Google Cloud Run in the same project as Vertex AI, on the $300 Free Trial once the billing test confirms coverage. **Dev, staging and production are separate**, each with its own credentials. Agents never hold staging or production credentials, and production promotion is a manual owner step with fast rollback. Budget alerts and spend caps are set on the billing account and on model usage.
- **Repo guardrails (CI on GitHub Actions):** every change runs format, lint, type check, tests, a dependency audit (lockfile, `pip-audit`, a check that each new package really exists) and secret scanning. A protected-path check fails any change to dataset/truth, dataset/archive, docs/adr or docs/reference outside the allowed tool. A test-integrity guard flags deleted, skipped, xfail-marked or loosened tests for owner approval. Merging is blocked on failure.
- **Architecture note:** a living architecture document (modules, seams, data flow) is kept current, with a refactor-only pass at the midpoint.
- Any framework, library or API limit used must be checked against current official docs when implemented (source-driven-development).

### User interface (from the UI grill, 2026-10-12)
- **Targets:** desktop and laptop first, tablet usable. English interface with bilingual (Arabic and English) report labels. Layout uses logical properties so a full Arabic right-to-left switch can be added later.
- **Look:** the UAE Design System base (open source, MIT; Roboto and Noto Kufi Arabic; WCAG 2.2 AA) with the old project's teal accent (#006E6D). No frosted glass. No government logos or official names (ADR 0004 content only, never branding). Name: "Incident Video Report — prototype", with a BITS Pilani Dubai prototype line.
- **Screens:**
  - incidents list
  - new incident (Interior and Exterior boxes, labels, how each file was obtained, optional question)
  - processing (live steps, leave and return, plain retry on failure, never a half-made report)
  - review workspace
  - print/PDF view
  - sample incidents
  - admin (share links, sessions, Feedback inbox)
- **Review workspace:**
  - The video player sits on the left: switch views or show two side by side, with event and collision markers on the scrubber. Playing highlights the matching timeline event.
  - The report sits on the right in the fixed section order. Clicking any timestamp seeks the right video.
- **Override:** a form takes the new value and a required reason (a short list plus free text). The AI original stays visible and greyed beside the override. One Approve button makes the report read-only, with a "new version" option.
- **Uncertainty display:**
  - Fault chips always carry a text label (At Fault / Not at Fault / Cannot Determine).
  - An amber "Check" badge marks findings where the runs disagreed.
  - Cannot Determine shows its reason inline.
  - A "provisional" tag marks press-only figures.
  - Contradictions get a boxed section.
- **Feedback:** a button on every screen plus an "on this report" variant. Text is required; category, screenshot and name are optional. Context is captured automatically. The admin inbox has statuses and a pull command. Feedback is stored, escaped when displayed, and never enters analysis.
- **Tech:** server-rendered templates with HTMX (server-sent events for progress), a small vanilla video-player script, and print CSS for the PDF. UI work uses the `ui-ux-pro-max` skill, with `design-taste-frontend` for polish only. Evidence: the UI best-practice research note.

### Access (ADR 0010)
- A forwardable share link (long random secret) is enough to use the tool. There is no sign-in and no name entry.
- Each browser gets an anonymous session ID, recorded with the share link used. Actions are attributed to "visitor N via link X".
- The admin can switch off or rotate links, keep several links, list sessions and block one.
- The admin area needs the owner's own admin secret and is never reachable with a share link.

### Run log contract
Each Run records:
- run ID, Origin, timestamp
- input file hashes and view roles
- model ID, prompt version, configuration
- raw model outputs per stage
- final Report object
- guardrail results and status
- per-stage and total latency
- retry and 429 counts, and token usage

## Testing Decisions
- **What a good test is:** it exercises behaviour through a seam and asserts on the Report data object or the rule engine's output, never on internal steps or prompt wording. Tests follow tdd.
- **Seam 3 (rule engine):** exhaustive table-driven tests over facts. Every fines-table row reproduces its official amount, points, impound period and item number. The priority rules, pedestrian liability, Referral triggers, No Incident logic, Contributing Factor separation and Cannot Determine for uncovered cases are all tested. A separate config test validates the fines table itself (no unsourced rows, valid flags).
- **Seam 1 with recorded AI (L1 replay):** each recorded clip produces the expected Report fields. Guardrails reject crafted bad outputs: citation out of range, missing view, fine without a Violation, injury without a citation, raw IDs, officer text leaking into analysis. Approval locking, Overrides, versioning and audit-chain integrity are also tested.
- **Seam 1 live (L2 benchmark):** about 25 clips growing toward 50+, a gate set with 3–5 repeats, exact-match scoring of structured fields, and a cross-family judge for the summary and timeline. Results include McNemar comparisons, clustered bootstrap intervals, the smallest detectable effect, the share of unreviewed truth, slice breakdowns, metamorphic invariance, trick probes, and latency at p50 and p95 (warm and cold). They are compared against the stored Baseline.
- **End-to-end:** at least one test drives the main flow through the real interface: real upload, processing, Report, override, approve, PDF.
- **Test integrity:** tests may not be deleted, skipped or weakened without owner approval. Reviewers read test diffs separately from code diffs.
- **Frontend parity:** the rendered page and the PDF show every field in the Report object. A rendering test checks for presence, plus escaping of model text.
- **Prior art:** none in the working tree, which was cleared on 2026-10-11. The old harness on tag `pre-flush-2026-10-11` is a lesson in what to avoid: it scored intermediate outputs and logged inconsistently. Evaluation design is ADR 0005; evidence is in the research note on advanced eval and guardrails.

## Out of Scope
- Accident reconstruction physics (speeds, trajectories, crush energy), which is a separate project. Reference material is kept separately.
- Authenticity, deepfake or game-footage detection.
- Emergency dispatch.
- Real-time or in-vehicle monitoring.
- UAE-resident model hosting (waits on the RTA's residency answer).
- The official RTA report template (swappable when supplied).
- Telematics inputs (IMU, GPS feed, radar). Video only.
- Fault percentages or comparative-negligence splits.

## Further Notes
- **Open owner and external items:**
  - The RTA's written answers on data residency, the DPA, blurring and retention (questions in the NDA-options research note).
  - The official new fines schedule, if issued.
  - Confirming that the $300 credit pays for Vertex AI (one test call).
  - A real redacted Dubai Police report sample for the layout.
- **Implementation routing:** Codex GPT-6.1 Sol (medium) implements, Claude Sonnet 5.5 (high) reviews, Haiku 5.5 (high) handles bulk work, with manual handoff through task, result and review cards (owner override, 2026-10-12).
- **Estimated size:** about 45–65 tickets. Accuracy tuning against the benchmark is expected to take longer than the build itself.
