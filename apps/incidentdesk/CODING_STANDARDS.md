# Coding standards (read at review time)

Reviewers apply these on every diff. They are **judgement calls** that no linter can make. Mechanical rules are enforced by guards in `scripts/` (see `docs/CI_GUARDRAILS.md`) and are not repeated here. Each rule names the review that taught it.

## Evidence and decisions
1. **Unknown stays unknown.** A value the evidence didn't supply is `unknown` / Cannot Determine, never a confident default, an inferred zero, or "the most likely" value. Check defaults, fallbacks, `or 0`, `get(..., default)` and exception handlers that substitute a value. *(RPT-11: missing confidence became 1.0.)*
2. **Decide only from typed, cited fields.** Fault, violations and fines come from explicit structured fields, each citing view + timestamp. Never derive them by reading prose, matching keywords, word order or colour names in model text. *(RPT-15: "red SUV … from the secondary road" made the white sedan At Fault.)*
3. **The code owns identifiers.** Anything the system already knows (view id, run id, hashes) is attached by code, never asked from or copied by the model. *(RPT-07/11: the 64-char view id round-tripped through the model.)*

## Sources and claims
4. **Claim only what you read.** An "official limit", rule or article must cite the page actually read (URL + section). Things inferred or remembered are "project assumption, to be validated" or a stricter project rule, worded as such. *(RPT-11: a Gemini schema limit claimed as official wasn't in the docs.)*
5. **Results are claims, not evidence.** Test names, counts and command outputs in a result file must exist and reproduce. The reviewer re-runs `scripts/check_all` and checks that every test name a result cites exists. *(RPT-11 named a non-existent test; RPT-07's resumed summary was stale.)*

## Security and integrity
6. **List every way to change protected state.** For an immutable or append-only store, check all paths: UPDATE, DELETE, TRUNCATE, DROP, ownership/role privileges, bypass settings, and the test helpers that use them (and that `src/` never does). *(RPT-14: TRUNCATE bypassed row triggers.)*
7. **Untrusted bytes stay contained.** External tools fed user files run with fixed arguments and input-only access (e.g. ffprobe `fd:` + protocol/format whitelists); no user text in arguments. *(RPT-06, kept as the pattern.)*
8. **Provider errors are recorded sanitized, never shown raw.** Keep status + message for diagnosis; strip keys, headers, signed URLs and file URIs; show the user a plain message.

## Evidence shown to the officer, and consumers of it
9. **A shown count needs a test where the inputs disagree or are missing.** Any agreement, vote, "N of M" or coverage claim shown to the officer is tested with inputs that name things differently or are absent; a missing input never counts as support, and the text states the true counts. *(Paid gate 2026-10-18: parties absent from two analyses counted as "2 of 3 agree", so a single-analysis decision showed as a majority; RPT-23, FIX-RPT23 and T2b all passed review.)*
10. **A judge, alarm or reader is tested on the real output of its producer.** One end-to-end test feeds the producer's actual output (a small replay run) into the consumer, not a hand-built fixture, and asserts it is accepted. *(RPT-35 merged with synthetic fixtures only; its alarm refused the first real runner file.)*

## Tests
11. **A changed test keeps its strength.** Swapping a fixture (dummy bytes → real synthetic video, plain client → authorized client) is fine if every assertion survives. Changing an expected value needs a stated reason (e.g. the fixture's real duration). Test diffs are reviewed separately from code. *(RPT-06, RPT-11.)*
12. **Expectations follow the source of truth.** Tests derive shipped sets (migrations, rows from a research note) from their source instead of hard-coding counts that every new ticket breaks. *(FIX-MIGRATION-TESTS.)*
