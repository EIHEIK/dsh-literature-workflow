"""Offline PDF text extraction entrypoint for a restricted single-file worker."""
from pathlib import Path
import json
import pymupdf as fitz
from core import atomic,digest
from source_text import ordered_text,EXTRACTOR_VERSION


def scan(source):
    fingerprint=digest(source);pages=[];remaining=8_000_000
    with fitz.open(source) as doc:
        if not doc.is_pdf or doc.needs_pass or not 1<=len(doc)<=2000:raise ValueError('Unsupported PDF')
        for number,page in enumerate(doc,1):
            text=ordered_text(page);selected=text[:min(30000,remaining)];remaining-=len(selected)
            pages.append({'sourcePage':number,'text':selected,'originalTextLength':len(text),'truncated':len(selected)<len(text)})
    if digest(source)!=fingerprint:raise ValueError('PDF changed during scan')
    return {'schema':1,'extractorVersion':EXTRACTOR_VERSION,'sourceSha256':fingerprint,'pages':pages,
            'sourceInfo':{'pages':len(pages),'text_lengths':[p['originalTextLength'] for p in pages]},
            'coverage':'partial_text' if any(p['truncated'] for p in pages) else 'extracted_text',
            'limitations':['Text extraction does not certify reading order, formulas, figures or OCR completeness.']}


if __name__=='__main__':
    result=scan('/input/source.pdf');atomic('/output/scan.json',result)
