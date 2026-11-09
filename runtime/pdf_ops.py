"""Source-preserving, explicitly mapped CN and side-by-side PDF composition."""
from pathlib import Path
import re
import pymupdf as fitz
from core import atomic

def header(page, text):
    page.insert_text((16,17),text,fontsize=10,color=(.25,.25,.25))

def mixed_width(text,size=12):
    return sum(fitz.get_text_length(c,fontname='helv',fontsize=size) if ord(c)<128 else size for c in text)

def mixed_line(page,text,x,y,size=12):
    for run in re.findall(r'[\x00-\x7f]+|[^\x00-\x7f]+',text):
        page.insert_text((x,y),run,fontname='helv' if ord(run[0])<128 else 'china-s',fontsize=size)
        x+=mixed_width(run,size)

def compose(source, mono, mapping, supplements, folder):
    folder=Path(folder); preview=folder/'preview';preview.mkdir()
    src=fitz.open(source); translated=fitz.open(mono); cn=fitz.open(); bi=fitz.open(); rows=[];parts={}
    def pair(doc,page_no,source_page,kind):
        p=doc[page_no]; original=src[source_page-1]
        parts[source_page]=parts.get(source_page,0)+1
        continuation=f' | Continuation {parts[source_page]-1}' if parts[source_page]>1 else ''
        c=cn.new_page(width=p.rect.width,height=p.rect.height+28)
        c.show_pdf_page(fitz.Rect(0,28,p.rect.width,p.rect.height+28),doc,page_no)
        header(c,f'Source PDF p. {source_page} | Chinese page {len(cn)}'+continuation+(' | Supplement' if kind=='supplement' else ''))
        b=bi.new_page(width=original.rect.width+p.rect.width+12,height=max(original.rect.height,p.rect.height)+28)
        b.show_pdf_page(fitz.Rect(0,28,original.rect.width,original.rect.height+28),src,source_page-1)
        b.show_pdf_page(fitz.Rect(original.rect.width+12,28,original.rect.width+12+p.rect.width,p.rect.height+28),doc,page_no)
        header(b,f'Source PDF p. {source_page} | Original (left) / Chinese (right) | Pair {len(bi)}'+continuation)
        rows.append({'sourcePage':source_page,'cnPage':len(cn),'biPage':len(bi),'kind':kind,'sourcePart':parts[source_page]})
    def supplement(item):
        extra=fitz.open(); lines=[]
        text=(item.get('title','图表中文对照')+'\n'+item['text']).replace('\t','    ')
        for line in text.splitlines():
            current=''
            for char in line:
                if mixed_width(current+char)>519:lines.append(current);current=''
                current+=char
            lines.append(current)
        for offset in range(0,len(lines),38):
            page=extra.new_page(width=595,height=842)
            for n,line in enumerate(lines[offset:offset+38]):
                mixed_line(page,line,38,48+n*19)
        for n in range(len(extra)): pair(extra,n,item['sourcePage'],'supplement')
        extra.close()
    # Keep source-page explanations adjacent to the corresponding translation,
    # including when a source page occupies several Chinese continuation pages.
    ordered=sorted(range(len(mapping)),key=lambda index:mapping[index])
    for position,idx in enumerate(ordered):
        source_page=mapping[idx]
        pair(translated,idx,source_page,'translation')
        if position+1==len(ordered) or mapping[ordered[position+1]]!=source_page:
            for item in supplements:
                if item['sourcePage']==source_page:supplement(item)
    cn.save(folder/'chinese.pdf',garbage=4,deflate=True);bi.save(folder/'bilingual.pdf',garbage=4,deflate=True)
    layout={'cnPages':len(cn),'biPages':len(bi),'rows':rows,'mappingStatus':'requires_independent_verification',
            'scope':'Full declared source scope; supplemental pages are explicitly marked'}
    failures=[]; warnings=[]; details={}
    for label,doc in [('source',src),('cn',cn),('bi',bi)]:
        pages=sorted(set(mapping)) if label=='source' else range(1,len(doc)+1)
        details[label]=[]
        for number in pages:
            p=doc[number-1];text=p.get_text()
            if p.rect.width<=0 or p.rect.height<=0: failures.append(f'{label} page {number}: invalid geometry')
            if len(text.strip())<40: warnings.append(f'{label} page {number}: little searchable text; inspect raster content')
            p.get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False).save(preview/f'{label}-{number:04}.png')
            details[label].append({'page':number,'textLength':len(text),'size':[p.rect.width,p.rect.height]})
    atomic(folder/'layout.json',layout)
    atomic(folder/'qa.json',{'hardFailures':failures,'warnings':warnings,'pages':details,
                           'limitations':['No automatic proof of semantic accuracy, formula fidelity, table relations, or figure-label completeness.',
                                          'Independent full review is mandatory, including continuation mapping.']})
    src.close();translated.close();cn.close();bi.close();return layout
