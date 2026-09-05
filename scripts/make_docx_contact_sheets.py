import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("outputs/pullback_20260830/qa_docx")
files = sorted(src.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[1]))
font = ImageFont.truetype("/System/Library/Fonts/AppleSDGothicNeo.ttc", 18)
for part in range((len(files)+11)//12):
    group = files[part*12:(part+1)*12]
    tw, th, lh, cols = 330, 430, 30, 4
    out = Image.new("RGB",(cols*tw,3*(th+lh)),"#D7DEE5"); d=ImageDraw.Draw(out)
    for i,p in enumerate(group):
        im=Image.open(p).convert("RGB"); im.thumbnail((tw-12,th-12))
        x=(i%cols)*tw; y=(i//cols)*(th+lh)
        out.paste(im,(x+(tw-im.width)//2,y+6)); d.text((x+8,y+th+2),p.stem,font=font,fill="#17324D")
    out.save(src/f"contact_{part+1}.png")
