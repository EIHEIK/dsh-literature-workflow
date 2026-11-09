"""Restore closed display-formula regions from immutable original PDF vectors.

The local pinned layout model proposes regions, never content. Prose overlap,
uncertain clipping or changed page geometry prevents replacement. Copying a
formula is verified by actual source/output pixels; this grants no acceptance.
"""
import re
from pathlib import Path

import pymupdf as fitz
from pdf_regions import clean_private_crop


def prose(text):
    words=[w for w in re.findall(r'[A-Za-z]{3,}',text) if not w.isupper()]
    return bool(re.search('[\u3400-\u9fff]',text)) or len(words)>=4


def detect(document,pages=None):
    import numpy as np
    import onnxruntime
    from babeldoc.docvision.doclayout import OnnxModel
    model=OnnxModel.from_pretrained();result={}
    # The worker is limited to two CPUs. ONNX's host-sized default thread pool
    # otherwise oversubscribes this cgroup and makes every page much slower.
    options=onnxruntime.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
    model.model=onnxruntime.InferenceSession(model.model_path,sess_options=options,providers=['CPUExecutionProvider'])
    for n,page in enumerate(document,1):
        if pages is not None and n not in pages:continue
        pix=page.get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
        image=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3)
        layout=model.predict(image[:,:,::-1])[0]
        result[n]=[{'kind':layout.names[int(b.cls)],'confidence':float(b.conf),
                    'bbox':[float(v)/1.5 for v in b.xyxy]} for b in layout.boxes]
    return result


def lines(page):
    return [(fitz.Rect(line['bbox']),''.join(s['text'] for s in line['spans']))
            for block in page.get_text('dict')['blocks'] if block['type']==0 for line in block['lines']]


def clean_border(page,rect):
    pix=page.get_pixmap(matrix=fitz.Matrix(2,2),clip=rect,colorspace=fitz.csGRAY,alpha=False)
    data=pix.samples;w=pix.width;h=pix.height
    border=list(data[:w])+list(data[(h-1)*w:])+[data[y*w] for y in range(h)]+[data[y*w+w-1] for y in range(h)]
    return not any(v<180 for v in border)


def embed_original_bitmap(source,target,rect):
    """Exact source pixels when malformed font metrics make vector clipping unsafe."""
    pix=source.get_pixmap(matrix=fitz.Matrix(600/72,600/72),clip=rect,alpha=False)
    xref=target.insert_image(rect,pixmap=pix,keep_proportion=False,overlay=True)
    stored=fitz.Pixmap(target.parent,xref)
    if (pix.width,pix.height,pix.samples)!=(stored.width,stored.height,stored.samples):
        raise ValueError('Original formula bitmap bytes changed')
    return {'method':'lossless_original_region_no_ocr','dpi':600,'sourcePixelsEmbeddedExactly':True}


def closed_region(page, proposal, detections):
    seed=fitz.Rect(proposal['bbox']);rect=fitz.Rect(seed);caption=False
    for item in detections:
        box=fitz.Rect(item['bbox'])
        if (item['kind']=='formula_caption' and item['confidence']>=.5 and
            abs((box.y0+box.y1-seed.y0-seed.y1)/2)<=max(14,seed.height/2) and
            box.x0>=seed.x0 and box.x1-seed.x0<=page.rect.width*.55):
            rect|=box;caption=True
    if proposal['confidence']<.75 and not caption:return None,'low_confidence_without_number'
    # Include every non-prose line touched by the proposed formula. A sentence
    # inside it means the detector did not separate the content safely.
    for box,text in lines(page):
        if box.intersects(rect):
            if prose(text):return None,'source_prose_overlap'
            rect|=box
    above=0;below=page.rect.height
    for box,text in lines(page):
        if not prose(text) or min(box.x1,rect.x1)<=max(box.x0,rect.x0):continue
        if box.y1<=rect.y0:above=max(above,box.y1)
        elif box.y0>=rect.y1:below=min(below,box.y0)
    rect=fitz.Rect(max(0,rect.x0-4),max(above+.7,rect.y0-3),
                   min(page.rect.width,rect.x1+4),min(below-.7,rect.y1+3))
    if not rect.contains(seed):return None,'formula_touches_prose_boundary'
    # A visible border of clean paper is required, including at tiny delimiters.
    if not clean_border(page,rect):return None,'ink_at_formula_boundary'
    return rect,None


def preserve(source,mono,mapping,destination,detections=None):
    original=fitz.open(source);translated=fitz.open(mono);evidence=[];uncertain=[]
    try:
        if len(translated)!=len(mapping):raise ValueError('Formula page mapping differs')
        detections=detect(original,set(mapping)) if detections is None else detections
        seen=set()
        for index,sp in enumerate(mapping):
            if sp in seen:continue # Later plain-text continuation pages contain no original formula slots.
            seen.add(sp)
            src=original[sp-1];target=translated[index]
            if src.rect.width!=target.rect.width or not src.rect.height<=target.rect.height<=src.rect.height+100:
                uncertain.append({'sourcePage':sp,'reason':'page_geometry_changed'});continue
            accepted=[]
            for item in detections.get(sp,[]):
                if item['kind']!='isolate_formula':continue
                rect,reason=closed_region(src,item,detections[sp])
                if reason:uncertain.append({'sourcePage':sp,'bbox':item['bbox'],'reason':reason});continue
                # Shrinking translation can place a prose line inside otherwise
                # blank padding around the source formula. Trim only that blank
                # padding; never trim the actual original formula or number.
                core=fitz.Rect(item['bbox'])
                for box,text in lines(src):
                    if box.intersects(rect) and not prose(text):core|=box
                for box,text in lines(target):
                    if not prose(text) or not box.intersects(rect):continue
                    if box.y0>=core.y1:rect.y1=min(rect.y1,box.y0-.5)
                    elif box.y1<=core.y0:rect.y0=max(rect.y0,box.y1+.5)
                if not rect.contains(core) or not clean_border(src,rect):
                    uncertain.append({'sourcePage':sp,'bbox':list(rect),'reason':'no_clean_formula_margin'});continue
                if any(rect.intersects(prior) for prior in accepted):
                    uncertain.append({'sourcePage':sp,'bbox':list(rect),'reason':'overlapping_formula_regions'});continue
                conflicts=[{'bbox':list(box),'text':text[:120]} for box,text in lines(target)
                           if box.intersects(rect) and (prose(text) or not rect.contains(box))]
                unsafe=bool(conflicts)
                if unsafe:
                    uncertain.append({'sourcePage':sp,'bbox':list(rect),'reason':'translated_prose_or_partial_line_overlap',
                                      'conflicts':conflicts[:4]});continue
                accepted.append(rect)
                target.add_redact_annot(rect,fill=False,cross_out=False)
                target.apply_redactions(images=0,graphics=0,text=0)
                # Cover old formula paths, then place the actual original. The
                # immutable source and other figure/data regions are untouched.
                target.draw_rect(rect,color=None,fill=(1,1,1),overlay=True)
                before_copy=translated.tobytes()
                region=fitz.open();region.insert_pdf(original,from_page=sp-1,to_page=sp-1);rp=region[0];bounds=rp.rect
                for outside in (fitz.Rect(0,0,bounds.width,rect.y0),fitz.Rect(0,rect.y1,bounds.width,bounds.height),
                                fitz.Rect(0,rect.y0,rect.x0,rect.y1),fitz.Rect(rect.x1,rect.y0,bounds.width,rect.y1)):
                    if not outside.is_empty:rp.add_redact_annot(outside,fill=False,cross_out=False)
                rp.apply_redactions(images=0,graphics=0,text=0)
                region,asset_proof=clean_private_crop(region,rect)
                frozen=fitz.open(stream=region.tobytes(),filetype='pdf')
                try:target.show_pdf_page(rect,frozen,0,clip=rect)
                finally:frozen.close();region.close()
                a=src.get_pixmap(matrix=fitz.Matrix(2,2),clip=rect,alpha=False)
                b=target.get_pixmap(matrix=fitz.Matrix(2,2),clip=rect,alpha=False)
                if (a.width,a.height,a.samples)!=(b.width,b.height,b.samples):
                    debug=Path(destination).parent
                    a.save(debug/f'formula-source-{sp:04}.png');b.save(debug/f'formula-restored-{sp:04}.png')
                    # Bad glyph boxes can make outside-text redaction delete a
                    # visible subscript. Discard that vector attempt completely.
                    replacement=fitz.open(stream=before_copy,filetype='pdf');translated.close();translated=replacement;target=translated[index]
                    proof=embed_original_bitmap(src,target,rect)
                    proof['vectorAttemptRejected']='original_region_pixels_differed'
                else:proof={'method':'immutable_original_vectors','pixelsEqual':True}
                evidence.append({'sourcePage':sp,'monoPage':index+1,'bbox':list(rect),'assetClipCleanup':asset_proof,**proof})
        translated.save(destination,garbage=4,deflate=True)
    finally:original.close();translated.close()
    return {'restored':evidence,'uncertain':uncertain,'acceptance':'not_granted'}
