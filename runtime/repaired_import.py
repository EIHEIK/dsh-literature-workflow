"""Maintenance-created repair imports keep semantic rounds and artifact hashes."""
from pathlib import Path
from core import digest,read_json


EXTRAS={'supplements':'supplements.json','sourceQuotes':'source-quotes.json','repairApplied':'repair-applied.json'}


def validate_import(imported,source_sha,pages):
    rounds=imported.get('repairRoundUsed',0)
    if type(rounds) is not int or not 0<=rounds<=2:raise ValueError('Invalid imported repair count')
    if not rounds:
        if any(k in imported for k in ('repairPackage',*EXTRAS)):raise ValueError('Repair artifacts cannot reset semantic rounds')
        return 0
    receipt=imported.get('repairPackage',{});path=Path(receipt.get('path','')).resolve(strict=True)
    if digest(path)!=receipt.get('sha256'):raise ValueError('Repair package receipt changed')
    package=read_json(path);folder=path.parent/'output'
    if (package.get('sourceSha256')!=source_sha or package.get('sourcePages')!=pages or
        package.get('repairRoundUsed')!=rounds or package.get('acceptance')!='not_granted' or
        package.get('fullIndependentReviewRequired') is not True):raise ValueError('Repair package identity or review boundary changed')
    for label,name in {'mono':'mono.pdf',**EXTRAS}.items():
        entry={'path':imported['path'],'sha256':imported['sha256']} if label=='mono' else imported.get(label,{})
        target=Path(entry.get('path','')).resolve(strict=True)
        if target!=folder/name or digest(target)!=entry.get('sha256') or entry['sha256']!=package['files'][name]:
            raise ValueError('Repair import artifact changed')
    mapping_path=folder/'mapping.json'
    if digest(mapping_path)!=package['files']['mapping.json'] or read_json(mapping_path)['sourcePages']!=imported['mapping']:
        raise ValueError('Repair import page mapping changed')
    groups=[]
    for page in imported['mapping']:
        if type(page) is not int:raise ValueError('Invalid repaired source page')
        if not groups or groups[-1]!=page:groups.append(page)
    if groups!=pages:raise ValueError('Repaired source groups changed')
    return rounds
