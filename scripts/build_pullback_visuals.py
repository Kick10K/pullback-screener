#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from pullback_backtest import Rule, add_setups, indicators, load_yahoo, safe_symbol


OUT = Path("analysis_output/figures")
OUT.mkdir(parents=True, exist_ok=True)
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
NAVY, BLUE, TEAL, ORANGE, RED, GREEN = "#17324D", "#2F6B9A", "#3B9C9C", "#E69F47", "#C94C4C", "#4E8D61"
LIGHT, GRID, TEXT, WHITE = "#F4F7FA", "#D9E2EA", "#263746", "#FFFFFF"


def font(size=24, bold=False):
    return ImageFont.truetype(FONT_PATH, size=size, index=3 if bold else 0)


def canvas(w=1400, h=820, title="", subtitle=""):
    im = Image.new("RGB", (w, h), WHITE); d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, 92), fill=NAVY)
    d.text((44, 20), title, font=font(32, True), fill=WHITE)
    if subtitle: d.text((46, 60), subtitle, font=font(16), fill="#CFE0EE")
    return im, d


def save_bar(name, title, labels, series, ylabel="기대값", percent=True, subtitle="2022-2025 OOS"):
    im, d = canvas(title=title, subtitle=subtitle); w, h = im.size
    x0, y0, x1, y1 = 150, 150, w-80, h-120
    vals = [v for _, arr, _ in series for v in arr if pd.notna(v)]
    lo, hi = min(vals+[0]), max(vals+[0]); pad=(hi-lo)*.15 or .01; lo-=pad; hi+=pad
    def yy(v): return y1-(v-lo)/(hi-lo)*(y1-y0)
    for k in range(6):
        v=lo+(hi-lo)*k/5; y=yy(v); d.line((x0,y,x1,y),fill=GRID,width=1)
        txt=f"{v*100:.1f}%" if percent else f"{v:.2f}"; d.text((35,y-10),txt,font=font(16),fill=TEXT)
    d.line((x0,yy(0),x1,yy(0)),fill=NAVY,width=2)
    n=len(labels); group=(x1-x0)/max(n,1); bw=group/(len(series)+1)
    for si,(sname,arr,color) in enumerate(series):
        for i,v in enumerate(arr):
            if pd.isna(v): continue
            cx=x0+i*group+group*.16+si*bw; y=yy(v); z=yy(0)
            d.rectangle((cx,min(y,z),cx+bw*.82,max(y,z)),fill=color)
        lx=x0+si*190; d.rectangle((lx,y1+72,lx+26,y1+94),fill=color); d.text((lx+34,y1+69),sname,font=font(17),fill=TEXT)
    for i,lab in enumerate(labels):
        bb=d.textbbox((0,0),str(lab),font=font(15)); tw=bb[2]-bb[0]
        d.text((x0+i*group+group/2-tw/2,y1+18),str(lab),font=font(15),fill=TEXT)
    im.save(OUT/name)


def heatmap(name, title, df):
    im,d=canvas(title=title,subtitle="셀 값: 거래비용 차감 후 거래당 기대수익"); x0,y0=250,180; cw,ch=180,110
    vals=df.expectancy.dropna(); lo,hi=vals.min(),vals.max()
    mas=sorted(df.support_ma.unique()); depths=[f"{a:.0%}-{b:.0%}" for a,b in df[["depth_min","depth_max"]].drop_duplicates().itertuples(index=False)]
    for j,ma in enumerate(mas): d.text((x0+j*cw+50,y0-48),f"{ma}일선",font=font(21,True),fill=TEXT)
    for i,dep in enumerate(depths):
        d.text((65,y0+i*ch+35),dep,font=font(20),fill=TEXT)
        a,b=[float(x.strip('%'))/100 for x in dep.split('-')]
        for j,ma in enumerate(mas):
            z=df[(df.depth_min.round(4)==round(a,4))&(df.depth_max.round(4)==round(b,4))&(df.support_ma==ma)]
            v=float(z.expectancy.iloc[0]) if len(z) else np.nan
            t=0.5 if hi==lo else (v-lo)/(hi-lo); color=(int(205-100*t),int(230-70*abs(t-.5)),int(245-100*(1-t)))
            d.rectangle((x0+j*cw,y0+i*ch,x0+(j+1)*cw-10,y0+(i+1)*ch-10),fill=color)
            d.text((x0+j*cw+45,y0+i*ch+35),f"{v:.2%}",font=font(23,True),fill=NAVY)
    im.save(OUT/name)


def concept_diagram():
    im,d=canvas(title="눌림목의 객관적 구조",subtitle="Prior Trend → Pullback → Trigger → Risk 관리")
    pts=[(120,650),(280,490),(420,270),(560,360),(700,500),(830,430),(980,250),(1160,170),(1300,220)]
    d.line(pts,fill=BLUE,width=7,joint="curve")
    peak=pts[2]; low=pts[4]; trig=pts[6]
    for p,c in [(peak,ORANGE),(low,RED),(trig,GREEN)]: d.ellipse((p[0]-12,p[1]-12,p[0]+12,p[1]+12),fill=c)
    d.text((120,685),"사전 상승추세\n20일 상승 ≥15%",font=font(21,True),fill=TEXT)
    d.text((470,200),"최근 고점",font=font(20,True),fill=ORANGE)
    d.text((620,545),"2~10일 조정\n깊이 5~15%",font=font(20,True),fill=RED)
    d.text((920,300),"전일 고가 돌파 확인\n다음 날 시가 진입",font=font(20,True),fill=GREEN)
    d.line((700,500,700,650),fill=RED,width=3); d.text((625,662),"구조적 손절",font=font(19),fill=RED)
    im.save(OUT/"01_pullback_structure.png")


def type_diagram():
    im,d=canvas(title="눌림목 8개 유형: 모양은 비슷해도 검증 질문은 다르다",subtitle="도식은 개념 예시이며 통계적 우위를 의미하지 않음")
    titles=["A 이동평균","B 돌파 후 되돌림","C 얕은 조정","D 거래량 감소","E 변동성 수축","F 박스 돌파","G 신고가 부근","H 급등 후 깊은 조정"]
    patterns=[[5,4,3,4,5,6],[5,4,2,5,4,6],[6,5.7,5.4,6.2],[6,5,4.7,5.2,6],[6,4.5,5.3,4.9,5.1,6],[4,4.2,4.1,6,5,6.5],[4,5.5,6.2,5.8,6.5],[3,6.5,4,2.5,3.2]]
    for i,(t,pat) in enumerate(zip(titles,patterns)):
        col,row=i%4,i//4; x0=55+col*335; y0=150+row*300
        d.rounded_rectangle((x0,y0,x0+300,y0+250),radius=16,fill=LIGHT,outline=GRID,width=2)
        d.text((x0+18,y0+15),t,font=font(19,True),fill=NAVY)
        p=[]
        for k,v in enumerate(pat): p.append((x0+25+k*245/(len(pat)-1),y0+215-v*23))
        d.line(p,fill=BLUE,width=5,joint="curve")
    im.save(OUT/"02_pullback_types.png")


def line_panel(name,title,cases,data):
    im,d=canvas(w=1600,h=1250,title=title,subtitle="수정 OHLCV; 신호 시점까지의 정보만 표시")
    for i,case in enumerate(cases):
        col,row=i%2,i//2; x0=70+col*760; y0=135+row*340; w=680; h=280
        sym=case["symbol"]; x=data[sym]; center=pd.Timestamp(case["setup_date"]); z=x.loc[center-pd.Timedelta(days=60):center+pd.Timedelta(days=55)].copy()
        if len(z)<5: continue
        vals=z.close.to_numpy(); lo,hi=np.nanmin(vals),np.nanmax(vals); pad=(hi-lo)*.1 or 1; lo-=pad;hi+=pad
        def xx(k): return x0+k*(w-80)/(len(z)-1)
        def yy(v): return y0+h-40-(v-lo)/(hi-lo)*(h-80)
        d.rounded_rectangle((x0,y0,x0+w,y0+h),radius=12,fill=WHITE,outline=GRID,width=2)
        pts=[(xx(k),yy(v)) for k,v in enumerate(vals)]; d.line(pts,fill=BLUE,width=4)
        if "ma20" in z:
            q=[(xx(k),yy(v)) for k,v in enumerate(z.ma20) if pd.notna(v)]; d.line(q,fill=ORANGE,width=2)
        sd=center; k=int(np.argmin(np.abs((z.index-sd).days))); d.line((xx(k),y0+35,xx(k),y0+h-35),fill=RED,width=2)
        label=f"{case['market']} {case['name']} | Setup {sd.date()}"
        if pd.notna(case.get("net_return",np.nan)): label+=f" | {case['net_return']:.1%}"
        d.text((x0+18,y0+10),label,font=font(18,True),fill=TEXT)
    im.save(OUT/name)


def build_examples(data, meta):
    logs=pd.read_csv("analysis_output/all_trade_logs.csv",parse_dates=["setup_date","signal_date","entry_date","exit_date"])
    b=logs[(logs.test_family=="Baseline")&(logs.entry_date>=pd.Timestamp("2022-01-01"))].drop_duplicates(["market","symbol","entry_date"])
    wins=pd.concat([b[b.market=="US"].nlargest(3,"net_return"),b[b.market=="KR"].nlargest(2,"net_return")]).head(5)
    fails=pd.concat([b[b.market=="US"].nsmallest(3,"net_return"),b[b.market=="KR"].nsmallest(2,"net_return")]).head(5)
    rows=[]
    for ctype,g in [("성공",wins),("실패",fails)]:
        for r in g.itertuples(index=False):
            rows.append({"case_type":ctype,"market":r.market,"symbol":r.symbol,"name":r.name,"setup_date":r.setup_date,
                "trigger_date":r.signal_date,"entry_price":r.entry_price,"stop":r.initial_stop,"exit_date":r.exit_date,
                "net_return":r.net_return,"known_info":f"직전 상승 {r.prior_advance:.1%}; 조정 {r.depth:.1%}/{int(r.duration)}일; 거래량비율 {r.vol_ratio:.2f}",
                "setup":"가격·추세 조건 충족","trigger":"전일 고가 돌파 후 다음 날 시가","outcome":f"{r.exit_reason}; 순수익 {r.net_return:.1%}",
                "lesson":"손절과 시간청산이 손익분포를 결정" if ctype=="실패" else "낮은 승률도 큰 평균이익으로 보완 가능"})
    ambiguous=[]
    mm=meta.set_index("symbol")
    for sym,x0 in data.items():
        if len(ambiguous)>=3: break
        x=add_setups(x0,Rule())
        for j in range(max(253,len(x)-1000),len(x)-4):
            if x.iloc[j].setup and not any(x.iloc[k].close>x.iloc[k-1].high for k in range(j+1,j+4)):
                r=x.iloc[j]
                if (not bool(r.market_up)) or (pd.notna(r.vol_ratio) and r.vol_ratio>1):
                    market=mm.loc[sym,"market"]
                    ambiguous.append({"case_type":"진입 보류","market":market,"symbol":sym,"name":mm.loc[sym,"name"],"setup_date":x.index[j],
                        "trigger_date":pd.NaT,"entry_price":np.nan,"stop":r.pull_low-0.1*r.atr14,"exit_date":pd.NaT,"net_return":np.nan,
                        "known_info":f"조정 {r.depth:.1%}/{int(r.duration)}일; 거래량비율 {r.vol_ratio:.2f}; 시장상승={bool(r.market_up)}",
                        "setup":"가격상 후보이나 필터 충돌","trigger":"3일 내 전일 고가 돌파 없음","outcome":"미진입(사후 결과로 판단하지 않음)",
                        "lesson":"Setup만으로 매수하지 않고 Trigger 부재 시 통과"}); break
    rows+=ambiguous
    cases=pd.DataFrame(rows); cases.to_csv("analysis_output/example_cases.csv",index=False)
    line_panel("16_examples_success.png","성공한 눌림목 5개",cases[cases.case_type=="성공"].to_dict("records"),data)
    line_panel("17_examples_failure.png","실패한 눌림목 5개",cases[cases.case_type=="실패"].to_dict("records"),data)
    line_panel("18_examples_ambiguous.png","Setup은 있지만 진입하지 않은 사례 3개",cases[cases.case_type=="진입 보류"].to_dict("records"),data)


def main():
    concept_diagram(); type_diagram()
    b=pd.read_csv("analysis_output/bucket_analysis.csv")
    for analysis,file,title in [("조정깊이","04_depth_performance.png","조정 깊이별 기대값"),("조정기간","05_duration_performance.png","조정 기간별 기대값"),("시장환경","06_market_regime.png","시장환경별 기대값")]:
        z=b[b.analysis==analysis]; labels=list(dict.fromkeys(z.bucket.astype(str)))
        series=[]
        for m,c in [("US",BLUE),("KR",ORANGE)]: series.append((m,[z[(z.market==m)&(z.bucket.astype(str)==lab)].expectancy.iloc[0] if len(z[(z.market==m)&(z.bucket.astype(str)==lab)]) else np.nan for lab in labels],c))
        save_bar(file,title,labels,series)
    comp=pd.read_csv("analysis_output/strategy_comparison.csv"); o=comp[comp.period=="OOS"]
    def cmp_family(prefix,file,title):
        z=o[o.label.str.startswith(prefix)]; labels=[s.split(":",1)[1] for s in z[z.market=="US"].label]
        series=[(m,[z[(z.market==m)&(z.label==prefix+":"+lab)].expectancy.iloc[0] for lab in labels],c) for m,c in [("US",BLUE),("KR",ORANGE)]]
        save_bar(file,title,labels,series)
    cmp_family("Entry","09_entry_comparison.png","진입 방식 비교")
    cmp_family("Stop","10_stop_comparison.png","손절 방식 비교")
    cmp_family("Exit","11_exit_comparison.png","청산 방식 비교")
    filt=o[o.label.str.startswith("Filter")]; labels=[f"F{i}" for i in range(6)]
    series=[]
    for m,c in [("US",BLUE),("KR",ORANGE)]:
        z=filt[filt.market==m].sort_values("label"); series.append((m,z.expectancy.tolist(),c))
    save_bar("15_incremental_filters.png","Alpha 필터 증분 테스트",labels,series,subtitle="F0 기본 → F1 거래량 → F2 시장 → F3 상대강도 → F4 변동성 → F5 신고가")
    base=o[o.label=="Filter:0:baseline"]; vol=o[o.label=="Filter:1:volume"]
    save_bar("07_volume_filter.png","거래량 필터 전/후",["기본","거래량≤0.75"],[(m,[float(base[base.market==m].expectancy.iloc[0]),float(vol[vol.market==m].expectancy.iloc[0])],c) for m,c in [("US",BLUE),("KR",ORANGE)]])
    f2=o[o.label.str.startswith("Filter:2")]; f3=o[o.label.str.startswith("Filter:3")]
    save_bar("08_rs_filter.png","상대강도 필터 추가 전/후",["거래량+시장","+상대강도"],[(m,[float(f2[f2.market==m].expectancy.iloc[0]),float(f3[f3.market==m].expectancy.iloc[0])],c) for m,c in [("US",BLUE),("KR",ORANGE)]])
    save_bar("12_expectancy.png","기본전략 vs 필터전략 기대값",["기본","필터 3단계"],[(m,[float(base[base.market==m].expectancy.iloc[0]),float(f3[f3.market==m].expectancy.iloc[0])],c) for m,c in [("US",BLUE),("KR",ORANGE)]])
    save_bar("13_max_drawdown.png","기본전략 vs 필터전략 최대낙폭",["기본","필터 3단계"],[(m,[float(base[base.market==m].max_drawdown.iloc[0]),float(f3[f3.market==m].max_drawdown.iloc[0])],c) for m,c in [("US",BLUE),("KR",ORANGE)]])
    rob=pd.read_csv("analysis_output/robustness.csv"); heatmap("14_robustness_us.png","파라미터 견고성: 미국",rob[rob.market=="US"])
    save_bar("03_volume_relationship.png","상승구간 대비 조정구간 거래량",["≤0.50","0.50-0.75","0.75-1.00",">1.00"],[(m,b[(b.analysis=="거래량비율")&(b.market==m)].expectancy.tolist(),c) for m,c in [("US",BLUE),("KR",ORANGE)]])
    raw=Path("data/raw_yahoo"); meta=pd.read_csv("config/pullback_universe.csv"); idx={"US":load_yahoo(raw/f"{safe_symbol('^GSPC')}.json","^GSPC"),"KR":load_yahoo(raw/f"{safe_symbol('^KS11')}.json","^KS11")}; data={}
    for r in meta.itertuples(index=False):
        try: data[r.symbol]=indicators(load_yahoo(raw/f"{safe_symbol(r.symbol)}.json",r.symbol),idx[r.market])
        except Exception: pass
    build_examples(data,meta)


if __name__=="__main__": main()
