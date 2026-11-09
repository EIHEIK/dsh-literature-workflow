"""Synthetic layout demo; never invokes a translation API or accesses Zotero."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
import pymupdf as fitz
from pdf_ops import compose
root=Path(sys.argv[1]).resolve();root.mkdir(parents=True,exist_ok=True)
src=fitz.open();p=src.new_page(width=420,height=595)
p.insert_text((30,45),'Synthetic aerodynamic paper',fontsize=18)
p.insert_text((30,90),'Upper(j) = max(x1, x2)     (8)',fontsize=14)
p.insert_text((30,120),'Lower(j) = min(x1, x2)     (9)',fontsize=14)
p.draw_rect(fitz.Rect(50,170,370,350));p.insert_text((70,200),'Wind-tunnel test -> Upper layer',fontsize=12)
p.insert_text((70,320),'Angle of attack / Lift coefficient',fontsize=12)
src.save(root/'source.pdf');src.close()
cn=fitz.open();p=cn.new_page(width=420,height=595)
p.insert_text((30,45),'合成气动论文排版测试',fontname='china-s',fontsize=18)
p.insert_text((30,90),'Upper(j) = max(x1, x2)     (8)',fontsize=14)
p.insert_text((30,120),'Lower(j) = min(x1, x2)     (9)',fontsize=14)
p.insert_text((30,180),'中文正文保持公式、数据和限定条件。',fontname='china-s',fontsize=14)
cn.save(root/'mono.pdf');cn.close()
layout=compose(str(root/'source.pdf'),str(root/'mono.pdf'),[1],[{'sourcePage':1,'title':'图 1 中文标签对照','text':'Wind-tunnel test：风洞试验\nUpper layer：上层\nAngle of attack：攻角\nLift coefficient：升力系数'}],root)
print(root/'preview'/'bi-0002.png')
