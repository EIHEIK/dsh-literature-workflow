"""Deterministic PDF checks, run inside the isolated rendering worker.

These checks do not certify scientific meaning. Inherited original figure labels
are not required to meet generated-body font rules. Without a verified region
classification, generated Chinese text is conservatively required to be 10 pt.
"""
from pathlib import Path
import pymupdf as fitz


def cjk(code):return 0x3400<=code<=0x9fff or 0xf900<=code<=0xfaff


def summarize_failures(failures):
    """Bound glyph diagnostics while preserving every failure occurrence."""
    result=[];groups={}
    for failure in failures:
        if failure['kind'] not in ('generated_chinese_font','visible_text_clipped'):
            result.append(failure);continue
        key=(failure['kind'],failure.get('document'),failure.get('page'))
        if key not in groups:
            group={**failure,'occurrences':0,'bboxSamples':[]}
            if failure['kind']=='generated_chinese_font':group['sizeCounts']={}
            groups[key]=group;result.append(group)
        group=groups[key];group['occurrences']+=1
        if len(group['bboxSamples'])<4:group['bboxSamples'].append(failure['bbox'])
        if 'sizeCounts' in group:
            size=str(failure['size']);group['sizeCounts'][size]=group['sizeCounts'].get(size,0)+1
            group['size']=min(group['size'],failure['size'])
    return result


def check_composition(folder,layout,source_pages):
    folder=Path(folder);failures=[];warnings=[];pages=[]
    rows=layout.get('rows',[])
    if ([r.get('cnPage') for r in rows]!=list(range(1,layout['cnPages']+1)) or
        [r.get('biPage') for r in rows]!=list(range(1,layout['biPages']+1)) or
        set(r.get('sourcePage') for r in rows)!=set(source_pages)):
        failures.append({'kind':'page_mapping','detail':'Incomplete or nonsequential page map'})
    last=0;seen=set()
    for row in rows:
        number=row['sourcePage']
        if number!=last and number in seen:failures.append({'kind':'page_mapping','detail':'Source page groups are not contiguous'})
        seen.add(number);last=number
    for label,filename in [('cn','chinese.pdf'),('bi','bilingual.pdf')]:
        with fitz.open(folder/filename) as doc:
            expected=layout['cnPages' if label=='cn' else 'biPages']
            if len(doc)!=expected:failures.append({'kind':'page_count','document':label})
            for index,page in enumerate(doc):
                if page.rect.width<=0 or page.rect.height<=0:failures.append({'kind':'geometry','document':label,'page':index+1})
                if label!='cn':continue # source side can retain tiny original labels
                count=0;minimum=None;invisible=0
                for span in page.get_texttrace():
                    if span.get('type')==3 or span.get('opacity',1)<.05:
                        invisible+=len(span['chars']);continue
                    # Fully off-page glyphs are not visible. Partially clipped
                    # glyphs still intersect the page and must fail below.
                    han=[ch for ch in span['chars'] if cjk(ch[0]) and page.rect.intersects(fitz.Rect(ch[3]))]
                    if not han:continue
                    count+=len(han);size=float(span['size']);minimum=size if minimum is None else min(size,minimum)
                    if size<9.95:
                        failures.append({'kind':'generated_chinese_font','document':label,'page':index+1,
                            'size':round(size,3),'minimum':10,'bbox':list(span['bbox']),
                            'detail':'Chinese text needs role-aware reflow; tables/captions may use 9 pt only with verified classification'})
                    boundary=fitz.Rect(page.rect);boundary.x0-=1;boundary.y0-=1;boundary.x1+=1;boundary.y1+=1
                    if any(not boundary.contains(fitz.Rect(ch[3])) for ch in han):
                        failures.append({'kind':'visible_text_clipped','document':label,'page':index+1,'bbox':list(span['bbox'])})
                if count==0:
                    failures.append({'kind':'no_visible_chinese','document':label,'page':index+1,
                                     'detail':'Verify source scope and provide a readable translated heading or explanation'})
                pages.append({'page':index+1,'visibleChineseCharacters':count,'minVisibleChinesePt':minimum,'invisibleCharacters':invisible})
                if invisible:warnings.append({'kind':'invisible_text_present','page':index+1,
                    'detail':'Presence only; this does not mean the visible translation is hidden'})
    return {'profile':'strict-generated-cn-v1','hardFailures':summarize_failures(failures),'rawHardFailureCount':len(failures),'warnings':warnings,'pages':pages,
            'limitations':['No proof of meaning, reading order, formula fidelity, table relations or figure-label coverage.',
                            'Independent image and source-text review remains mandatory.']}
