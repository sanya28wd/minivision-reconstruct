"""Generate incident analyses and reviews with unchanged producer APIs (run in WSL).

Prerequisite: scripts/create_incident_fixture.py has created the MIG-02 cases.
"""
from pathlib import Path
from minivision_reconstruct.movement.pipeline import analyze_movement
from minivision_reconstruct.incident.pipeline import analyze_incident
from minivision_reconstruct.incident.review import load_incident_run, save_review_revision, export_reviewed_view
from minivision_reconstruct.incident.models import ReviewDecision

root = Path('../cases').resolve()
for case in sorted(root.glob('mig02-*')):
    if not case.is_dir() or (case/'analysis/incident/mig02-incident').exists():
        continue
    analyze_movement(root,case.name,'week8-incident',Path('configs/movement.json').resolve(),'mig02-movement')
    run=analyze_incident(root,case.name,'mig02-movement',Path('configs/incident.json').resolve(),'mig02-incident')
    events=load_incident_run(run).events
    decisions=[]
    if events:
        for index, action in enumerate(('confirm','reject','modify')):
            event=events[index % len(events)]
            decision=ReviewDecision(target_kind='event',target_id=event.event_id,action=action,note='Synthetic MIG-02 review '+action,modified_peak_time=event.peak_time+0.001 if action=='modify' else None,modified_location=None,modified_outcome=None,modified_contributing_track_ids=None,reviewer='synthetic-fixture-reviewer',decided_at='2026-10-30T00:00:00Z')
            decisions=[item for item in decisions if item.target_id!=event.event_id]+[decision]
            revision=save_review_revision(run,tuple(decisions),'synthetic-fixture-reviewer')
            export_reviewed_view(run,revision.name)
    else:
        export_reviewed_view(run,'initial')
    print(case.name,len(events),flush=True)
