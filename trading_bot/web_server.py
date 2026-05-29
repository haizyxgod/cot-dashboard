"""Web Dashboard v3 — equity graph, monthly P&L, CSV export, theme, mobile."""
import threading
import io
import csv
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, Response
from flask_cors import CORS
import db as database

app = Flask(__name__)
CORS(app)

pending_signals = {}
database.init()

bot_state = {"last_scan": None, "scan_result": "", "mt5_connected": False, "auto_mode": True}
log_entries = []

def add_log(msg):
    log_entries.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg})
    if len(log_entries) > 200: log_entries.pop()

HTML = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FIN Bot</title>
<style>
    :root{--bg:#0d1117;--bg2:#161b22;--border:#21262d;--text:#8b949e;--head:#c9d1d9;--muted:#484f58;--sub:#6e7681}
    [data-theme="light"]{--bg:#f6f8fa;--bg2:#fff;--border:#d0d7de;--text:#57606a;--head:#1f2328;--muted:#8b949e;--sub:#656d76}
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:12px;min-height:100vh}
    .container{max-width:960px;margin:0 auto}
    .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:8px}
    .header h1{font-size:1.1rem;color:var(--head)}
    .status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
    .status-dot.online{background:#00e676;animation:pulse 2s infinite}
    .status-dot.offline{background:#ff5252}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.1}}
    .nav{display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap}
    .nav a{color:var(--muted);text-decoration:none;font-size:0.8rem;font-weight:600;padding:4px 0;border-bottom:2px solid transparent}
    .nav a:hover,.nav a.active{color:var(--head);border-bottom-color:#00e676}
    .theme-btn{background:var(--bg2);border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:0.7rem}

    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
    .card{background:var(--bg2);border-radius:10px;padding:12px 14px;border:1px solid var(--border)}
    .card .label{font-size:0.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
    .card .value{font-size:1.2rem;font-weight:700;color:var(--head);margin-top:2px}
    .card .sub{font-size:0.65rem;color:var(--sub);margin-top:2px}
    .val-green{color:#00e676!important}.val-red{color:#ff5252!important}.val-gold{color:#ffab00!important}

    .section-title{font-size:0.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:16px 0 8px}
    .pos-row{background:var(--bg2);border-radius:10px;padding:14px 16px;margin:8px 0;display:flex;align-items:center;gap:12px;border:1px solid var(--border);font-size:0.9rem;flex-wrap:wrap}
    .pos-row .pair{font-weight:700;min-width:70px;color:var(--head);font-size:0.95rem}
    .pos-row .dir{min-width:65px;font-size:0.8rem}
    .dir.buy{color:#00e676}.dir.sell{color:#ff5252}
    .pos-row .info{flex:1;font-size:0.78rem;color:var(--sub);min-width:200px}
    .pos-row .info span{color:var(--text)}
    .pos-row .pnl{font-weight:700;min-width:85px;text-align:right;font-size:0.95rem}
    .pnl.pos{color:#00e676}.pnl.neg{color:#ff5252}
    .be-badge{font-size:0.7rem;padding:3px 8px;border-radius:4px;font-weight:700}
    .be-active{background:#1a3a2a;color:#00e676}.be-waiting{background:#3a2a1a;color:#ffab00}
    .sl-tp{font-size:0.68rem;color:var(--muted);margin-top:3px}

    .dow-bar{flex:1;text-align:center;background:var(--bg2);border-radius:8px;padding:8px 4px;border:1px solid var(--border);min-width:40px}
    .dow-bar .label{font-size:0.65rem;color:var(--muted)}
    .dow-bar .bar{height:24px;border-radius:3px;margin:4px 0;min-width:100%;transition:height 0.5s}
    .dow-bar .val{font-size:0.6rem;color:var(--sub)}
    .bar-pos{background:linear-gradient(to top,#00e67644,#00e676)}.bar-neg{background:linear-gradient(to bottom,#ff525244,#ff5252)}.bar-zero{background:var(--border)}

    .monthly-table{width:100%;font-size:0.72rem;border-collapse:collapse;margin-bottom:12px}
    .monthly-table th,.monthly-table td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--border)}
    .monthly-table th{color:var(--muted);font-weight:600;font-size:0.65rem;text-transform:uppercase}
    .monthly-table td:first-child{text-align:left;color:var(--text)}
    .monthly-table .pnl-pos{color:#00e676}.monthly-table .pnl-neg{color:#ff5252}

    .hist-entry{background:var(--bg2);border-radius:8px;padding:8px 13px;margin:4px 0;font-size:0.78rem;display:flex;align-items:center;gap:10px;border:1px solid var(--border);flex-wrap:wrap}
    .hist-entry .tag{font-size:0.6rem;padding:2px 6px;border-radius:3px;font-weight:700;min-width:55px;text-align:center}
    .tag-win{background:#0d3320;color:#00e676}.tag-loss{background:#330d0d;color:#ff5252}.tag-be{background:var(--bg2);color:var(--sub)}

    .filter-row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
    .filter-row select,.filter-row input{padding:6px 10px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:0.75rem}
    .filter-row select:focus,.filter-row input:focus{outline:none;border-color:#00e676}
    .btn{padding:6px 14px;border:none;border-radius:6px;cursor:pointer;font-size:0.75rem;font-weight:700;text-decoration:none;display:inline-block}
    .btn-ok{background:#00c853;color:#fff}.btn-out{background:var(--bg2);color:var(--text);border:1px solid var(--border)}

    canvas{max-width:100%;margin:8px 0}

    .log-entry{font-size:0.72rem;padding:4px 0;border-bottom:1px solid var(--border);display:flex;gap:10px}
    .log-time{color:var(--muted);min-width:50px}.log-msg{color:var(--sub)}.log-msg .hl{color:var(--head)}
    .empty{text-align:center;padding:30px;color:var(--muted)}
    .toast{position:fixed;top:16px;right:16px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px 18px;z-index:999;min-width:220px;box-shadow:0 8px 24px rgba(0,0,0,0.4);animation:slideIn 0.3s ease}
    .toast-ok{border-left:3px solid #00e676}.toast-err{border-left:3px solid #ff5252}
    .toast .t-title{font-size:0.85rem;color:var(--head);font-weight:600}
    .toast .t-body{font-size:0.72rem;color:var(--sub);margin-top:2px}
    @keyframes slideIn{from{transform:translateX(120%)}to{transform:translateX(0)}}
    .refresh-note{font-size:0.65rem;color:var(--muted);text-align:center;margin-top:12px}

    @media(max-width:600px){
        .cards{grid-template-columns:repeat(2,1fr)}
        .header h1{font-size:1rem}
        .pos-row .info{min-width:150px;font-size:0.68rem}
    }
</style>
</head>
<body>
<div class="container">

<div class="header">
    <div>
        <h1>FIN Trading Bot</h1>
        <span style="font-size:0.7rem;color:var(--muted)">
            <span class="status-dot {{ 'online' if state.mt5_connected else 'offline' }}"></span>
            {{ 'MT5 Online' if state.mt5_connected else 'MT5 Offline' }}
            &nbsp;|&nbsp; Скан: <b id="next-scan">--</b>
            <button id="btn-scan" class="btn btn-ok" onclick="manualScan()" style="font-size:0.65rem;padding:3px 10px;margin-left:8px" title="Запустить сканирование">▶ Скан</button>
            <label class="mode-toggle" title="Авто / Ручной" style="margin-left:6px;cursor:pointer;display:inline-flex;align-items:center;gap:4px">
                <span style="font-size:0.6rem;color:var(--muted)">АВТО</span>
                <input type="checkbox" id="mode-switch" onchange="toggleAutoMode()" checked style="display:none">
                <span id="mode-knob" style="display:inline-block;width:30px;height:16px;background:#00c853;border-radius:8px;position:relative;transition:background 0.2s">
                    <span style="display:inline-block;width:12px;height:12px;background:#fff;border-radius:50%;position:absolute;top:2px;right:2px;transition:all 0.2s"></span>
                </span>
            </label>
        </span>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
        <div style="font-size:0.72rem;color:var(--text);text-align:right" id="live-prices">Gold: -- | JPY: --</div>
        <button class="theme-btn" onclick="toggleTheme()" title="Тема">☀/☾</button>
        <span style="font-size:0.65rem;color:var(--muted)" id="clock">--</span>
    </div>
</div>

<div class="nav">
    <a href="/bot" class="{{ 'active' if tab == 'dashboard' else '' }}">Дашборд</a>
    <a href="/bot/positions" class="{{ 'active' if tab == 'positions' else '' }}">Позиции</a>
    <a href="/bot/history" class="{{ 'active' if tab == 'history' else '' }}">История</a>
    <a href="/bot/stats" class="{{ 'active' if tab == 'stats' else '' }}">Статистика</a>
    <a href="/bot/login" class="{{ 'active' if tab == 'login' else '' }}">Аккаунт</a>
</div>

{% if tab == 'dashboard' %}
<div class="cards" id="cards">
    <div class="card"><div class="label">Баланс</div><div class="value" id="bal">--</div><div class="sub">Equity: <span id="eq">--</span></div></div>
    <div class="card"><div class="label">Открыто позиций</div><div class="value" id="pos-count">--</div><div class="sub">BE: <span id="be-count">--</span></div></div>
    <div class="card"><div class="label">P&L (открытые)</div><div class="value" id="open-pnl">--</div><div class="sub" id="pnl-sub"></div></div>
    <div class="card"><div class="label">P&L (закрытые)</div><div class="value" id="closed-pnl">--</div><div class="sub" id="closed-sub"></div></div>
    <div class="card"><div class="label">P&L Сегодня</div><div class="value" id="daily-pnl">--</div><div class="sub" id="daily-sub"></div></div>
    <div class="card"><div class="label">Лучшая / Худшая</div><div class="value" style="font-size:0.9rem"><span id="best-trade" style="color:#00e676">--</span> / <span id="worst-trade" style="color:#ff5252">--</span></div><div class="sub">сделка</div></div>
</div>

<div class="section-title">P&amp;L по месяцам</div>
<div style="overflow-x:auto">
<table class="monthly-table" id="monthly-table"><tbody><tr><td colspan="13">Загрузка...</td></tr></tbody></table>
</div>

<div class="section-title">P&amp;L по дням недели</div>
<div style="display:flex;gap:4px;margin-bottom:12px" id="dow-heatmap">
    <div class="dow-bar"><div class="label">Пн</div><div class="bar"></div><div class="val">--</div></div>
    <div class="dow-bar"><div class="label">Вт</div><div class="bar"></div><div class="val">--</div></div>
    <div class="dow-bar"><div class="label">Ср</div><div class="bar"></div><div class="val">--</div></div>
    <div class="dow-bar"><div class="label">Чт</div><div class="bar"></div><div class="val">--</div></div>
    <div class="dow-bar"><div class="label">Пт</div><div class="bar"></div><div class="val">--</div></div>
</div>

<div class="section-title">Открытые позиции <span style="font-weight:400;color:var(--muted)" id="pos-time"></span></div>
<div id="positions"><div class="empty">Загрузка...</div></div>

<div class="section-title">Последние события</div>
<div id="log"><div class="empty">Загрузка...</div></div>

<div class="refresh-note">Автообновление 10с &bull; <span id="refresh-counter"></span> &bull; <a href="/api/export/csv" class="btn btn-out" style="font-size:0.65rem;padding:2px 8px;margin-left:6px">CSV</a></div>

{% elif tab == 'positions' %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
    <span style="font-size:0.85rem;color:var(--text)">{{ count }} поз. | P&L: <b style="color:{{ '#00e676' if total_pnl >= 0 else '#ff5252' }}">{{ total_pnl }}</b></span>
    {% if positions %}
    <form method="POST" action="/bot/close_all">
        <button class="btn" style="background:#ff5252;color:#fff">Закрыть всё</button>
    </form>
    {% endif %}
</div>
{% if positions %}
    {% for p in positions %}
    <div class="pos-row">
        <div class="pair">{{ p.symbol }}</div>
        <div class="dir {{ 'buy' if p.type == 0 else 'sell' }}">{{ '▲' if p.type == 0 else '▼' }} {{ 'BUY' if p.type == 0 else 'SELL' }}</div>
        <div class="info">
            Vol: <span>{{ p.volume }}</span> | Open: <span>{{ p.price_open }}</span> | Curr: <span>{{ p.price_current }}</span>
            {% if p.get('sl') %} | SL: <span style="color:#ff5252">{{ p.sl }}</span>{% endif %}
            {% if p.get('tp') %} | TP: <span style="color:#00e676">{{ p.tp }}</span>{% endif %}
        </div>
        {% if p.get('be_triggered') %}<span class="be-badge be-active">BE</span>{% endif %}
        <div class="pnl {{ 'pos' if p.profit > 0 else 'neg' }}">${{ '{:+.2f}'.format(p.profit) }}</div>
        <div style="display:flex;gap:4px">
            {% if not p.get('be_triggered') %}
            <form onsubmit="event.preventDefault();submitAction('/bot/be/{{ p.ticket }}')" style="margin:0"><button class="btn btn-out" style="font-size:0.75rem;padding:6px 14px">BE</button></form>
            {% endif %}
            <form onsubmit="event.preventDefault();submitAction('/bot/close/{{ p.ticket }}')" style="margin:0"><button class="btn" style="background:#ff5252;color:#fff;font-size:0.75rem;padding:6px 14px">✕</button></form>
        </div>
    </div>
    {% endfor %}
{% else %}
    <div class="empty">Нет открытых позиций</div>
{% endif %}

{% elif tab == 'history' %}
<div class="filter-row">
    <form method="GET" action="/bot/history" style="display:flex;gap:8px;flex-wrap:wrap">
        <select name="pair"><option value="">Все пары</option>
            {% for p in ['XAU/USD','USD/JPY'] %}
            <option value="{{ p }}" {{ 'selected' if pair_filter == p else '' }}>{{ p }}</option>
            {% endfor %}
        </select>
        <input name="date" type="month" value="{{ date_filter }}">
        <button class="btn btn-ok" type="submit">Фильтр</button>
        <a href="/bot/history" class="btn btn-out">Сброс</a>
    </form>
</div>
{% if history %}
    {% for h in history %}
    <div class="hist-entry">
        <span class="tag {{ 'tag-win' if h.get('result') == 'win' else 'tag-loss' if h.get('result') == 'loss' else 'tag-be' }}">{{ h.get('result', '?') }}</span>
        <span style="font-weight:600;min-width:55px;color:var(--head)">{{ h.pair }}</span>
        <span style="font-size:0.7rem;color:var(--sub)">{{ '▲ BUY' if h.direction == 'BUY' else '▼ SELL' }}</span>
        <span style="flex:1;font-size:0.72rem;color:var(--sub)">
            Entry: {{ h.entry_price }} | Lot: {{ h.volume }}
            {% if h.get('pnl') %}| P&L: <span style="color:{{ '#00e676' if h.pnl > 0 else '#ff5252' }}">${{ '{:+.2f}'.format(h.pnl) }}</span>{% endif %}
        </span>
        <span style="font-size:0.65rem;color:var(--muted)">{{ h.time[:19] if h.time else '' }}</span>
    </div>
    {% endfor %}
{% else %}
    <div class="empty">История пуста</div>
{% endif %}
{% elif tab == 'stats' %}
<div class="section-title">Детальная статистика</div>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px" id="stats-grid">
    <div class="empty">Загрузка...</div>
</div>

<script>
async function loadStats(){
    try{
        var d=await fetch('/api/stats/detailed').then(r=>r.json());
        var html='';
        Object.keys(d).sort().forEach(function(pair){
            var s=d[pair];
            var cards=[
                ['Сделок',s.trades+' ('+s.be+' BE)'],
                ['Win Rate',s.win_rate+'%'],
                ['Wins / Losses',s.wins+' / '+s.losses],
                ['P&L','$'+(s.total_pnl>=0?'+':'')+s.total_pnl.toFixed(0)],
                ['Profit Factor',s.profit_factor],
                ['Avg Win / Loss','$'+s.avg_win+' / $'+s.avg_loss],
                ['Avg RR (реализованный)',s.avg_rr],
                ['Max DD',s.max_dd_pct+'%'],
                ['Sharpe',s.sharpe],
                ['Max убытков подряд',s.max_consec_loss],
                ['Лучшая / Худшая','$'+s.best+' / $'+s.worst],
            ];
            html+='<div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px">';
            html+='<div style="font-size:0.95rem;font-weight:700;color:var(--head);margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)">'+pair+'</div>';
            html+='<table style="width:100%;font-size:0.78rem">';
            cards.forEach(function(c){
                html+='<tr><td style="padding:5px 0;color:var(--muted)">'+c[0]+'</td><td style="padding:5px 0;text-align:right;color:var(--head);font-weight:600">'+c[1]+'</td></tr>';
            });
            html+='</table></div>';
        });
        document.getElementById('stats-grid').innerHTML=html||'<div class="empty">Нет данных</div>';
    }catch(e){
        document.getElementById('stats-grid').innerHTML='<div class="empty">Ошибка загрузки</div>';
    }
}
loadStats();
</script>

{% endif %}

</div>

<script>
var isDashboard = {{ 'true' if tab == 'dashboard' else 'false' }};
var scanMin = {{ scan_interval if scan_interval is defined else 180 }};
var autoMode = {{ 'true' if auto_mode else 'false' }};

function updateTimer(){
    var now=new Date(), next=new Date(now);
    var h = now.getHours();
    var nextHour = h - (h % 3) + 3;
    if (nextHour >= 24) {
        nextHour = 0;
        next.setDate(next.getDate() + 1);
    }
    next.setHours(nextHour, 0, 0, 0);
    var d=Math.floor((next-now)/1000);
    document.getElementById('next-scan').textContent=Math.floor(d/3600)+'h '+Math.floor(d%3600/60)+'m';
    document.getElementById('clock').textContent=now.toLocaleTimeString();
}
setInterval(updateTimer,1000); updateTimer();

// Init mode toggle
(function initMode(){
    var knob=document.getElementById('mode-knob');
    var label=document.getElementById('mode-switch').parentElement.querySelector('span');
    if(!autoMode){
        document.getElementById('mode-switch').checked=false;
        knob.style.background='#ff5252';
        knob.querySelector('span').style.right='16px';
        label.textContent='РУЧН';
        document.getElementById('next-scan').style.opacity='0.4';
        document.getElementById('btn-scan').style.opacity='1';
    }
})();

var theme=localStorage.getItem('theme')||'dark';
document.documentElement.setAttribute('data-theme',theme);
function toggleTheme(){
    theme=theme==='dark'?'light':'dark';
    document.documentElement.setAttribute('data-theme',theme);
    localStorage.setItem('theme',theme);
}

function showToast(title,body,type){
    var t=document.createElement('div');
    t.className='toast '+(type==='ok'?'toast-ok':'toast-err');
    t.innerHTML='<div class="t-title">'+title+'</div><div class="t-body">'+body+'</div>';
    document.body.appendChild(t);
    setTimeout(function(){t.style.opacity='0';t.style.transition='opacity 0.3s';setTimeout(function(){t.remove()},300)},2500);
}

async function submitAction(url){
    try{
        var r=await fetch(url,{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}});
        var d=await r.json();
        if(d.ok){showToast('Готово',d.msg,'ok')}else{showToast('Ошибка',d.msg,'err')}
        setTimeout(function(){if(isDashboard)refresh();else window.location.reload()},800);
    }catch(e){showToast('Ошибка','Не удалось выполнить','err')}
}

async function manualScan(){
    var btn=document.getElementById('btn-scan');
    btn.disabled=true; btn.textContent='...';
    try{
        var r=await fetch('/bot/scan',{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}});
        var d=await r.json();
        showToast(d.ok?'Скан':'Ошибка',d.msg,d.ok?'ok':'err');
    }catch(e){showToast('Ошибка','Скан не удался','err')}
    btn.disabled=false; btn.textContent='▶ Скан';
    if(isDashboard) refresh();
}

async function toggleAutoMode(){
    try{
        var r=await fetch('/bot/mode',{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}});
        var d=await r.json();
        var knob=document.getElementById('mode-knob');
        var label=document.getElementById('mode-switch').parentElement.querySelector('span');
        if(d.auto_mode){
            knob.style.background='#00c853';
            knob.querySelector('span').style.right='2px';
            label.textContent='АВТО';
            document.getElementById('next-scan').style.opacity='1';
            document.getElementById('btn-scan').style.opacity='0.5';
        }else{
            knob.style.background='#ff5252';
            knob.querySelector('span').style.right='16px';
            label.textContent='РУЧН';
            document.getElementById('next-scan').style.opacity='0.4';
            document.getElementById('btn-scan').style.opacity='1';
        }
        showToast('Режим',d.msg,'ok');
        if(isDashboard) refresh();
    }catch(e){showToast('Ошибка','Не удалось переключить','err')}
}

var refreshCount=0;

function buildMonthlyTable(months){
    var t=document.getElementById('monthly-table');
    if(!t)return;
    var keys=Object.keys(months).sort();
    if(!keys.length){t.innerHTML='<tbody><tr><td colspan="13">Нет данных</td></tr></tbody>';return;}
    var html='<thead><tr><th>Год</th>';
    for(var m=1;m<=12;m++)html+='<th>'+['Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек'][m-1]+'</th>';
    html+='<th>Год</th></tr></thead><tbody>';
    var years={};
    keys.forEach(function(k){var y=k.substring(0,4);if(!years[y])years[y]={};years[y][k]=months[k];});
    Object.keys(years).sort().forEach(function(y){
        html+='<tr><td style="color:var(--head);font-weight:600">'+y+'</td>';
        var yTotal=0;
        for(var m=1;m<=12;m++){
            var mk=y+'-'+String(m).padStart(2,'0'),v=months[mk]||0;
            yTotal+=v;
            if(v===0)html+='<td style="color:var(--muted)">-</td>';
            else html+='<td class="'+(v>=0?'pnl-pos':'pnl-neg')+'">$'+(v>=0?'+':'')+v.toFixed(0)+'</td>';
        }
        html+='<td style="font-weight:700" class="'+(yTotal>=0?'pnl-pos':'pnl-neg')+'">$'+(yTotal>=0?'+':'')+yTotal.toFixed(0)+'</td></tr>';
    });
    html+='</tbody>';
    t.innerHTML=html;
}

async function refresh(){
    refreshCount++;
    document.getElementById('refresh-counter').textContent='обновление #'+refreshCount;
    var p=null,s=null,l=null,pr=null;
    try{p=await fetch('/api/positions').then(r=>r.json())}catch(e){}
    try{s=await fetch('/api/stats').then(r=>r.json())}catch(e){}
    try{l=await fetch('/api/log?n=10').then(r=>r.json())}catch(e){}
    try{pr=await fetch('/api/prices').then(r=>r.json())}catch(e){}
    if(!s)return;

    if(pr && !pr.error){
        var g=pr['XAU/USD']||{}, u=pr['USD/JPY']||{};
        document.getElementById('live-prices').innerHTML=
            'Gold <b style="color:#ffab00">'+(g.bid||0).toFixed(2)+'</b> | '+
            'JPY <b style="color:#ffab00">'+(u.bid||0).toFixed(3)+'</b>';
    }

    // Cards
    document.getElementById('bal').textContent='$'+s.balance.toFixed(0);
    document.getElementById('eq').textContent='$'+s.equity.toFixed(0);
    document.getElementById('pos-count').textContent=s.positions_count;
    document.getElementById('be-count').textContent=s.be_count;
    var opnl=s.open_pnl, opp=s.open_pnl_pct||0;
    var opnEl=document.getElementById('open-pnl');
    opnEl.textContent='$'+(opnl>=0?'+':'')+opnl.toFixed(2);
    opnEl.className='value '+(opnl>=0?'val-green':'val-red');
    document.getElementById('pnl-sub').textContent=(opp>=0?'+':'')+opp.toFixed(2)+'% | '+s.be_count+'/'+s.positions_count+' in BE';
    var cpnl=s.closed_pnl, cpp=cpnl/s.balance*100||0;
    var cpnlEl=document.getElementById('closed-pnl');
    cpnlEl.textContent='$'+(cpnl>=0?'+':'')+cpnl.toFixed(2);
    cpnlEl.className='value '+(cpnl>=0?'val-green':'val-red');
    document.getElementById('closed-sub').textContent=(cpp>=0?'+':'')+cpp.toFixed(2)+'% | '+s.total_trades+' сделок';
    var dpnl=s.daily_pnl||0, dpp=s.daily_pnl_pct||0;
    var dpnlEl=document.getElementById('daily-pnl');
    dpnlEl.textContent='$'+(dpnl>=0?'+':'')+dpnl.toFixed(2);
    dpnlEl.className='value '+(dpnl>=0?'val-green':'val-red');
    document.getElementById('daily-sub').textContent=(dpp>=0?'+':'')+dpp.toFixed(2)+'% сегодня';
    document.getElementById('best-trade').textContent='$'+(s.best_trade||0).toFixed(0);
    document.getElementById('worst-trade').textContent='$'+(s.worst_trade||0).toFixed(0);

    // Monthly P&L
    if(s.monthly_pnl) buildMonthlyTable(s.monthly_pnl);

    // DOW heatmap
    var dow=s.dow_pnl||{}, days=['Пн','Вт','Ср','Чт','Пт'];
    var maxAbs=1;
    days.forEach(function(d){maxAbs=Math.max(maxAbs,Math.abs(dow[d]||0))});
    days.forEach(function(d,i){
        var v=dow[d]||0, pct=Math.min(Math.abs(v)/maxAbs*100,100);
        var bar=document.querySelectorAll('#dow-heatmap .dow-bar')[i];
        if(!bar)return;
        bar.querySelector('.val').textContent='$'+(v>=0?'+':'')+v.toFixed(0);
        var b=bar.querySelector('.bar');
        b.style.height=(pct||4)+'px';
        b.className='bar '+(v>0?'bar-pos':v<0?'bar-neg':'bar-zero');
    });

    // Positions
    document.getElementById('pos-time').textContent='(live)';
    var ph=document.getElementById('positions');
    if(!p||!p.positions||p.positions.length===0){
        ph.innerHTML='<div class="empty">Нет открытых позиций</div>';
    }else{
        var html='';
        p.positions.forEach(function(pos){
            var dirClass=pos.type===0?'buy':'sell';
            var dirArrow=pos.type===0?'&#9650;':'&#9660;';
            var pnlClass=pos.profit>=0?'pos':'neg';
            var dur=pos.duration||'';
            var slDist=pos.sl_dist_pct||0, tpDist=pos.tp_dist_pct||0;
            var beHtml=pos.be_triggered?
                '<span class="be-badge be-active">BE</span>':
                '<span class="be-badge be-waiting">SL</span>';
            var btns='<div style="display:flex;gap:5px;margin-left:6px">';
            if(!pos.be_triggered) btns+='<button class="btn btn-out" onclick="submitAction(\'/bot/be/'+pos.ticket+'\')" style="font-size:0.75rem;padding:6px 14px">BE</button>';
            btns+='<button class="btn" onclick="submitAction(\'/bot/close/'+pos.ticket+'\')" style="background:#ff5252;color:#fff;font-size:0.75rem;padding:6px 14px">✕</button></div>';
            html+='<div class="pos-row">'+
                '<div class="pair">'+pos.symbol+'</div>'+
                '<div class="dir '+dirClass+'">'+dirArrow+' '+(pos.type===0?'BUY':'SELL')+'</div>'+
                '<div class="info">'+
                    'Entry: <span>'+pos.entry+'</span> &nbsp;'+
                    'SL: <span style="color:#ff5252">'+pos.sl+'</span> &nbsp;'+
                    'TP: <span style="color:#00e676">'+pos.tp+'</span> &nbsp;'+
                    'Lot: <span>'+pos.volume+'</span>'+
                    (dur?' &nbsp; <span style="color:var(--muted)">'+dur+'</span>':'')+
                    '<div class="sl-tp">SL: <span style="color:#ff5252">'+(slDist>=0?'+':'')+slDist.toFixed(2)+'%</span> | TP: <span style="color:#00e676">'+(tpDist>=0?'+':'')+tpDist.toFixed(2)+'%</span></div>'+
                '</div>'+beHtml+
                '<div class="pnl '+pnlClass+'">$'+(pos.profit>=0?'+':'')+pos.profit.toFixed(2)+'</div>'+
                btns+
                '</div>';
        });
        ph.innerHTML=html;
    }

    // Log
    var lh=document.getElementById('log');
    if(!l||!l.log||l.log.length===0){
        lh.innerHTML='<div class="empty">Нет событий</div>';
    }else{
        var lhtml='';
        l.log.forEach(function(e){
            lhtml+='<div class="log-entry"><span class="log-time">'+e.time+'</span><span class="log-msg">'+e.msg+'</span></div>';
        });
        lh.innerHTML=lhtml;
    }
}
if(isDashboard){ refresh(); setInterval(refresh,10000); }
</script>
</body>
</html>"""

# --- API ---

@app.route("/api/positions")
def api_positions():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        positions = mt5.get_positions()
        mt5.disconnect()
        result = []
        for p in positions:
            ticket = p["ticket"]
            be_info = _main.be_tracked.get(ticket, {})
            open_time = p.get("time", None)
            duration = ""
            if open_time:
                if isinstance(open_time, (int, float)):
                    open_time_dt = datetime.fromtimestamp(open_time)
                    open_time_str = str(open_time_dt)[:19]
                else:
                    open_time_str = str(open_time)[:19]
                    open_time_dt = open_time
                delta = datetime.now() - open_time_dt if isinstance(open_time_dt, datetime) else None
                if delta:
                    days = delta.days
                    hours, rem = divmod(delta.seconds, 3600)
                    mins = rem // 60
                    if days > 0: duration = f"{days}d {hours}h"
                    elif hours > 0: duration = f"{hours}h {mins}m"
                    else: duration = f"{mins}m"
            else:
                open_time_str = None
            entry_p = p["price_open"]
            sl_p = p.get("sl", 0)
            tp_p = p.get("tp", 0)
            sl_dist = tp_dist = 0
            if entry_p and sl_p: sl_dist = round((sl_p - entry_p) / entry_p * 100, 2)
            if entry_p and tp_p: tp_dist = round((tp_p - entry_p) / entry_p * 100, 2)
            result.append({
                "ticket": ticket, "symbol": p["symbol"], "type": p["type"],
                "volume": p["volume"], "entry": entry_p, "sl": sl_p, "tp": tp_p,
                "profit": p.get("profit", 0),
                "be_triggered": be_info.get("be_triggered", False),
                "open_time": open_time_str, "duration": duration,
                "sl_dist_pct": sl_dist, "tp_dist_pct": tp_dist,
            })
        return jsonify({"positions": result})
    except Exception as e:
        return jsonify({"positions": [], "error": str(e)})


@app.route("/api/stats")
def api_stats():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))

        mt5.connect()
        acc = mt5.get_account_summary()
        positions = mt5.get_positions()
        mt5.disconnect()

        open_pnl = sum(p.get("profit", 0) for p in positions)
        be_count = sum(1 for p in positions
                       if _main.be_tracked.get(p["ticket"], {}).get("be_triggered"))
        orders = database.get_order_history(5000)
        closed_orders = [o for o in orders if o.get("result") in ("win", "loss", "be")]
        total_trades = len(closed_orders)
        wins = sum(1 for o in closed_orders if o.get("result") == "win")
        losses = sum(1 for o in closed_orders if o.get("result") == "loss")
        closed_pnl = sum(o.get("pnl", 0) for o in closed_orders)
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        today = datetime.now().strftime("%Y-%m-%d")
        daily_pnl = sum(o.get("pnl", 0) for o in closed_orders if (o.get("time") or "").startswith(today))

        # Best/worst trade
        pnls = [o.get("pnl", 0) for o in closed_orders]
        best = max(pnls) if pnls else 0
        worst = min(pnls) if pnls else 0

        # Day-of-week
        dow_names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
        dow_pnl = {d: 0.0 for d in dow_names}
        for o in closed_orders:
            t = o.get("time", "")
            if t:
                try:
                    dt = datetime.fromisoformat(t[:19])
                    dow_pnl[dow_names[dt.weekday()]] += o.get("pnl", 0)
                except: pass

        # Monthly P&L
        monthly_pnl = {}
        for o in closed_orders:
            t = o.get("time", "")
            if t:
                monthly_pnl[t[:7]] = monthly_pnl.get(t[:7], 0) + o.get("pnl", 0)

        bal = acc.get("balance", 0)
        open_pnl_pct = round(open_pnl / bal * 100, 2) if bal > 0 else 0
        daily_pnl_pct = round(daily_pnl / bal * 100, 2) if bal > 0 else 0

        return jsonify({
            "balance": bal, "equity": acc.get("equity", 0),
            "positions_count": len(positions), "be_count": be_count,
            "open_pnl": open_pnl, "open_pnl_pct": open_pnl_pct,
            "total_trades": total_trades, "closed_pnl": closed_pnl,
            "win_rate": wr, "daily_pnl": daily_pnl, "daily_pnl_pct": daily_pnl_pct,
            "best_trade": best, "worst_trade": worst,
            "dow_pnl": dow_pnl, "monthly_pnl": monthly_pnl,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/prices")
def api_prices():
    try:
        from mt5_client import client as mt5
        import config
        mt5.connect()
        prices = {}
        for pair_name, symbol in config.PAIRS.items():
            tick = mt5.get_current_price(symbol)
            prices[pair_name] = {"bid": tick["bid"], "ask": tick["ask"]}
        mt5.disconnect()
        return jsonify(prices)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/log")
def api_log():
    n = request.args.get("n", 20, type=int)
    return jsonify({"log": log_entries[:n]})


@app.route("/api/export/csv")
def export_csv():
    orders = database.get_order_history(5000)
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["time","pair","direction","entry_price","sl_price","tp_price","volume","pnl","result"])
    for o in orders:
        w.writerow([o.get("time",""), o.get("pair",""), o.get("direction",""),
                    o.get("entry_price",""), o.get("sl_price",""), o.get("tp_price",""),
                    o.get("volume",""), o.get("pnl",""), o.get("result","")])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=trades.csv"})


# --- Pages ---

@app.route("/bot")
def dashboard():
    return render_template_string(HTML, state=bot_state, tab="dashboard",
                                  positions=[], history=[], total_pnl=0, count=0,
                                  scan_interval=180, pair_filter="", date_filter="",
                                  auto_mode=bot_state.get("auto_mode", True))


@app.route("/bot/positions")
def positions_page():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        summary = mt5.get_positions_summary()
        positions = summary["positions"]
        for p in positions:
            p["be_triggered"] = _main.be_tracked.get(p["ticket"], {}).get("be_triggered", False)
        mt5.disconnect()
        total_pnl = summary["total_pnl"]; count = summary["count"]
    except Exception:
        positions = []; total_pnl = 0; count = 0
    return render_template_string(HTML, positions=positions, total_pnl=total_pnl,
                                  count=count, tab="positions", state=bot_state,
                                  history=[], pair_filter="", date_filter="")


@app.route("/api/stats/detailed")
def api_stats_detailed():
    import config
    orders = database.get_order_history(5000)
    pairs = list(config.PAIRS.keys())  # Only active pairs

    def calc_pair_stats(pair_name):
        trades = [o for o in orders if o.get("pair") == pair_name]
        closed = [o for o in trades if o.get("result") in ("win", "loss")]
        wins = [o for o in closed if o.get("result") == "win"]
        losses = [o for o in closed if o.get("result") == "loss"]
        bes = [o for o in trades if o.get("result") == "be"]

        n = len(closed)
        w = len(wins)
        l = len(losses)
        wr = w / n * 100 if n > 0 else 0
        total_pnl = sum(o.get("pnl", 0) for o in closed)
        avg_win = sum(o.get("pnl", 0) for o in wins) / w if w > 0 else 0
        avg_loss = sum(o.get("pnl", 0) for o in losses) / l if l > 0 else 0
        gross_profit = sum(o.get("pnl", 0) for o in wins)
        gross_loss = abs(sum(o.get("pnl", 0) for o in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Avg realized RR
        rr_values = []
        for o in closed:
            sl_d = abs(o.get("entry_price", 0) - o.get("sl_price", 0))
            tp_d = abs(o.get("tp_price", 0) - o.get("entry_price", 0))
            if sl_d > 0 and o.get("result") == "win":
                rr_values.append(tp_d / sl_d)

        avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0

        # Max DD (from balance history in trades)
        peak = 0; max_dd = 0
        for o in trades:
            bal = o.get("balance", 0)
            if bal > peak: peak = bal
            dd = (peak - bal) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        # Sharpe (simplified: mean / std of P&L)
        pnls = [o.get("pnl", 0) for o in closed]
        mean_pnl = sum(pnls) / len(pnls) if pnls else 0
        std_pnl = (sum((x - mean_pnl)**2 for x in pnls) / len(pnls))**0.5 if pnls else 0
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0

        # Consecutive losses
        max_consec = 0; cur = 0
        for o in closed:
            if o.get("result") == "loss":
                cur += 1; max_consec = max(max_consec, cur)
            else:
                cur = 0

        return {
            "trades": n, "be": len(bes), "wins": w, "losses": l,
            "win_rate": round(wr, 1), "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            "max_dd_pct": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
            "avg_rr": round(avg_rr, 2),
            "max_consec_loss": max_consec,
            "best": round(max(o.get("pnl", 0) for o in closed), 2) if closed else 0,
            "worst": round(min(o.get("pnl", 0) for o in closed), 2) if closed else 0,
        }

    result = {}
    for p in pairs:
        result[p] = calc_pair_stats(p)
    return jsonify(result)


@app.route("/bot/stats")
def stats_page():
    return render_template_string(HTML, tab="stats", state=bot_state,
                                  positions=[], history=[], total_pnl=0, count=0,
                                  scan_interval=180, pair_filter="", date_filter="")


@app.route("/bot/history")
def history_page():
    pair_filter = request.args.get("pair", "")
    date_filter = request.args.get("date", "")
    hist = database.get_order_history(500)
    # Only show trades with a result (closed)
    hist = [h for h in hist if h.get("result") in ("win", "loss", "be")]
    if pair_filter:
        hist = [h for h in hist if h.get("pair", "") == pair_filter]
    if date_filter:
        hist = [h for h in hist if (h.get("time", "") or "").startswith(date_filter)]
    hist = hist[:100]
    return render_template_string(HTML, history=hist, tab="history", state=bot_state,
                                  pair_filter=pair_filter, date_filter=date_filter)


@app.route("/bot/close/<int:ticket>", methods=["POST"])
def close_one(ticket):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        info = _main.be_tracked.get(ticket, {})
        mt5.connect()
        # Capture live P&L BEFORE closing
        live_pnl = 0
        for p in mt5.get_positions():
            if p["ticket"] == ticket:
                live_pnl = p.get("profit", 0)
                break
        ok = mt5.close_position(ticket)
        if ok:
            # Log closed trade immediately
            try:
                entry_price = info.get("entry_price", 0)
                symbol = info.get("symbol", "")
                # Use live P&L if available, fall back to history
                pnl = live_pnl if live_pnl != 0 else info.get("last_profit", 0)
                if pnl == 0:
                    pnl, exit_price, volume = mt5.get_closed_trade_pnl(
                        ticket, hours=72, symbol=symbol, entry_price=entry_price)
                else:
                    exit_price, volume = 0, 0
                if abs(pnl) > 0.01:
                    result = "win" if pnl > 0 else "loss"
                else:
                    result = "be"
                if info:
                    direction = info.get("direction", "?")
                    pair = _main.symbol_to_pair(symbol) if hasattr(_main, 'symbol_to_pair') else symbol
                    database.save_closed_trade(
                        ticket=ticket, pair=pair, direction=direction,
                        entry_price=entry_price, sl_price=info.get("sl", 0),
                        tp_price=info.get("tp", 0), volume=0,
                        pnl=pnl, result=result, exit_price=0,
                        open_time=str(datetime.now()))
                    _main.be_tracked.pop(ticket, None)
                    database.clear_be_ticket(ticket)
                    database.save_be_state(_main.be_tracked)
                add_log(f"#{ticket} {result.upper()} ${pnl:+.2f}")
            except Exception as e:
                add_log(f"Закрыта позиция #{ticket} (лог: {e})")
        mt5.disconnect()
        if ok:
            if is_ajax: return jsonify({"ok": True, "msg": f"#{ticket} закрыта"})
        else:
            if is_ajax: return jsonify({"ok": False, "msg": f"Не удалось закрыть #{ticket}"})
    except Exception as e:
        if is_ajax: return jsonify({"ok": False, "msg": str(e)})
    return "<script>window.location='/bot/positions'</script>"


@app.route("/bot/be/<int:ticket>", methods=["POST"])
def move_to_be(ticket):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        pos = mt5.get_positions()
        target = None
        for p in pos:
            if p["ticket"] == ticket:
                target = p
                break
        if target:
            entry = target["price_open"]
            current = target.get("price_current", 0)
            ptype = target["type"]  # 0=BUY, 1=SELL
            # Check: can only move SL to BE when position is in profit
            in_profit = (ptype == 0 and current > entry) or (ptype == 1 and current < entry)
            if not in_profit:
                mt5.disconnect()
                if is_ajax: return jsonify({"ok": False, "msg": "BE недоступен: позиция не в плюсе"})
                return "<script>alert('BE недоступен: позиция не в плюсе');window.location='/bot/positions'</script>"
            ok = mt5.modify_sl(ticket, entry)
            if ok:
                _main.be_tracked[ticket] = {"be_triggered": True, "entry_price": entry,
                                              "symbol": target["symbol"],
                                              "direction": "BUY" if target["type"] == 0 else "SELL"}
                add_log(f"#{ticket} SL → BE ({entry})")
                if is_ajax: return jsonify({"ok": True, "msg": f"#{ticket} → BE"})
            else:
                if is_ajax: return jsonify({"ok": False, "msg": f"Не удалось изменить SL #{ticket}"})
        mt5.disconnect()
    except Exception as e:
        if is_ajax: return jsonify({"ok": False, "msg": str(e)})
    return "<script>window.location='/bot/positions'</script>"


@app.route("/bot/close_all", methods=["POST"])
def close_all():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        # Capture be_tracked before closing (so we can log each)
        tracked_before = dict(_main.be_tracked)
        n = mt5.close_all_positions()
        # Log each closed position
        for ticket, info in tracked_before.items():
            try:
                symbol = info.get("symbol", "")
                entry_price = info.get("entry_price", 0)
                pnl, exit_price, volume = mt5.get_closed_trade_pnl(
                    ticket, hours=72, symbol=symbol, entry_price=entry_price)
                if abs(pnl) > 0.01:
                    result = "win" if pnl > 0 else "loss"
                else:
                    result = "be"
                pair = _main.symbol_to_pair(symbol) if hasattr(_main, 'symbol_to_pair') else symbol
                database.save_closed_trade(
                    ticket=ticket, pair=pair, direction=info.get("direction", "?"),
                    entry_price=info.get("entry_price", 0),
                    sl_price=info.get("sl", 0), tp_price=info.get("tp", 0),
                    volume=0, pnl=pnl, result=result, exit_price=0,
                    open_time=str(datetime.now()))
                _main.be_tracked.pop(ticket, None)
                database.clear_be_ticket(ticket)
            except Exception as e:
                print(f"[LOG] Error logging #{ticket}: {e}")
        database.save_be_state(_main.be_tracked)
        mt5.disconnect()
        add_log(f"Закрыто позиций: {n}")
    except Exception as e:
        add_log(f"Ошибка: {e}")
    return "<script>window.location='/bot/positions'</script>"


@app.route("/bot/scan", methods=["POST"])
def trigger_scan():
    """Trigger a manual scan now."""
    try:
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        _main.scan_all(is_manual=True)
        return {"ok": True, "msg": "Скан выполнен — проверьте лог"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.route("/bot/mode", methods=["POST"])
def toggle_mode():
    """Toggle between auto and manual scan mode."""
    bot_state["auto_mode"] = not bot_state.get("auto_mode", True)
    mode = "авто" if bot_state["auto_mode"] else "ручной"
    add_log(f"Режим переключён: <b>{mode}</b>")
    return {"ok": True, "auto_mode": bot_state["auto_mode"], "msg": f"Режим: {mode}"}

@app.route("/bot/accept/<sig_id>", methods=["POST"])
def accept(sig_id):
    return "<script>alert('Авто-режим');window.location='/bot'</script>"

@app.route("/bot/reject/<sig_id>", methods=["POST"])
def reject(sig_id):
    return "<script>window.location='/bot'</script>"


def run_web(port=5002):
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def start_web():
    t = threading.Thread(target=run_web, daemon=True)
    t.start()
    print(f"[Web] Dashboard v3 at http://localhost:5002/bot")
    return t
