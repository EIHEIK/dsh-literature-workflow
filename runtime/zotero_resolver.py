"""Resolve selections using current Zotero metadata and verified offline scans."""
from pathlib import Path
import re

from core import digest,read_json
from library_index import PDFScanner
from zotero import Zotero,key
from source_text import EXTRACTOR_VERSION


class ZoteroResolver:
    def __init__(self,scan_root,*,client=None):self.scan_root=Path(scan_root).resolve();self.client=client or Zotero()

    def inventory(self,scope):
        parents=list(dict.fromkeys(scope['keys']));omissions=[];inputs=[]
        if scope['kind']=='collection':
            parents=[]
            for start in range(0,10000,100):
                items=self.client.get('/users/0/collections/'+key(scope['keys'][0])+'/items/top?limit=100&start='+str(start))
                if not isinstance(items,list):raise ValueError('Invalid collection response')
                for item in items:
                    value=item.get('key',item.get('data',{}).get('key'))
                    if value in parents:raise ValueError('Repeated collection pagination')
                    parents.append(key(value))
                if len(items)<100:break
            else:raise ValueError('Collection inventory limit exceeded')
        for parent in parents:
            parent=key(parent);meta=self.client.get('/users/0/items/'+parent);meta=meta.get('data',meta)
            if meta.get('itemType') in (None,'attachment','note') or meta.get('deleted'):
                omissions.append({'parentItemKey':parent,'reason':'invalid_parent'});continue
            sources=[];translations=[]
            for child in self.client.children(parent):
                data=child.get('data',child)
                if data.get('itemType')!='attachment' or data.get('deleted') or data.get('parentItem')!=parent:continue
                if re.search(r'中文|译文|译本|双语|中英|translation|bilingual',data.get('title',''),re.I):
                    translations.append(child.get('key',data.get('key')));continue
                if data.get('contentType')=='application/pdf':sources.append(child)
            if translations:
                omissions.append({'parentItemKey':parent,'reason':'existing_translation_requires_revision_selection','attachments':translations});continue
            if len(sources)!=1:
                omissions.append({'parentItemKey':parent,'reason':'source_missing_or_ambiguous','sourceCount':len(sources)});continue
            attachment=key(sources[0].get('key',sources[0].get('data',{}).get('key')))
            inputs.append({'parentItemKey':parent,'sourceAttachmentKey':attachment,'title':meta.get('title') or 'Untitled'})
        return inputs,omissions

    def __call__(self,scope,pages):
        inventory,omissions=self.inventory(scope);inputs=[];seen=set()
        for item in inventory:
            parent=item['parentItemKey'];attachment=item['sourceAttachmentKey']
            try:
                source=Path(self.client.file(attachment));sha=digest(source)
                folder=self.scan_root/EXTRACTOR_VERSION/sha;receipt=read_json(folder/'receipt.json')
                if receipt.get('extractorVersion')!=EXTRACTOR_VERSION or receipt['sourceSha256']!=sha or digest(folder/'output/scan.json')!=receipt['outputSha256']:
                    raise ValueError('Cached source scan changed')
                scan=PDFScanner.validate(folder/'output',sha)
            except (OSError,ValueError,KeyError):
                omissions.append({'parentItemKey':parent,'sourceAttachmentKey':attachment,'reason':'source_requires_offline_index_refresh'});continue
            if sha in seen:
                omissions.append({'parentItemKey':parent,'reason':'duplicate_source_hash'});continue
            seen.add(sha)
            inputs.append({'source':str(source),'title':item['title'],'pages':pages,
                           '_sourceInfo':scan,'zotero':{'libraryType':'user','parentItemKey':parent,'sourceAttachmentKey':attachment}})
        return inputs,omissions
