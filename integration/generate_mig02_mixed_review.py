"""Generate a synthetic two-contact producer scene and one mixed-action review (WSL)."""
from dataclasses import replace
from pathlib import Path
import numpy as np
from minivision_reconstruct.incident.staged_fixture import scenario_script, INCIDENT_LANES
from minivision_reconstruct.movement.staged_fixture import render_staged_case, FPS
from minivision_reconstruct.movement.pipeline import analyze_movement
from minivision_reconstruct.incident.pipeline import analyze_incident
from minivision_reconstruct.incident.review import load_incident_run, save_review_revision, export_reviewed_view
from minivision_reconstruct.incident.models import ReviewDecision
root=Path('../cases').resolve()
case_id='mig02-multi-review'
script=scenario_script('rear_end',1)
second=tuple(replace(user,track_id=user.track_id+2,path=replace(user.path,points=user.path.points+np.array([8.0,0.0]))) for user in script.users)
render_staged_case(root,case_id,script.users+second,1,0,int(script.duration_seconds*FPS),'week8-incident',str(root.parent/'linux-tools/ffmpeg/bin/ffmpeg'),str(root.parent/'linux-tools/ffmpeg/bin/ffprobe'),INCIDENT_LANES)
analyze_movement(root,case_id,'week8-incident',Path('configs/movement.json').resolve(),'mig02-movement')
run=analyze_incident(root,case_id,'mig02-movement',Path('configs/incident.json').resolve(),'mig02-incident')
loaded=load_incident_run(run)
if len(loaded.events)<2 or not loaded.assessments: raise RuntimeError('need two candidates and an assessment')
targets=[('event',loaded.events[0].event_id,'confirm',None),('assessment',loaded.assessments[0].assessment_id,'reject',None),('event',loaded.events[1].event_id,'modify',loaded.events[1].peak_time+0.001)]
decisions=tuple(ReviewDecision(target_kind=kind,target_id=target,action=action,note='Synthetic mixed-action review',modified_peak_time=time,modified_location=None,modified_outcome=None,modified_contributing_track_ids=None,reviewer='synthetic-fixture-reviewer',decided_at='2026-10-30T00:00:00Z') for kind,target,action,time in targets)
revision=save_review_revision(run,decisions,'synthetic-fixture-reviewer')
export_reviewed_view(run,revision.name)
print(case_id,len(loaded.events),[d.action for d in decisions],flush=True)
