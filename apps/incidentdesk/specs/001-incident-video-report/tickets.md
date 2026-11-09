# Spec 001 ticket index (RPT-01 … RPT-54)

Priority order; every blocker precedes the ticket. Live = spends model quota. All tickets after RPT-02 must pass CI.

| # | Ticket | Blocked by | Quota |
|---|---|---|---|
| RPT-01 | Walking skeleton: one upload to a minimal report, run logged | — | free |
| RPT-02 | CI and repo guardrails | RPT-01 | free |
| RPT-03 | Persistent storage and backups (Cloud Storage + PostgreSQL) | RPT-01, RPT-02 | free |
| RPT-04 | UI foundation: UAE Design System base, teal accent, bilingual labels | RPT-02 | free |
| RPT-05 | Dataset manifest | RPT-02 | free |
| RPT-06 | Evidence intake: fingerprint, untouched original, metadata | RPT-03 | free |
| RPT-07 | Live Gemini connection: overview pass on one view | RPT-02 | live |
| RPT-08 | Verified fines table (MR 178/2017) | RPT-02 | free |
| RPT-09 | Share-link access, anonymous sessions, separate admin | RPT-02 | free |
| RPT-10 | Get the benchmark clips (bulk + owner) | RPT-05 | free |
| RPT-11 | Fact schema and citation checker | RPT-07 | free |
| RPT-12 | Vertex AI zero-retention connection and real-footage block | RPT-07 | live |
| RPT-13 | Truth drafting tool and owner review | RPT-05, RPT-12 | live |
| RPT-14 | Tamper-evident audit trail | RPT-06 | free |
| RPT-15 | Rule engine skeleton: pink/green/white per Party | RPT-11 | free |
| RPT-16 | Replay test harness (L1) | RPT-07, RPT-11 | free |
| RPT-17 | Violations and fines in the Report | RPT-08, RPT-15 | free |
| RPT-18 | Core fault rules | RPT-15 | free |
| RPT-19 | Dense collision pass | RPT-11 | live |
| RPT-20 | New-incident screen: Interior and Exterior drop boxes | RPT-04, RPT-06, RPT-07 | free |
| RPT-21 | Processing screen: live steps, leave and return, retry | RPT-04, RPT-07 | free |
| RPT-22 | Review workspace: video player, markers, click-to-seek | RPT-04, RPT-11 | free |
| RPT-23 | Agreement signal: trust, flag, Cannot Determine | RPT-11 | live |
| RPT-24 | Live benchmark runner (L2) | RPT-10, RPT-13, RPT-16 | live |
| RPT-25 | Multiple collisions and Dubai classification | RPT-15, RPT-19 | free |
| RPT-26 | No Incident logic and Contributing Factors | RPT-17 | free |
| RPT-27 | Referral (Decree-Law 14/2024 Arts. 38–40) | RPT-17 | free |
| RPT-28 | RTA-native report in the workspace | RPT-04, RPT-17, RPT-22 | free |
| RPT-29 | Cross-view comparison and Contradictions | RPT-15, RPT-20 | free |
| RPT-30 | Phone use (entertainment vs GPS) and seatbelt | RPT-11, RPT-20 | live |
| RPT-31 | Location and conditions | RPT-11 | live |
| RPT-32 | Drowsiness events | RPT-26, RPT-30 | live |
| RPT-33 | Remaining guardrails | RPT-17, RPT-23, RPT-25 | free |
| RPT-34 | Statistics and slices | RPT-24 | free |
| RPT-35 | Baseline and regression alarm | RPT-34 | free |
| RPT-36 | Error-analysis pass | RPT-24 | live |
| RPT-37 | Architecture note and refactor-only pass | RPT-18, RPT-28 | free |
| RPT-38 | Approve, override, lock, versions | RPT-09, RPT-14, RPT-28 | free |
| RPT-39 | Print/PDF with parity | RPT-28 | free |
| RPT-40 | Sample incidents tab | RPT-28 | free |
| RPT-41 | Incidents list | RPT-09, RPT-21 | free |
| RPT-42 | End-to-end test of the main flow | RPT-20, RPT-21, RPT-38, RPT-39 | free |
| RPT-43 | Feedback button and form | RPT-09, RPT-14 | free |
| RPT-44 | Admin page: share links, sessions, Feedback inbox | RPT-09, RPT-43 | free |
| RPT-45 | Feedback pull command | RPT-44 | free |
| RPT-46 | Distortion (metamorphic) suite | RPT-24 | live |
| RPT-47 | Trick probes | RPT-24 | live |
| RPT-48 | Second-model judge for summary and timeline | RPT-24 | live |
| RPT-49 | Overrides become labels | RPT-05, RPT-38 | free |
| RPT-50 | Model pinning and canary procedure | RPT-35 | free |
| RPT-51 | Deploy to Cloud Run | RPT-09, RPT-12 | free |
| RPT-52 | Security review before sharing | RPT-38, RPT-44, RPT-51 | free |
| RPT-53 | Question box (last feature) | RPT-28, RPT-33 | live |
| RPT-54 | Demo readiness | RPT-35, RPT-42, RPT-52 | live |
