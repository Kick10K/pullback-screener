from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

src = Path("outputs/pullback_20260830/qa_workbooks")
files = sorted(src.glob("*.png"))
font = ImageFont.truetype("/System/Library/Fonts/AppleSDGothicNeo.ttc", 16)
tw, th, label_h, cols = 360, 250, 36, 4
rows = (len(files) + cols - 1) // cols
out = Image.new("RGB", (cols * tw, rows * (th + label_h)), "white")
d = ImageDraw.Draw(out)
for i, p in enumerate(files):
    im = Image.open(p).convert("RGB")
    im.thumbnail((tw - 12, th - 12))
    x, y = (i % cols) * tw, (i // cols) * (th + label_h)
    out.paste(im, (x + 6, y + 6))
    d.text((x + 6, y + th + 4), p.stem[:42], font=font, fill="#17324D")
out.save(src / "_contact_sheet.png")
