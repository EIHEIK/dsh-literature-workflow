"""Narrow syntax normalization; never invent review decisions or evidence."""
import json
from core import CHECKS


def load_review(text):
    text=text.strip()
    repaired=False
    try:value=json.loads(text)
    except json.JSONDecodeError as error:
        # Observed provider output omitted one closing object delimiter. Only
        # attempt an EOF closure; no string/token edits or missing field filling.
        if error.pos!=len(text.rstrip()):raise
        value=json.loads(text.rstrip()+'}');repaired=True
    if not isinstance(value,dict):raise ValueError('Review must be an object')
    checks=value.get('checks')
    misplaced=('issues','verdict','summary','corpusReview')
    if set(value)=={'unitId','candidateId','checks'} and isinstance(checks,dict) and all(k in checks for k in (*CHECKS,*misplaced)):
        if 'sourceEvidenceReview' in checks:misplaced+=('sourceEvidenceReview',)
        value={**value,**{k:checks[k] for k in misplaced},'checks':{k:v for k,v in checks.items() if k not in misplaced}}
        value['formatNormalization']='provided root fields moved out of checks; values unchanged'
    elif repaired:
        if not all(k in value for k in ('unitId','candidateId','checks',*misplaced)):
            raise ValueError('Missing review fields cannot be repaired')
        value['formatNormalization']='one final object delimiter restored; values unchanged'
    return value
