const fs = require("node:fs/promises");
const { SpreadsheetFile, Workbook } = require("@oai/artifact-tool");

const root = "/Users/kyungjunkim/.codex/.chatgpt-projects/g-p-694d58a990dc8191ae5e73b95ae0db1c";

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        field += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += ch;
    }
  }
  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

async function main() {
  const csv = await fs.readFile(`${root}/research_sources.csv`, "utf8");
  const rows = parseCsv(csv);
  const headers = rows[0];
  const academic = rows.slice(1).filter((row) => (row[1] || "").startsWith("학술"));

  const workbook = Workbook.create();
  const sources = workbook.worksheets.add("Academic Sources");
  sources.showGridLines = false;
  sources.getRange("A1:K1").merge();
  sources.getRange("A1").values = [["눌림목 매매 학술 근거 색인"]];
  sources.getRange("A1:K1").format = {
    fill: "#17324D",
    font: { name: "Arial Unicode MS", bold: true, color: "#FFFFFF", size: 16 },
    horizontalAlignment: "center",
    verticalAlignment: "center",
  };
  sources.getRange("A2:K2").merge();
  sources.getRange("A2").values = [[
    "자체·공개 백테스트를 제외하고 학술 논문만 수록했습니다. 구성요소 근거와 눌림 적용 한계를 함께 확인하세요.",
  ]];
  sources.getRange("A2:K2").format = {
    fill: "#EAF1F6",
    font: { name: "Arial Unicode MS", color: "#263746", italic: true },
    wrapText: true,
  };

  const endRow = academic.length + 4;
  sources.getRange(`A4:K${endRow}`).values = [headers, ...academic];
  sources.getRange("A4:K4").format = {
    fill: "#2E74B5",
    font: { name: "Arial Unicode MS", bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D9E2EA" },
  };
  sources.getRange(`A5:K${endRow}`).format = {
    font: { name: "Arial Unicode MS", color: "#263746", size: 9 },
    verticalAlignment: "top",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D9E2EA" },
  };
  [10, 18, 42, 25, 10, 30, 42, 42, 42, 55, 14].forEach((width, column) => {
    sources.getRangeByIndexes(0, column, endRow, 1).format.columnWidth = width;
  });
  sources.getRange("A1:K1").format.rowHeight = 30;
  sources.getRange("A2:K2").format.rowHeight = 34;
  sources.getRange("A4:K4").format.rowHeight = 32;
  sources.getRange(`A5:K${endRow}`).format.rowHeight = 54;
  sources.freezePanes.freezeRows(4);

  const guide = workbook.worksheets.add("Guide");
  guide.showGridLines = false;
  guide.getRange("A1:F1").merge();
  guide.getRange("A1").values = [["학술 근거 읽는 법"]];
  guide.getRange("A1:F1").format = {
    fill: "#17324D",
    font: { name: "Arial Unicode MS", bold: true, color: "#FFFFFF", size: 16 },
    horizontalAlignment: "center",
  };
  const categoryCounts = {};
  academic.forEach((row) => { categoryCounts[row[1]] = (categoryCounts[row[1]] || 0) + 1; });
  const categoryRows = Object.entries(categoryCounts).sort((a, b) => a[0].localeCompare(b[0]));
  const summaryEndRow = categoryRows.length + 4;
  guide.getRange(`A3:B${summaryEndRow}`).values = [
    ["분류", "논문 수"],
    ...categoryRows,
    ["합계", academic.length],
  ];
  guide.getRange("A3:B3").format = {
    fill: "#2E74B5",
    font: { name: "Arial Unicode MS", bold: true, color: "#FFFFFF" },
    borders: { preset: "all", style: "thin", color: "#D9E2EA" },
  };
  guide.getRange(`A4:B${summaryEndRow}`).format = {
    font: { name: "Arial Unicode MS", color: "#263746" },
    borders: { preset: "all", style: "thin", color: "#D9E2EA" },
  };
  guide.getRange("D3:F3").merge();
  guide.getRange("D3").values = [["해석 원칙"]];
  guide.getRange("D3:F3").format = {
    fill: "#2E74B5",
    font: { name: "Arial Unicode MS", bold: true, color: "#FFFFFF" },
  };
  guide.getRange("D4:F7").values = [
    ["1", "구성요소 근거", "모멘텀·신고가·거래량·지지저항을 완전한 눌림목 규칙과 구분"],
    ["2", "직접성", "시장·기간·보유기간·롱숏 여부가 다르면 수익률 직접 적용 금지"],
    ["3", "한국 적용", "한국 결과가 있는 연구는 별도 표시하고 보편 법칙으로 확대 금지"],
    ["4", "원문 확인", "제목·방법·한계와 URL을 함께 확인"],
  ];
  guide.getRange("D4:F7").format = {
    font: { name: "Arial Unicode MS", color: "#263746" },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: "#D9E2EA" },
  };
  [18, 12, 6, 8, 22, 62].forEach((width, column) => {
    guide.getRangeByIndexes(0, column, Math.max(summaryEndRow, 12), 1).format.columnWidth = width;
  });
  guide.getRange("A1:F1").format.rowHeight = 30;
  guide.getRange("D4:F7").format.rowHeight = 42;

  const outputPath = `${root}/outputs/pullback_20260830/Pullback_Academic_Source_Index.xlsx`;
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);
  const preview = await workbook.render({ sheetName: "Guide", autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile("/private/tmp/Pullback_Academic_Source_Index_preview.png", new Uint8Array(await preview.arrayBuffer()));
  const inspected = await workbook.inspect({
    kind: "sheet,region",
    sheetId: "Academic Sources",
    range: `A1:K${endRow}`,
    maxChars: 2000,
    tableMaxRows: 4,
    tableMaxCols: 11,
  });
  console.log(JSON.stringify({ outputPath, academicCount: academic.length, inspect: inspected.ndjson }));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
