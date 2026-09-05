from pathlib import Path
import pypdfium2 as pdfium

pdf_path = Path("outputs/pullback_20260830/qa_docx2/눌림목매매_학습및백테스트_20260830.pdf")
out = Path("outputs/pullback_20260830/qa_pdfium")
out.mkdir(parents=True, exist_ok=True)
pdf = pdfium.PdfDocument(str(pdf_path))
for i in range(len(pdf)):
    bitmap = pdf[i].render(scale=1.6)
    bitmap.to_pil().save(out / f"page-{i+1}.png")
