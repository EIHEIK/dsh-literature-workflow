"""Lossless visual relocation of small translated text, not OCR or rewriting.

Original glyphs, formulas, images and paths are copied as PDF form objects. Only
text in fully captured regions is removed from the old position. Unsupported
regions remain visible and fail normal QA; this function cannot grant acceptance.
"""
import pymupdf as fitz
from pdf_regions import clean_private_crop


def chinese(value):return 0x3400<=value<=0x9fff or 0xf900<=value<=0xfaff


def near(a,b):
    if a.intersects(b):return True
    xo=max(0,min(a.x1,b.x1)-max(a.x0,b.x0));yo=max(0,min(a.y1,b.y1)-max(a.y0,b.y0))
    xgap=max(0,max(a.x0,b.x0)-min(a.x1,b.x1));ygap=max(0,max(a.y0,b.y0)-min(a.y1,b.y1))
    return (ygap<=5 and xo>=.65*min(a.width,b.width)) or (xgap<=5 and yo>=.65*min(a.height,b.height))


def reading_order(items,page_width):
    """Read each column top-to-bottom, separated by full-width bands."""
    middle=page_width/2
    # A short abstract/copyright line may start above the gutter and extend
    # across it without occupying 60% of the sheet. It is still a spanning band,
    # not a right-column paragraph to append after the entire left column.
    wide=[item for item in items if item[0].x0<page_width*.4 and item[0].x1>page_width*.6]
    narrow=[item for item in items if item not in wide]
    def columns(group):
        left=[item for item in group if (item[0].x0+item[0].x1)/2<middle]
        right=[item for item in group if item not in left]
        if not left or not right:return sorted(group,key=lambda item:(item[0].y0,item[0].x0))
        return sorted(left,key=lambda item:(item[0].y0,item[0].x0))+sorted(right,key=lambda item:(item[0].y0,item[0].x0))
    result=[]
    for item in sorted(wide,key=lambda item:item[0].y0):
        before=[entry for entry in narrow if entry[0].y0<item[0].y0]
        result.extend(columns(before));result.append(item)
        narrow=[entry for entry in narrow if entry not in before]
    return result+columns(narrow)


def regions(page):
    chars=[(fitz.Rect(c[3]),float(span['size'])) for span in page.get_texttrace()
           if span.get('type')!=3 and span.get('opacity',1)>=.05
           for c in span['chars'] if chinese(c[0]) and page.rect.intersects(fitz.Rect(c[3]))]
    bad=[rect for rect,size in chars if size<9.95]
    lines=[fitz.Rect(line['bbox']) for block in page.get_text('dict')['blocks'] if block['type']==0 for line in block['lines']]
    selected=[rect for rect in lines if any(rect.intersects(small) for small in bad)]
    # Close over intersecting lines before deletion, so no text line can be cut
    # in half. Adjacent lines in one column are packed into a paragraph region.
    changed=True
    while changed:
        changed=False
        for index,a in enumerate(selected):
            for line in lines:
                if a.intersects(line) and not a.contains(line):a|=line;changed=True
            for other in range(len(selected)-1,index,-1):
                if near(a,selected[other]):a|=selected.pop(other);changed=True
            selected[index]=a
    result=[]
    for rect in selected:
        sizes=[size for box,size in chars if rect.intersects(box)]
        if not sizes or min(sizes)<=0:continue
        scale=max(1,10.75/min(sizes));rect=rect+(-.5,-.5,.5,.5)
        # Clipped content is not reconstructed by guessing. Keep it for review.
        boundary=page.rect
        if not boundary.contains(rect) or rect.width*scale>920 or rect.height*scale>1450:continue
        result.append((rect,scale))
    return reading_order(result,page.rect.width)


def reflow(mono,mapping,destination):
    source=fitz.open(mono);out=fitz.open();new_mapping=[];evidence=[]
    try:
        if len(source)!=len(mapping):raise ValueError('Vector reflow page map differs')
        for index,page in enumerate(source):
            moves=regions(page)
            if not moves:
                out.insert_pdf(source,from_page=index,to_page=index);new_mapping.append(mapping[index]);continue
            working=fitz.open();working.insert_pdf(source,from_page=index,to_page=index);p=working[0]
            # Frozen source is never mutated after it is used for a vector copy.
            original=fitz.open(stream=working.tobytes(),filetype='pdf')
            try:
                for rect,scale in moves:p.add_redact_annot(rect,fill=False,cross_out=False)
                p.apply_redactions(images=0,graphics=0,text=0)
                snapshot=fitz.open(stream=working.tobytes(),filetype='pdf')
                first=out.new_page(width=page.rect.width,height=page.rect.height+30)
                if p.get_contents():first.show_pdf_page(page.rect,snapshot,0)
                first.insert_text((20,page.rect.height+20),'本页部分译文与公式见紧随续页，原始矢量内容保留。',fontname='china-s',fontsize=10.5)
                snapshot.close();new_mapping.append(mapping[index]);continuation=None;cursor=65
                for number,(rect,scale) in enumerate(moves,1):
                    width=rect.width*scale;height=rect.height*scale
                    wide=width>515 or height>700
                    if continuation is None or wide or cursor+height>800:
                        continuation=out.new_page(width=max(595,width+80),height=max(842,height+110))
                        continuation.insert_text((40,35),f'译文矢量续页：源页 {mapping[index]}',fontname='china-s',fontsize=11)
                        cursor=65;new_mapping.append(mapping[index])
                    target=fitz.Rect(40,cursor,40+width,cursor+height)
                    # Form clipping alone leaves off-clip text in extraction /
                    # tracing. Remove that text from a private region copy first,
                    # keeping all paths/images and the captured vector text.
                    region_doc=fitz.open();region_doc.insert_pdf(original,from_page=0,to_page=0)
                    rp=region_doc[0];bounds=rp.rect
                    for outside in (fitz.Rect(0,0,bounds.width,rect.y0),fitz.Rect(0,rect.y1,bounds.width,bounds.height),
                                    fitz.Rect(0,rect.y0,rect.x0,rect.y1),fitz.Rect(rect.x1,rect.y0,bounds.width,rect.y1)):
                        if not outside.is_empty:rp.add_redact_annot(outside,fill=False,cross_out=False)
                    rp.apply_redactions(images=0,graphics=0,text=0)
                    region_doc,asset_proof=clean_private_crop(region_doc,rect)
                    captured=fitz.open(stream=region_doc.tobytes(),filetype='pdf')
                    try:continuation.show_pdf_page(target,captured,0,clip=rect,keep_proportion=True)
                    finally:captured.close();region_doc.close()
                    evidence.append({'sourcePage':mapping[index],'inputPage':index+1,'region':number,
                        'clip':list(rect),'scale':scale,'outputPage':len(out),'method':'original_pdf_vectors_no_ocr',
                        'assetClipCleanup':asset_proof})
                    cursor+=height+16
                    if wide:continuation=None
            finally:original.close();working.close()
        out.save(destination,garbage=4,deflate=True)
    finally:out.close();source.close()
    return new_mapping,evidence
