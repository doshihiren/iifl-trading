"""Read-only 15-minute Camarilla / Wilder RSI backtest, index-points proxy only."""
from collections import defaultdict
from flask import Blueprint, jsonify, request, render_template_string
import candles as store
import db

bp = Blueprint("camarilla_rsi", __name__)

def aggregate(rows):
    slots=defaultdict(list)
    for x in sorted(rows,key=lambda x:x["ts"]):
        ts=x["ts"]; mins=int(ts[11:13])*60+int(ts[14:16]); offset=mins-555
        if 0<=offset<375:
            slots[(ts[:10],555+offset//15*15)].append(x)
    result=[]
    for (day,start),candles in sorted(slots.items()):
        if len(set(x["ts"] for x in candles))!=15: continue
        result.append(dict(day=day,ts=f"{day} {start//60:02d}:{start%60:02d}",
                           open=float(candles[0]["open"]),high=max(float(x["high"]) for x in candles),
                           low=min(float(x["low"]) for x in candles),close=float(candles[-1]["close"])))
    return result

def pivots(bars):
    days=defaultdict(list)
    for x in bars:days[x["day"]].append(x)
    result={}; previous=None
    for day in sorted(days):
        if previous and len(days[previous])==25:
            older=days[previous]; high=max(x["high"] for x in older);low=min(x["low"] for x in older)
            close=older[-1]["close"]; r=1.1*(high-low)
            result[day]={"S4":close-r/2,"S2":close-r/6,"R2":close+r/6,"R4":close+r/2}
        previous=day
    return result

def rsi21(bars):
    closes=[x["close"] for x in bars]; n=21; out=[None]*len(closes)
    if len(closes)<=n:return out
    ds=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    g=sum(max(d,0) for d in ds[:n])/n; l=sum(max(-d,0) for d in ds[:n])/n
    def calc(g,l):return 100-100/(1+g/l) if l else (100.0 if g else 50.0)
    out[n]=calc(g,l)
    for i in range(n+1,len(closes)):
        d=ds[i-1];g=(g*20+max(d,0))/21;l=(l*20+max(-d,0))/21;out[i]=calc(g,l)
    return out

def simulate(rows, start=None,end=None,tolerance=5.0,slippage=1.0,fees=0.0,quantity=1):
    if not(0<=tolerance<=200 and 0<=slippage<=100 and 0<=fees<=100 and 1<=quantity<=100000):
        raise ValueError("Invalid tolerance, slippage, fees or quantity")
    bars=aggregate(rows);levels=pivots(bars);values=rsi21(bars)
    trades=[];pending=None;position=None;equity=peak=drawdown=0.;signals=0
    def finish(px,ts,why):
        nonlocal position,equity,peak,drawdown
        p=position;gross=(px-p["entry"])*p["direction"]
        net=(gross-2*fees)*quantity;equity+=net;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
        trades.append({**p,"exit_ts":ts,"exit":round(px,3),"exit_reason":why,
                       "gross_points":round(gross,3),"net_points":round(gross-2*fees,3),
                       "net":round(net,2),"result":"WIN" if net>0 else "LOSS" if net<0 else "FLAT"})
        position=None
    for i,b in enumerate(bars):
        day=b["day"]
        if day not in levels or (start and day<start) or (end and day>end):continue
        lv=levels[day]
        if pending:
            p=pending;pending=None
            if p["day"]==day and b["ts"][11:]<"15:15":
                direction=p["direction"]; entry=b["open"]+direction*slippage
                if (direction==1 and p["stop"]<entry<p["target"]) or (direction==-1 and p["target"]<entry<p["stop"]):
                    position={"side":"BUY" if direction==1 else "SELL","direction":direction,
                              "level":p["level"],"signal_ts":p["signal_ts"],"entry_ts":b["ts"],
                              "entry":round(entry,3),"stop":p["stop"],"target":p["target"],
                              "rsi":round(p["rsi"],2)}
        if position:
            p=position;direction=p["direction"]
            stop=(b["low"]<=p["stop"]) if direction==1 else (b["high"]>=p["stop"])
            target=(b["high"]>=p["target"]) if direction==1 else (b["low"]<=p["target"])
            if stop:
                px=(min(b["open"],p["stop"])-slippage if direction==1
                    else max(b["open"],p["stop"])+slippage)
                finish(px,b["ts"],"STOP")
            elif target:finish(p["target"]-direction*slippage,b["ts"],"TARGET")
            elif b["ts"][11:]=="15:15":finish(b["close"]-direction*slippage,b["ts"],"EOD")
        if position or pending or b["ts"][11:]>="15:00" or i==0 or values[i] is None or values[i-1] is None:
            continue
        direction=1 if values[i-1]<40<=values[i] else -1 if values[i-1]>60>=values[i] else 0
        if not direction:continue
        choices=("S4","S2") if direction==1 else ("R4","R2")
        touched=[k for k in choices if b["low"]<=lv[k]+tolerance and b["high"]>=lv[k]-tolerance]
        if not touched:continue
        name=touched[0];goal=lv[{"S4":"S2","S2":"R2","R4":"R2","R2":"S2"}[name]]
        stop=b["low"] if direction==1 else b["high"]
        if (direction==1 and stop<b["close"]<goal) or (direction==-1 and goal<b["close"]<stop):
            pending={"day":day,"signal_ts":b["ts"],"direction":direction,"level":name,
                     "stop":stop,"target":goal,"rsi":values[i]};signals+=1
    wins=sum(t["result"]=="WIN" for t in trades);losses=sum(t["result"]=="LOSS" for t in trades)
    profits=sum(max(0,t["net"]) for t in trades);loss_amount=-sum(min(0,t["net"]) for t in trades)
    summary={"closed_trades":len(trades),"signals":signals,"wins":wins,"losses":losses,
             "flat":len(trades)-wins-losses,
             "win_rate_pct":round(100*wins/len(trades),2) if trades else None,
             "net_proxy_pnl":round(sum(t["net"] for t in trades),2),
             "profit_factor":round(profits/loss_amount,3) if loss_amount else None,
             "average_trade":round(sum(t["net"] for t in trades)/len(trades),2) if trades else None,
             "max_closed_trade_drawdown":round(drawdown,2),
             "complete_15m_bars":len(bars),"days_with_pivots":len(levels),
             "unit":"index points x quantity; NOT option premium P&L"}
    return {"summary":summary,"trades":trades}

@bp.get("/iifl/camarilla-rsi")
def page():
    return render_template_string(PAGE)

@bp.post("/iifl/api/camarilla-rsi/backtest")
def run():
    data=request.get_json(silent=True) or {}
    iid=str(data.get("instrument_id") or "")
    sym=db.row("SELECT * FROM analysis_symbols WHERE instrumentId=?",(iid,))
    if not sym:return jsonify(status="error",message="Choose a symbol synced in Analysis first"),400
    try:
        start=str(data.get("from") or "1900-01-01");end=str(data.get("to") or "2100-01-01")
        if start>end:raise ValueError("Start must be before end")
        rows=store.load(iid,"1900-01-01",end)
        if len(rows)<600:raise ValueError("Please sync at least two full 1-minute sessions")
        result=simulate(rows,start=start,end=end,tolerance=float(data.get("tolerance",5)),
                        slippage=float(data.get("slippage",1)),fees=float(data.get("fees",0)),
                        quantity=int(data.get("quantity",1)))
    except (TypeError,ValueError) as exc:
        return jsonify(status="error",message=str(exc)),400
    return jsonify(status="Ok",result=result,meta={"symbol":sym["symbol"],"first":rows[0]["ts"],
                                                     "last":rows[-1]["ts"],"candles":len(rows)})

PAGE='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Camarilla RSI Backtest</title><style>
body{background:#0d1929;color:#e5edf9;font:15px system-ui;margin:30px auto;max-width:1100px;padding:0 20px}
a{color:#9cbffa}.panel{border:1px solid #304966;border-radius:12px;padding:20px;margin:18px 0;background:#15243a}
input,select,button{padding:9px;background:#243954;color:white;border:1px solid #61728c;border-radius:6px;margin:6px}
button{background:#1766c8;cursor:pointer}.stats{display:flex;flex-wrap:wrap;gap:12px}
.stats div{background:#20354c;padding:12px;border-radius:9px;min-width:130px}.stats b{display:block;font-size:22px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:7px;border-bottom:1px solid #34465b;text-align:left}
#tradelist{overflow:auto;max-height:550px}
</style></head><body><a href="/iifl/">Dashboard</a> | <a href="/iifl/analysis">Sync NIFTY candles</a>
<h1>Camarilla + RSI(21) Backtest</h1>
<p>15-minute candles | Previous complete trading day's Camarilla pivots | RSI 40/60 crossover | Next-bar entries</p>
<div class="panel"><label>Stored symbol <select id="symbol"></select></label>
<label>From <input type="date" id="from"></label><label>To <input type="date" id="to"></label>
<label>Pivot touch tolerance <input id="tolerance" type="number" value="5" step=".5" min="0"></label>
<label>Slippage points per side <input id="slippage" type="number" value="1" step=".5" min="0"></label>
<label>Fees points per side <input id="fees" type="number" value="0" step=".1" min="0"></label>
<label>Index proxy quantity <input id="quantity" type="number" value="1" min="1"></label>
<p><button onclick="runBacktest()">Run Backtest</button> <span id="message"></span></p>
<small>Read-only analysis. NOT a real NIFTY CE/PE options backtest. P&L is a synthetic index-points proxy.</small></div>
<div class="panel" id="output" hidden><div class="stats" id="stats"></div><h3>Closed trades</h3>
<div id="tradelist"><table><thead><tr><th>Side</th><th>Level</th><th>Entry</th><th>Entry price</th><th>Stop</th>
<th>Target</th><th>Exit time</th><th>Exit price</th><th>Reason</th><th>Net proxy</th></tr></thead>
<tbody id="trades"></tbody></table></div></div>
<script>
function el(id){return document.getElementById(id)}
function escapeHTML(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){
return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
fetch('/iifl/api/analysis/symbols').then(r=>r.json()).then(d=>{
el('symbol').innerHTML=(d.result||[]).map(s=>'<option value="'+escapeHTML(s.instrumentId)+'">'+escapeHTML(s.symbol)+' ('+escapeHTML(s.days)+' days)</option>').join('');
if(!d.result.length)el('message').textContent='Add and sync NIFTY in Analysis first.'
}).catch(e=>el('message').textContent=e.message)
async function runBacktest(){
let payload={instrument_id:el('symbol').value};
['from','to','tolerance','slippage','fees','quantity'].forEach(k=>{if(el(k).value)payload[k]=el(k).value});
el('message').textContent='Running...';el('output').hidden=true;
try{
let response=await fetch('/iifl/api/camarilla-rsi/backtest',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
let data=await response.json();if(!response.ok)throw Error(data.message||'Backtest error');
let s=data.result.summary;el('stats').innerHTML=Object.keys(s).map(k=>'<div><small>'+escapeHTML(k.replace(/_/g,' '))+'</small><b>'+escapeHTML(s[k])+'</b></div>').join('');
el('trades').innerHTML=data.result.trades.map(t=>'<tr>'+['side','level','entry_ts','entry','stop','target','exit_ts','exit','exit_reason','net'].map(k=>'<td>'+escapeHTML(t[k])+'</td>').join('')+'</tr>').join('');
el('message').textContent='Done: '+data.meta.first+' to '+data.meta.last;el('output').hidden=false;
}catch(e){el('message').textContent=e.message}
}
</script></body></html>'''
