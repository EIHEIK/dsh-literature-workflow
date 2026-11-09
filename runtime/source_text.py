"""Native paragraph order and narrow typographic quote normalization.

This does not reconstruct missing mathematical signs or claim OCR accuracy.
The original page images remain authoritative for symbol/number fidelity.
"""
import re
import unicodedata
import pymupdf as fitz
from vector_reflow import reading_order

EXTRACTOR_VERSION='native-block-order-v2'
LIGATURES=str.maketrans({'\ufb00':'ff','\ufb01':'fi','\ufb02':'fl','\ufb03':'ffi',
                        '\ufb04':'ffl','\ufb05':'st','\ufb06':'st'})


def normalize_evidence(text):
    # Unambiguous Latin printing ligatures only. Do not normalize mathematical
    # characters, signs, exponents or numeric values by guessing their meaning.
    text=text.translate(LIGATURES)
    # Some PDF CJK fonts expose canonical compatibility ideographs (e.g.
    # U+F97E for 量). Normalize only these Chinese glyph equivalents, never
    # general NFKC, mathematical alphanumerics, superscripts or minus signs.
    text=''.join(unicodedata.normalize('NFC',c) if 0xf900<=ord(c)<=0xfaff or 0x2f800<=ord(c)<=0x2fa1f else c for c in text)
    return re.sub(r'\s+',' ',text).strip()


def ordered_text(page):
    items=[]
    for block in page.get_text('dict')['blocks']:
        if block['type']!=0:continue
        text='\n'.join(''.join(s['text'] for s in line['spans']) for line in block['lines']).strip()
        if text:items.append((fitz.Rect(block['bbox']),text))
    return '\n\n'.join(item[1] for item in reading_order(items,page.rect.width))
