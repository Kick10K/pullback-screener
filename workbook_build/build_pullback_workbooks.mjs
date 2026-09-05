import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const ROOT = path.resolve("..");
const OUT = path.join(ROOT, "outputs", "pullback_20260830");
const QA = path.join(OUT, "qa_workbooks");
await fs.mkdir(QA, { recursive: true });

const NAVY = "#17324D", BLUE = "#2F6B9A", LIGHT = "#EAF1F6", PALE = "#F6F8FA", WHITE = "#FFFFFF", RED = "#C94C4C", GREEN = "#4E8D61", GRID = "#D9E2EA";

function parseCsv(text) {
  const rows = []; let row = [], cell = "", quote = false;
  for (let i=0; i<text.length; i++) {
    const ch=text[i];
    if (quote) {
      if (ch==='"' && text[i+1]==='"') { cell+='"'; i++; }
      else if (ch==='"') quote=false;
      else cell+=ch;
    } else {
      if (ch==='"') quote=true;
      else if (ch===',') { row.push(cell); cell=""; }
      else if (ch==='\n') { row.push(cell.replace(/\r$/, "")); rows.push(row); row=[]; cell=""; }
      else cell+=ch;
    }
  }
  if (cell.length || row.length) { row.push(cell); rows.push(row); }
  const headers=rows[0];
  return rows.slice(1).filter(r=>r.some(x=>x!=="")).map(r=>Object.fromEntries(headers.map((h,i)=>[h,coerce(r[i]??"")])));
}

function coerce(v) {
  if (v === "") return null;
  if (/^(true|false)$/i.test(v)) return v.toLowerCase()==="true";
  if (/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(v)) return Number(v);
  return v;
}

async function csv(rel) { return parseCsv(await fs.readFile(path.join(ROOT, rel), "utf8")); }
function colLetter(n) { let s=""; while(n>0){ n--; s=String.fromCharCode(65+n%26)+s; n=Math.floor(n/26);} return s; }

function titleBlock(sheet, title, subtitle, width=10) {
  const end=colLetter(width);
  sheet.getRange(`A1:${end}1`).merge(); sheet.getRange("A1").values=[[title]];
  sheet.getRange(`A1:${end}1`).format={fill:NAVY,font:{bold:true,color:WHITE,size:20},rowHeight:34,verticalAlignment:"center"};
  sheet.getRange(`A2:${end}2`).merge(); sheet.getRange("A2").values=[[subtitle]];
  sheet.getRange(`A2:${end}2`).format={fill:LIGHT,font:{color:NAVY,italic:true},wrapText:true,rowHeight:30};
  sheet.showGridLines=false;
}

function matrix(rows, headers) { return rows.map(r=>headers.map(h=>r[h] ?? null)); }

function formatData(sheet, headers, startRow, endRow) {
  const pctNames = new Set(["total_return","cagr","win_rate","avg_win","avg_loss","expectancy","max_drawdown","exposure","gross_expectancy","net_expectancy","gross_return","net_return","risk_pct","depth","prior_advance","atr_pct","rs60","dist52","train_expectancy","test_expectancy","test_win_rate","base_expectancy","double_cost_expectancy","ci95_low","ci95_high"]);
  const dateNames = new Set(["setup_date","signal_date","entry_date","exit_date","train_start","train_end"]);
  headers.forEach((h,i)=>{
    const col=colLetter(i+1); const r=sheet.getRange(`${col}${startRow}:${col}${endRow}`);
    if (pctNames.has(h)) r.format.numberFormat="0.00%;[Red](0.00%);-";
    else if (dateNames.has(h)) r.format.numberFormat="yyyy-mm-dd";
    else if (["profit_factor","avg_r","r_multiple","vol_ratio","gap_fill","sharpe"].includes(h)) r.format.numberFormat="0.00x;[Red](0.00x);-";
    else if (["entry_price","exit_price","initial_stop","peak_high"].includes(h)) r.format.numberFormat="#,##0.00;[Red](#,##0.00);-";
    else if (["trades","holding_days","duration","test_year","rows"].includes(h)) r.format.numberFormat="#,##0";
    if (["title","name","key_result","limitation","definition_or_method","known_info","lesson","outcome","url","rule","selected_rule"].includes(h)) r.format.wrapText=true;
  });
}

function addTable(sheet, rows, headers, start=4, name="DataTable") {
  if (!rows.length) return {end:start};
  const end=start+rows.length; const last=colLetter(headers.length);
  sheet.getRange(`A${start}:${last}${start}`).values=[headers];
  sheet.getRange(`A${start}:${last}${start}`).format={fill:BLUE,font:{bold:true,color:WHITE},wrapText:true,rowHeight:28,borders:{preset:"all",style:"thin",color:GRID}};
  sheet.getRange(`A${start+1}:${last}${end}`).values=matrix(rows,headers);
  sheet.getRange(`A${start+1}:${last}${end}`).format={font:{size:9,color:"#263746"},borders:{preset:"all",style:"thin",color:GRID},verticalAlignment:"top"};
  sheet.tables.add(`A${start}:${last}${end}`,true,name).style="TableStyleMedium2";
  sheet.freezePanes.freezeRows(start); sheet.freezePanes.freezeColumns(Math.min(2,headers.length));
  formatData(sheet,headers,start+1,end);
  headers.forEach((h,i)=>{
    const col=sheet.getRange(`${colLetter(i+1)}:${colLetter(i+1)}`);
    col.format.columnWidth = ["url"].includes(h)?44:(["rule","selected_rule"].includes(h)?40:(["title","key_result","limitation","definition_or_method","known_info","lesson","outcome"].includes(h)?28:(["name","label","category","analysis"].includes(h)?20:13)));
  });
  return {end,last};
}

async function finish(wb, fileName, previews) {
  const errors=await wb.inspect({kind:"match",searchTerm:"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",options:{useRegex:true,maxResults:300},summary:"final formula error scan"});
  console.log(fileName, errors.ndjson);
  for (const p of previews) {
    const img=await wb.render({sheetName:p.sheet,range:p.range,scale:1,format:"png"});
    await fs.writeFile(path.join(QA,`${fileName.replace('.xlsx','')}_${p.sheet}.png`),new Uint8Array(await img.arrayBuffer()));
  }
  const out=await SpreadsheetFile.exportXlsx(wb); await out.save(path.join(OUT,fileName));
}

const sources=await csv("research_sources.csv"), universe=await csv("config/pullback_universe.csv"), status=await csv("analysis_output/data_status.csv");
const baseline=await csv("analysis_output/baseline_metrics.csv"), comp=await csv("analysis_output/strategy_comparison.csv"), bucket=await csv("analysis_output/bucket_analysis.csv"), robust=await csv("analysis_output/robustness.csv"), walk=await csv("analysis_output/walk_forward.csv"), bench=await csv("analysis_output/benchmark_metrics.csv"), ci=await csv("analysis_output/expectancy_ci.csv"), stress=await csv("analysis_output/cost_stress.csv"), nav=await csv("analysis_output/nav_series.csv"), standalone=await csv("analysis_output/standalone_filters.csv"), control=await csv("analysis_output/control_metrics.csv"), logs=await csv("analysis_output/all_trade_logs.csv"), cases=await csv("analysis_output/example_cases.csv");

// 1) Source Index
{
  const wb=Workbook.create();
  const g=wb.worksheets.add("안내"), s=wb.worksheets.add("출처"), u=wb.worksheets.add("표본"), ds=wb.worksheets.add("데이터상태");
  titleBlock(g,"Pullback Source Index","학술·공개 백테스트·정책·데이터 출처를 직접 근거와 간접 근거로 구분",8);
  g.getRange("A4:B12").values=[
    ["목적","눌림목 전략의 각 주장과 자체 백테스트 가정을 감사 가능하게 추적"],
    ["중요 결론","완전한 눌림목 규칙을 직접 검증한 학술 근거는 제한적"],
    ["직접성 원칙","모멘텀·거래량·이동평균 연구는 구성요소 근거이며 전략 전체의 증거가 아님"],
    ["데이터 기준일","2015-01-01~2025-12-31; 2026-08-30 조사"],
    ["표본","현행 유동성 표본 US 40 / KR 40; 생존편향 있음"],
    ["출처 수","=COUNTA('출처'!A5:A200)"],
    ["학술 출처","=COUNTIF('출처'!B5:B200,\"학술*\")"],
    ["공개·관찰·정책·데이터","=COUNTA('출처'!A5:A200)-COUNTIF('출처'!B5:B200,\"학술*\")"],
    ["사용법","URL과 한계를 함께 읽고, 결과 숫자만 떼어 비교하지 않기"],
  ];
  g.getRange("B9:B11").formulas=[["=COUNTA('출처'!A5:A200)"],["=COUNTA('출처'!A5:A22)"],["=B9-B10"]];
  g.getRange("A4:A12").format={fill:LIGHT,font:{bold:true,color:NAVY}}; g.getRange("A4:B12").format.borders={preset:"all",style:"thin",color:GRID}; g.getRange("B4:B12").format.wrapText=true; g.getRange("A:A").format.columnWidth=22; g.getRange("B:B").format.columnWidth=70;
  titleBlock(s,"출처 색인","제목·시장·정의·결과·한계·원문 URL",11); addTable(s,sources,Object.keys(sources[0]),4,"SourceTable");
  titleBlock(u,"백테스트 표본","현재의 유동성 종목 표본이며 point-in-time 지수 구성종목이 아님",5); addTable(u,universe,Object.keys(universe[0]),4,"UniverseTable");
  titleBlock(ds,"데이터 적재 상태","각 시계열의 적재 여부와 관측치 수",4); addTable(ds,status,Object.keys(status[0]),4,"DataStatusTable");
  await finish(wb,"Pullback_Source_Index.xlsx",[{sheet:"안내",range:"A1:H14"},{sheet:"출처",range:"A1:K18"},{sheet:"표본",range:"A1:E18"},{sheet:"데이터상태",range:"A1:D18"}]);
}

// 2) Backtest Result
{
  const wb=Workbook.create(); const s=wb.worksheets.add("요약"); titleBlock(s,"Pullback Backtest Result","OHLCV-only, 다음 날 시가 체결, 비용 차감, IS/OOS 분리",12);
  s.getRange("A4:H4").values=[["시장","기간","전략 CAGR","지수 CAGR","CAGR 차이","기대값","최대낙폭","거래 수"]];
  const keys=[baseline.find(x=>x.market==="US"&&x.period==="OOS"),baseline.find(x=>x.market==="KR"&&x.period==="OOS")];
  s.getRange("A5:D6").values=keys.map((x,i)=>[x.market,"OOS",x.cagr,bench.find(b=>b.market===x.market&&b.period==="OOS").cagr]);
  s.getRange("E5:E6").formulas=[["=C5-D5"],["=C6-D6"]]; s.getRange("F5:H6").values=keys.map(x=>[x.expectancy,x.max_drawdown,x.trades]);
  s.getRange("A4:H4").format={fill:BLUE,font:{bold:true,color:WHITE}}; s.getRange("A4:H6").format.borders={preset:"all",style:"thin",color:GRID}; s.getRange("C5:G6").format.numberFormat="0.00%;[Red](0.00%);-";
  s.getRange("A9:H14").values=[["판단 항목","미국 OOS","한국 OOS",null,null,null,null,null],["거래당 기대값",keys[0].expectancy,keys[1].expectancy,null,null,null,null,null],["95% CI",`${ci.find(x=>x.market==='US'&&x.family==='Baseline').ci95_low.toFixed(4)} ~ ${ci.find(x=>x.market==='US'&&x.family==='Baseline').ci95_high.toFixed(4)}`,`${ci.find(x=>x.market==='KR'&&x.family==='Baseline').ci95_low.toFixed(4)} ~ ${ci.find(x=>x.market==='KR'&&x.family==='Baseline').ci95_high.toFixed(4)}`,null,null,null,null,null],["비용 2배 기대값",stress.find(x=>x.market==='US'&&x.family==='Baseline').double_cost_expectancy,stress.find(x=>x.market==='KR'&&x.family==='Baseline').double_cost_expectancy,null,null,null,null,null],["지수 대비",keys[0].cagr-bench.find(b=>b.market==='US'&&b.period==='OOS').cagr,keys[1].cagr-bench.find(b=>b.market==='KR'&&b.period==='OOS').cagr,null,null,null,null,null],["해석","양의 거래 기대값, 지수 미달","비용 후 우위 확인 불가",null,null,null,null,null]];
  s.getRange("A9:C14").format.borders={preset:"all",style:"thin",color:GRID}; s.getRange("A9:C9").format={fill:NAVY,font:{bold:true,color:WHITE}}; s.getRange("B10:C14").format.wrapText=true; s.getRange("B10:C10").format.numberFormat="0.00%"; s.getRange("B12:C13").format.numberFormat="0.00%";
  s.getRange("J4:L6").values=[["시장","전략 CAGR","지수 CAGR"],...keys.map(x=>[x.market,x.cagr,bench.find(b=>b.market===x.market&&b.period==="OOS").cagr])];
  s.getRange("J4:L6").format={font:{color:WHITE},numberFormat:"0.00%"};
  const ch=s.charts.add("bar",s.getRange("J4:L6")); ch.title="OOS CAGR: 전략 vs 지수"; ch.hasLegend=true; ch.yAxis={numberFormatCode:"0.0%"}; ch.setPosition("J8","Q24");
  s.getRange("A:A").format.columnWidth=22; s.getRange("B:H").format.columnWidth=16;
  const defs=[
    ["CAGR","연복리수익률"],["Win Rate","전체 거래 중 이익 거래 비율"],["Profit Factor","총이익/총손실 절댓값"],["Expectancy","거래 1회당 평균 순수익"],["Maximum Drawdown","고점 대비 최대 자산 하락폭"],["Sharpe","일별 수익률 평균/변동성의 연율화"],["Exposure","평균 투자 비중"],["R","순수익률/초기 손절폭"]
  ];
  const d=wb.worksheets.add("지표정의"); titleBlock(d,"평가 지표 정의","승률보다 기대값·낙폭·노출도를 함께 평가",4); addTable(d,defs.map(x=>({metric:x[0],meaning:x[1]})),["metric","meaning"],4,"MetricTable");
  const tables=[
    ["기본성과",baseline,"BaselineTable"],["벤치마크",bench,"BenchmarkTable"],["증분필터",comp.filter(x=>x.label.startsWith("Filter")),"IncrementalTable"],["버킷분석",bucket,"BucketTable"],["견고성",robust,"RobustnessTable"],["워크포워드",walk,"WalkForwardTable"],["신뢰구간비용",[...ci.map(x=>({...x,type:"CI"})),...stress.map(x=>({...x,type:"CostStress"}))],"ConfidenceTable"],["NAV",nav,"NavTable"]
  ];
  const previews=[{sheet:"요약",range:"A1:Q24"},{sheet:"지표정의",range:"A1:D14"}];
  for (const [name,rows,tname] of tables){const sh=wb.worksheets.add(name); titleBlock(sh,name,"자체 백테스트 산출표",Math.min(15,Object.keys(rows[0]).length)); addTable(sh,rows,Object.keys(rows[0]),4,tname); previews.push({sheet:name,range:`A1:${colLetter(Math.min(15,Object.keys(rows[0]).length))}18`});}
  await finish(wb,"Pullback_Backtest_Result.xlsx",previews);
}

// 3) Trade Log
{
  const wb=Workbook.create(); const g=wb.worksheets.add("안내"); titleBlock(g,"Pullback Trade Log","선택된 포트폴리오 거래 로그; 동일 종목 중첩 및 최대 10종목 제약 반영",7);
  g.getRange("A4:B10").values=[["필드","설명"],["setup_date","가격·추세·조정 조건이 충족된 날"],["signal_date","Trigger가 종가 기준 확정된 날"],["entry_date","다음 거래일 시가 체결"],["initial_stop","진입 전 확정한 구조적 손절가"],["net_return","시장별 비용 가정 차감 후 수익률"],["r_multiple","순수익률/초기 위험폭"]]; g.getRange("A4:B4").format={fill:BLUE,font:{bold:true,color:WHITE}};g.getRange("A4:B10").format.borders={preset:"all",style:"thin",color:GRID};g.getRange("A:A").format.columnWidth=24;g.getRange("B:B").format.columnWidth=70;
  const baseLogs=logs.filter(x=>x.test_family==="Baseline");
  const advLogs=logs.filter(x=>x.test_family==="Filter"&&String(x.rule).includes("volume+market+rs")&&!String(x.rule).includes("volatility"));
  const previews=[{sheet:"안내",range:"A1:G12"}];
  for(const [name,rows,tname] of [["기본전략",baseLogs,"BaseTrades"],["필터전략",advLogs,"AdvancedTrades"],["사례",cases,"ExampleCases"]]){
    const sh=wb.worksheets.add(name); titleBlock(sh,name,"재현 가능한 거래·사례 기록",Math.min(15,Object.keys(rows[0]).length));
    const heads=Object.keys(rows[0]); const info=addTable(sh,rows,heads,4,tname);
    if(name!=="사례"){
      const c1=colLetter(heads.length+1),c2=colLetter(heads.length+2); sh.getRange(`${c1}4:${c2}4`).values=[["R_Check","Win_Flag"]]; sh.getRange(`${c1}4:${c2}4`).format={fill:NAVY,font:{bold:true,color:WHITE}};
      const net=colLetter(heads.indexOf("net_return")+1),risk=colLetter(heads.indexOf("risk_pct")+1);
      sh.getRange(`${c1}5`).formulas=[[`=IFERROR(${net}5/${risk}5,0)`]]; sh.getRange(`${c1}5:${c1}${info.end}`).fillDown();
      sh.getRange(`${c2}5`).formulas=[[`=IF(${net}5>0,1,0)`]]; sh.getRange(`${c2}5:${c2}${info.end}`).fillDown();
      sh.getRange(`${c1}5:${c1}${info.end}`).format.numberFormat="0.00x";
    }
    previews.push({sheet:name,range:`A1:${colLetter(Math.min(15,heads.length))}18`});
  }
  await finish(wb,"Pullback_Trade_Log.xlsx",previews);
}

// 4) Strategy Comparison
{
  const wb=Workbook.create(); const s=wb.worksheets.add("요약"); titleBlock(s,"Pullback Strategy Comparison","진입·손절·청산·필터를 한 번에 하나씩 비교",12);
  const oos=comp.filter(x=>x.period==="OOS");
  const best=(market,prefix)=>oos.filter(x=>x.market===market&&x.label.startsWith(prefix)).sort((a,b)=>(b.expectancy??-99)-(a.expectancy??-99))[0];
  const rows=[]; for(const m of ["US","KR"]) for(const p of ["Entry","Stop","Exit","Filter"]) {const x=best(m,p);rows.push({market:m,family:p,best_rule:x.label,trades:x.trades,expectancy:x.expectancy,profit_factor:x.profit_factor,cagr:x.cagr,max_drawdown:x.max_drawdown});}
  addTable(s,rows,Object.keys(rows[0]),4,"BestComparison");
  s.getRange("J4:L12").values=[["시장/영역","기대값","최대낙폭"],...rows.map(x=>[`${x.market}-${x.family}`,x.expectancy,x.max_drawdown])];
  s.getRange("J4:L12").format={font:{color:WHITE},numberFormat:"0.00%"};
  const ch=s.charts.add("bar",s.getRange("J4:L12")); ch.title="OOS 최선 조합: 기대값과 낙폭"; ch.hasLegend=true; ch.yAxis={numberFormatCode:"0.0%"}; ch.setPosition("J14","Q31");
  const families=[["진입",oos.filter(x=>x.label.startsWith("Entry")),"EntryTable"],["손절",oos.filter(x=>x.label.startsWith("Stop")),"StopTable"],["청산",oos.filter(x=>x.label.startsWith("Exit")),"ExitTable"],["증분필터",oos.filter(x=>x.label.startsWith("Filter")),"FilterTable"],["단독필터",standalone.filter(x=>x.period==="OOS"),"StandaloneTable"],["대조군",control,"ControlTable"],["견고성",robust,"RobustTable"]];
  const previews=[{sheet:"요약",range:"A1:Q31"}];
  for(const [name,data,tname] of families){const sh=wb.worksheets.add(name); titleBlock(sh,name,"2022-2025 OOS 비교",Math.min(15,Object.keys(data[0]).length));addTable(sh,data,Object.keys(data[0]),4,tname);previews.push({sheet:name,range:`A1:${colLetter(Math.min(15,Object.keys(data[0]).length))}18`});}
  await finish(wb,"Pullback_Strategy_Comparison.xlsx",previews);
}

console.log("Workbook package completed", OUT);
