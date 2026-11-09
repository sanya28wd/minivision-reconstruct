"""Regenerate the evidence-v1 schemas from the app's typed contract (app environment)."""
from pathlib import Path
import json
from incident_report.evidence_import.models import Envelope, SourceBinding, NormalizedRecord, ImportDecision
from incident_report.evidence_import import payloads
root=Path(__file__).resolve().parents[1]/'contracts/evidence-v1/schema'
for name,model in {'envelope':Envelope,'source-binding':SourceBinding,'normalized-record':NormalizedRecord,'import-decision':ImportDecision}.items():
    schema=model.model_json_schema(mode='serialization')
    schema['$schema']='https://json-schema.org/draft/2020-12/schema'
    (root/f'{name}.json').write_text(json.dumps(schema,indent=2)+'\n',encoding='utf-8')
for name,model in [('event-proposal',payloads.EventFields),('geometry',payloads.GeometryFields),('track-reference',payloads.TrackFields),('review-overlay',payloads.ReviewFields),('assessment',payloads.AssessmentFields),('sequence',payloads.SequenceFields)]:
    schema=NormalizedRecord.model_json_schema(mode='serialization')
    schema['$schema']='https://json-schema.org/draft/2020-12/schema'
    schema['properties']['kind']={'const':name.replace('-','_')}
    schema['properties']['data']={'$ref':f'#/$defs/{model.__name__}'}
    (root/f'{name}.json').write_text(json.dumps(schema,indent=2)+'\n',encoding='utf-8')
