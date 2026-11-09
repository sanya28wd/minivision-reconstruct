"""MIG-02 non-metric producer fixture with explicit lost/interpolated input states (WSL)."""
from dataclasses import replace
from pathlib import Path
import cv2
from pydantic import TypeAdapter
from minivision_reconstruct.domain.models import CalibrationDocument
from minivision_reconstruct.calibration import build_calibration, save_calibration_revision
from minivision_reconstruct.incident.staged_fixture import scenario_script, INCIDENT_LANES
from minivision_reconstruct.movement.staged_fixture import render_staged_case, FPS
from minivision_reconstruct.reconstruction.pipeline import automatic_track_input, prepare_reconstruction
from minivision_reconstruct.movement.pipeline import analyze_movement
from minivision_reconstruct.incident.pipeline import analyze_incident
from minivision_reconstruct.incident.review import export_reviewed_view

root=Path('../cases').resolve()
case_id='mig02-relative'
script=scenario_script('occluded_impact',1)
users=tuple(replace(user,interpolated=(3.0,3.2)) for user in script.users)
ffmpeg=str(root.parent/'linux-tools/ffmpeg/bin/ffmpeg')
ffprobe=str(root.parent/'linux-tools/ffmpeg/bin/ffprobe')
rendered=render_staged_case(root,case_id,users,1,0,int(script.duration_seconds*FPS),'week8-incident',ffmpeg,ffprobe,INCIDENT_LANES)
case=root/case_id
old=TypeAdapter(CalibrationDocument).validate_json((case/'analysis/calibration/revision-001/calibration.json').read_bytes())
# The same arbitrary coordinate plane is deliberately declared non-metric, with no physical-distance claim.
calibration=build_calibration(case_id,old.source_video_sha256,old.image_width,old.image_height,'non_metric','relative_units',old.control_points,old.validation_points,old.validation_tolerance,old.features)
video=next((case/'original').iterdir())
capture=cv2.VideoCapture(str(video))
ok,image=capture.read(); capture.release()
if not ok: raise RuntimeError('cannot read synthetic source frame')
revision=save_calibration_revision(case,calibration,image)
prepare_reconstruction(root,case_id,automatic_track_input(root,case_id,'staged-tracks'),revision/'calibration.json','week8-incident-relative',ffmpeg)
analyze_movement(root,case_id,'week8-incident-relative',Path('configs/movement.json').resolve(),'mig02-movement')
run=analyze_incident(root,case_id,'mig02-movement',Path('configs/incident.json').resolve(),'mig02-incident')
export_reviewed_view(run,'initial')
print(case_id,flush=True)
