# MIG-05 report version and package smoke

Status: Parts A–E implemented; automated evidence lives in the donor MIG-05 result.
Browser/PDF smoke prepared for Claude, NOT RUN by Codex.
Use a clearly labelled synthetic report with cited parties, fines and officer decisions.
Do not use real/NDA footage or make a model call for this check.

1. Open the report with a valid share session. In the review workspace, add an override
   with an explanation, approve version 1, and record the version and approval timestamp.
2. Open `/reports/<run_id>?version=1`, then its Print report link. Confirm the URL ends
   `/print?version=1`, and the header identifies version 1 and the same approval timestamp.
   Compare parties, Fault Status words and colour, violations, fine/points/impound values,
   source/item references, storyline, view/time citations, AI originals and officer overrides.
3. Use browser print preview to save an A4 PDF. Inspect EVERY page: table rows and Arabic
   text readable, no clipped columns, fines with source/item visible, citations readable,
   no player/decision controls. PDF is browser output; no server PDF library is introduced.
4. Start version 2, add a different override, inspect its draft print page ('Draft, not
   approved'), approve it. Reopen version 1 screen and print: original decisions and
   approval timestamp unchanged. Its print link must still select version 1.
5. Download approved version 1 and version 2 packages through the same
   share session; a draft and missing/mismatched evidence must be refused before streaming.
   Repeat a build with the same export options and compare ZIP bytes. Confirm one build
   audit entry per successful build, attributed to the actual downloading session.
6. Copy a ZIP into a clean folder and run
   `python -m incident_report.package verify <zip>` in the app environment. Open extracted
   report offline; verify originals and manifest hashes/sizes. Confirm no machine-local
   paths, secrets or active network resources. Check reconstruction only in its labelled
   proposal appendix when enabled; repeat with flag off and an interior view.
7. Change one byte without changing file length, add/remove a payload file, and introduce a traversal
   member in separate copies; each must fail with a readable reason.

Record browser/PDF results and any package-contract exception in the independent review.

Owner-approved format clarifications: manifest inventories every payload except itself;
it records the immutable approval audit hash, while a separate fresh build audit event
records the manifest hash. Inert legal citation hyperlinks remain; active external
resources and machine-local paths/hosts are refused. CSS/fonts are embedded. Current
reconstruction/driver export selections are explicit in export_options; source review
records are anchored to the approval snapshot (legacy approvals use a timestamp cutoff).
App commit is unknown when the original report did not capture that provenance.

## Round 2 note (mig-05-r2)
Extra manual checks after the R2 fixes:
8. Add an override reason with Arabic text after a colon, a bare slash word ("checked /ref", "lane (/left)")
   and "60 km/h"; approve and download the package. It must download; a reason containing a real machine
   path (`C:\Users\x`, `/home/x`, `\server\share`) must be refused with a message naming the payload file.
9. Timestamps in `manifest.json`, `decisions.json` and the printed approval line end in `+00:00` (UTC).
10. `python -m incident_report.package verify` refuses a copy with a ZIP comment, extra bytes before or
    after the archive, a compressed member, or more than the member/size bounds (engineering bounds, not standards).

## Round 3 note (mig-05-r2)
11. Keep the allowed Arabic/slash examples in step 8, plus `12:30` and `ratio 3:1`.
    Reasons containing `path:C:\Users\x`, `Note:C:/Users/x`, `{C:/Users/x}` or
    `path:/home/x/a` must be refused with the payload filename. Also check `/root/`,
    `/srv/`, `/Volumes/`, `/private/`, `/var/` and `~/` paths, including mixed-case roots.
12. Automated regressions isolate evidence-root wiring with a synthetic configured root
    outside every generic known-root rule; the real evidence stays in its fixture store.
    They also refuse CSS backslashes in style blocks/embedded stylesheets, retain the
    style-attribute refusal, and validate actual print HTML plus a complete built package.
