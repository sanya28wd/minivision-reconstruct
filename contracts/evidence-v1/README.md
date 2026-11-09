# Evidence contract 1.0

The report app accepts a **local directory** containing `bundle.json` and only its
listed artifacts. ZIP extraction is deliberately not offered. The JSON Schemas use
Draft 2020-12. The stdlib/Pydantic importer does not import the worker, AI package,
network clients, or execute bundle contents. Persistence accepts an already-open
local application PostgreSQL connection; it never creates a connection itself.

`bundle.json` is the import envelope. It pins producer git revision, case, creation
time, local-only policy, config/model identifiers, artifact SHA-256s and one source
binding per externally assigned view. Each binding pins reconstruction, movement,
incident and review runs, tracking/calibration revisions, original-video digest,
metadata, zero-based frame convention and transformation history. Video bytes stay
outside these JSON fixtures; a digest binding is not authentication or proof the
provided video is genuine. Upstream absolute source paths are preserved inertly;
resolution uses included relative artifact names and digests only.

Normalized records preserve the full original payload. They distinguish source
bindings/frame maps, namespaced track references, geometry, event proposals,
assessments, review overlays and sequence entries. Identity hashes encode separate
case/view/tracking-run/revision/track-ID components; track references are never
parties. Producer event IDs remain in `original.event_id`; source artifact path and
view identify them across runs. Geometry retains its original units, calibration
manifest link, uncertainty and nulls; no unit conversion occurs. Ordering ambiguity
and supporting/contrary evidence are exposed in the discriminated typed `data` payload
and also retained unchanged in `original`. MIG-03 consumes typed fields only.

The source timestamp table is authoritative. Frame-based citations and typed event
interval times use its canonical time/provenance within the documented tolerance.
Producer values, windows, phases and signals remain unchanged in `original`. An
off-table correction has null citation fields and needs_review, never the old frame.
Replay/export time never substitutes for source time.

Review revisions are complete snapshots, hash-chained to earlier snapshots and the
incident manifest. The latest snapshot alone supplies decisions in force, including
an empty snapshot. Original and effective values remain separate. Reviewer strings
are unauthenticated. Parent event status is checked for each assessment. Every
record has `affirmative=false`, `officer_approved=false` and `rule_source=null`:
acceptance here means structural/provenance acceptance, never accepted fault facts.
MIG-03/MIG-04 must implement their own authenticated officer review boundary.

Unknown versions, unsafe bundle paths, malformed/oversized JSON and hash mismatches
reject the whole bundle. Missing upstream evidence quarantines all normalized
records as `incomplete_provenance`; available originals remain listed. Empty candidate
lists are explicit missing-input states, never a finding of no incident. Original
artifact bytes, raw envelope, hashes, decisions and normalized rows are stored in
append-only tables by migration 0020 (with a destructive down migration for disposable
data). Byte-identical imports return the existing decision; changed runs/reviews
produce distinct imports. Database owner/superuser privileges can still drop tables
or disable triggers; this is an application immutability guarantee, not protection
against the database administrator.

Resource budgets in `models.py` are **project assumptions, to be validated**, not
scientific thresholds: 16 MiB/file, 256 KiB/record, 64 MiB/bundle, 512 files, 1024
filesystem entries and 100,000 parsed records. Input must be on a fixed local drive
on Windows; UNC/device paths, symlinks, junctions, linked ancestors and special files
are refused. Supply a stable directory; this is not an OS sandbox against another
process concurrently replacing filesystem ancestors.

Versioning: contract `1.0`, importer `1.4.0`. Unknown contract or producer schema
versions reject rather than guessing compatibility. Regenerate schemas from the
Pydantic models when the reviewed contract changes. No existing app behavior or
fault engine is connected to this module.

Unsupported crop/rotation/scaling transformations reject explicitly; the currently
supported transformation descriptor is the producer reconstruction replay with its
relative frame-map path. Native source tables support variable frame rates without
inventing a frame/rate conversion. JSON text that PostgreSQL JSONB cannot represent
(NUL or invalid Unicode) rejects while its raw bytes stay in BYTEA. If reading is
refused before a complete content identity is available, the rejection has a unique
attempt ID; it cannot be deduplicated as an unrelated unreadable attempt. Such
unreadable attempts have no claimed byte-identity guarantee.

## Round 2 timestamp compatibility
Source citations resolve by zero-based frame and use the timestamp table's value
and provenance. `TIMESTAMP_TOLERANCE_SECONDS = 1e-9` seconds is a **project assumption,
to be validated**, permitting serialization noise, not alignment or a frame-sized
shift. Producer values remain unchanged in `original`. Time-to-frame corrections
use the same tolerance and must match exactly one table entry. Larger/ambiguous
corrections retain null citations and needs_review; they are quarantined separately
from structurally verified provenance.

Round 2 derived artifact classification (owner ruling): absent `replay.mp4` in a
reconstruction manifest is derived/presentation (producer
`src/minivision_reconstruct/reconstruction/pipeline.py:260,287`); absent
`trajectories.csv` in a movement manifest is a derived tabulation of smoothed/movement
JSON records (`src/minivision_reconstruct/movement/pipeline.py:146,181`). Only these
exact role/name combinations are classified `not_supplied_derived`, retaining both
artifact and manifest SHA-256. Present files always undergo hash verification.
JSON/JSONL, configs, source tables, tracks, calibration and revision artifacts remain
evidence-bearing: absence makes provenance incomplete. Empty candidate lists remain
an explicit missing-input observation, not an incomplete provenance chain or a
No Incident conclusion. 

`structurally_accepted_records` means validated structure/provenance, never approved
facts. Needs-review records are quarantined even when the provenance chain is verified.
IDs include importer_version as well as relative paths and byte hashes, allowing a
new importer to re-decide the same bytes without overwriting an old immutable import.
Duplicate video digests or shared run manifests across source views reject.

Migration 0021 adds append-only failed-read blobs keyed by content SHA-256 and
per-attempt artifact references, so distinct interrupted attempts share the same
stored bytes. 0020 is unchanged. Both new tables block UPDATE/DELETE/TRUNCATE and
have a down migration. No raw-envelope copy is duplicated in the parent row for an
incomplete read. The fixture packager reads the actual producer revision from git;
it does not require changes to the producer or rewrite checked-in producer artifacts.

Historical importer 1.0.0 decisions load as `LegacyImportSnapshot`, exposing their
unchanged original_decision, raw_envelope and raw_artifacts. They are deliberately
not upgraded to typed evidence: absent typed fields stay absent. Current import IDs
include version 1.2.0, so re-importing bytes yields a new decision while the historical
snapshot remains queryable and immutable. Consumers must distinguish the two types.

Round 3: typed sequence peaks resolve through their movement event's peak frame,
using the same table and tolerance as event citations. Sequence time-window endpoints
resolve only to unique source table times within that tolerance. Off-table uncertainty
bounds (for example a half-frame bound) remain null in the typed window and mark the
sequence needs_review; the producer's complete raw window remains in original. No
interpolation or nearest-frame snap is invented. Provenance can still be verified
while these sequence records are quarantined. Importer version is now 1.2.0.

A bundle's envelope case_id is the shared application case. SourceBinding may declare
producer_case_id to pin each producer run's original case; if omitted it defaults to
the envelope case for existing single-view bundles. Source manifest and calibration
identities are checked against that producer case, without rewriting artifacts.
Track identities still use the shared application case plus view/run/revision/track.
This permits two genuine videos/runs in one shared case while duplicate videos/run
manifests still reject. Typed source bindings expose producer_case_id explicitly.

ImportDecision.missing_inputs is a typed list of application codes, including
no_incident_candidates, calibration_failed, calibration_unavailable,
source_timestamps_unavailable, source_timestamps_incomplete and upstream_artifacts_missing.
It is populated from the importer's explicit validation issues, including when
loading prior typed decisions that lacked this field. An empty candidate list can
have verified file provenance and still carries no_incident_candidates. No consumer
may interpret verified provenance or an empty event list as No Incident.

An off-table producer review correction also emits a needs_review issue naming the
producer target event and requested time. This is a review diagnostic, not missing
upstream evidence: provenance may remain verified, but the overlay stays quarantined
with null citation fields. All fault/approval flags remain false.

MIG-03 L1 supersedes Round 3's frame-only window endpoints: sequence time_window is
a source-time interval. Matching bounds use canonical table times; other bounds stay
unchanged. covering_frames records the first/last source frames inside the closed
interval. A window quarantines only if no frame lies inside; a separately unresolved
peak still needs review. Raw windows remain untouched. No interpolation is invented.

MIG-03 L3: each raised importer diagnostic now carries a structured code. Message
text is retained in issues for old consumers; issue_codes is parallel to it and
missing_inputs is a tuple derived from codes only. Previously stored missing_inputs
lists remain readable as tuples; older records lacking codes remain readable without
inventing codes from their historical text. Rewording messages leaves missing_inputs
unchanged. Importer version 1.3.0 identifies these revised decisions.

Importer 1.4.0 adds optional `class_name` to the typed track reference, copied from
the producer observation, for the unmapped-track display. Historical records without
this field remain readable as unknown class; they must be re-imported to gain it.
Track associations remain reviewer-set display data, separate from fault and AI.

Diagnostic mapping: both "missing source bindings" and "assessment parent event
missing" emit `upstream_artifacts_missing`. These are missing-input states, never
evidence of no incident. Reversed or wrong-length sequence windows quarantine only
that row, retain its raw window in `original`, and expose unknown normalized bounds.
