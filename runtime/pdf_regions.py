"""Remove only off-clip asset metadata in a private cropped page copy.

PDF Form clipping can hide a figure while extraction still reports its full
bounding box elsewhere. That made plain prose look like an image or a table.
The visible crop must remain pixel-identical; otherwise keep the original copy.
"""
import pymupdf as fitz


def visible_images(page):
    result=[]
    for image in page.get_image_info(xrefs=True):
        xref=image.get('xref',0)
        if xref and image['width']==1 and image['height']==1:
            # delete_image intentionally leaves a 1px transparent placeholder.
            # Verify its actual alpha mask, never trust a PDF-supplied role tag.
            mask=page.parent.extract_image(xref).get('smask',0)
            if mask and not any(fitz.Pixmap(page.parent,mask).samples):continue
        result.append(image)
    return result


def clean_private_crop(document,rect):
    page=document[0];rect=fitz.Rect(rect);snapshot=document.tobytes()
    before=page.get_pixmap(matrix=fitz.Matrix(2,2),clip=rect,alpha=False)
    uses={}
    for image in page.get_image_info(xrefs=True):
        uses.setdefault(image.get('xref',0),[]).append(fitz.Rect(image['bbox']))
    removed=0
    for xref,boxes in uses.items():
        if xref and not any(box.intersects(rect) for box in boxes):
            page.delete_image(xref);removed+=1
    bounds=page.rect
    for outside in (fitz.Rect(0,0,bounds.width,rect.y0),fitz.Rect(0,rect.y1,bounds.width,bounds.height),
                    fitz.Rect(0,rect.y0,rect.x0,rect.y1),fitz.Rect(rect.x1,rect.y0,bounds.width,rect.y1)):
        if not outside.is_empty:page.add_redact_annot(outside,fill=False,cross_out=False)
    # Graphics=1 removes only fully covered paths. Text=1 keeps all text here;
    # its separate verified region selection already occurred before this step.
    page.apply_redactions(images=0,graphics=1,text=1)
    after=page.get_pixmap(matrix=fitz.Matrix(2,2),clip=rect,alpha=False)
    equal=(before.width,before.height,before.samples)==(after.width,after.height,after.samples)
    if not equal:
        document.close();document=fitz.open(stream=snapshot,filetype='pdf')
    return document,{'visibleCropPixelsEqual':equal,'replacedOffClipImages':removed if equal else 0,
                     'action':'pruned_invisible_assets' if equal else 'kept_original_private_copy'}
