#!/usr/bin/env python3
"""Local web server for browsing car listings — mobile-optimized.

Usage:
    python3 car_server.py              # start on port 8080
    python3 car_server.py --port 3000  # custom port
    python3 car_server.py --host 0.0.0.0  # accessible on LAN
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
import config as _config
DB_PATH = _config.DB_PATH

FUEL_MAP = _config.FUEL_MAP
TRANS_MAP = _config.TRANSMISSION_MAP
BODY_MAP  = _config.BODY_MAP


def get_cars(include_inactive: bool = False) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS car_equipment (vehicleid INTEGER PRIMARY KEY, full_text TEXT, acc INTEGER DEFAULT 0, ahk INTEGER DEFAULT 0, rfk INTEGER DEFAULT 0, scraped_at TEXT)")
    where = "" if include_inactive else "WHERE is_active=1"
    rows = conn.execute(f"""
        SELECT c.*, e.acc as _acc, e.ahk as _ahk, e.rfk as _rfk, e.automatic as _automatic,
               e.dealer_address as _dealer_address, e.dealer_phone as _dealer_phone
        FROM cars c
        LEFT JOIN car_equipment e ON c.vehicleid = e.vehicleid
        {where} ORDER BY c.customerprice ASC
    """).fetchall()
    cars = []
    for r in rows:
        car = dict(r)
        for f in ("images", "envkv", "financing", "leasingbusiness", "leasingprivate"):
            try:
                car[f] = json.loads(car[f]) if car[f] else None
            except (json.JSONDecodeError, TypeError):
                car[f] = None
        cars.append(car)
    conn.close()
    return cars


def get_price_history(vehicleid: int) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT recorded_at, customerprice, price, monthlypayment FROM price_history WHERE vehicleid=? ORDER BY recorded_at DESC",
        (vehicleid,),
    ).fetchall()
    history = [dict(r) for r in rows]
    conn.close()
    return history


def get_stats() -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute("""
        SELECT COUNT(*) as total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) as active,
               AVG(CASE WHEN is_active=1 THEN customerprice END) as avg_price,
               MIN(CASE WHEN is_active=1 THEN customerprice END) as min_price,
               MAX(CASE WHEN is_active=1 THEN customerprice END) as max_price
        FROM cars
    """).fetchone()
    last = conn.execute("SELECT MAX(last_seen) as updated FROM cars WHERE is_active=1").fetchone()
    conn.close()
    return {
        "total": r["total"], "active": r["active"], "gone": r["total"] - r["active"],
        "avg_price": round(r["avg_price"] or 0),
        "min_price": round(r["min_price"] or 0), "max_price": round(r["max_price"] or 0),
        "updated": last["updated"] or "",
    }


def get_notes(vehicleid: int) -> str:
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT note FROM car_notes WHERE vehicleid=?", (vehicleid,)).fetchone()
    conn.close()
    return row[0] if row else ""


def save_note(vehicleid: int, note: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS car_notes (vehicleid INTEGER PRIMARY KEY, note TEXT)"
    )
    conn.execute(
        "INSERT INTO car_notes (vehicleid, note) VALUES (?, ?) ON CONFLICT(vehicleid) DO UPDATE SET note=excluded.note",
        (vehicleid, note),
    )
    conn.commit()
    conn.close()


def get_flags(vehicleid: int) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("CREATE TABLE IF NOT EXISTS car_flags (vehicleid INTEGER PRIMARY KEY, called INTEGER DEFAULT 0, ignored INTEGER DEFAULT 0)")
    row = conn.execute("SELECT called, ignored FROM car_flags WHERE vehicleid=?", (vehicleid,)).fetchone()
    conn.close()
    return {"called": bool(row[0]) if row else False, "ignored": bool(row[1]) if row else False}


def set_flag(vehicleid: int, flag: str, value: bool) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("CREATE TABLE IF NOT EXISTS car_flags (vehicleid INTEGER PRIMARY KEY, called INTEGER DEFAULT 0, ignored INTEGER DEFAULT 0)")
    conn.execute(
        f"INSERT INTO car_flags (vehicleid, {flag}) VALUES (?, ?) ON CONFLICT(vehicleid) DO UPDATE SET {flag}=excluded.{flag}",
        (vehicleid, int(value)),
    )
    conn.commit()
    conn.close()


# --- HTML -------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>E-Fahrzeuge &lt;31k — Berlin</title>
<style>
:root {
  --bg: #f5f5f7; --card: #fff; --text: #1d1d1f; --text2: #6e6e73;
  --accent: #0071e3; --green: #248a3d; --red: #d32f2f; --border: #e5e5ea;
  --shadow: 0 2px 8px rgba(0,0,0,.08); --radius: 14px;
  --safe-b: env(safe-area-inset-bottom,0px);
}
@media (prefers-color-scheme:dark) {
  :root { --bg:#1c1c1e; --card:#2c2c2e; --text:#f5f5f7; --text2:#98989d; --accent:#0a84ff; --green:#30d158; --border:#38383a; --shadow:0 2px 8px rgba(0,0,0,.3); }
}
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:var(--bg); color:var(--text); -webkit-tap-highlight-color:transparent; overscroll-behavior:none; padding-bottom:calc(64px + var(--safe-b)); }
header { position:sticky; top:0; z-index:100; background:var(--bg); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border-bottom:1px solid var(--border); padding:12px 16px; display:flex; align-items:center; gap:12px; }
header h1 { font-size:17px; font-weight:600; flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
header button { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:8px 14px; font-size:14px; color:var(--text); cursor:pointer; white-space:nowrap; touch-action:manipulation; min-height:44px; display:flex; align-items:center; gap:6px; }
header button:active { opacity:.6; }
.filter-panel { display:none; position:sticky; top:58px; z-index:99; background:var(--bg); border-bottom:1px solid var(--border); padding:12px 16px; }
.filter-panel.open { display:block; }
.filter-panel label { display:inline-flex; align-items:center; gap:6px; padding:8px 14px; margin:3px; background:var(--card); border:1px solid var(--border); border-radius:20px; font-size:13px; cursor:pointer; touch-action:manipulation; min-height:44px; }
.filter-panel label:has(input:checked), #ignoredFilterLabel:has(input:checked) { background:var(--accent); color:#fff; border-color:var(--accent); }
.filter-panel input[type=checkbox] { display:none; }
.sort-row { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
.sort-row select { flex:1; min-width:120px; background:var(--card); border:1px solid var(--border); border-radius:10px; padding:10px 12px; font-size:14px; color:var(--text); -webkit-appearance:none; appearance:none; background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12'%3E%3Cpath d='M2 4l4 4 4-4' stroke='%236e6e73' fill='none'/%3E%3C/svg%3E"); background-repeat:no-repeat; background-position:right 12px center; }
.grid { padding:8px; display:grid; grid-template-columns:1fr; gap:8px; }
@media (min-width:600px) { .grid { grid-template-columns:repeat(2,1fr); } }
@media (min-width:960px) { .grid { grid-template-columns:repeat(3,1fr); } }
.card { background:var(--card); border-radius:var(--radius); box-shadow:var(--shadow); overflow:hidden; cursor:pointer; transition:transform .15s; touch-action:manipulation; }
.card:active { transform:scale(.98); }
.card-img { width:100%; aspect-ratio:4/3; object-fit:cover; background:var(--border); display:block; }
.card-body { padding:12px 14px; }
.card-price { font-size:20px; font-weight:700; color:var(--accent); }
.card-title { font-size:13px; color:var(--text); margin:4px 0 8px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.card-meta { display:flex; flex-wrap:wrap; gap:4px 10px; font-size:12px; color:var(--text2); }
.card-meta span { display:flex; align-items:center; gap:3px; }
.badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
.badge-new { background:var(--green); color:#fff; }
.btn-called { display:inline-flex; align-items:center; gap:4px; padding:8px 16px; border-radius:20px; border:2px solid var(--green); background:transparent; color:var(--green); font:inherit; font-size:13px; font-weight:600; cursor:pointer; touch-action:manipulation; min-height:44px; }
.btn-called.active { background:var(--green); color:#fff; }
.feat-badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; margin:2px; }
.feat-yes { background:#e8f5e9; color:var(--green); }
.feat-no { background:#f5f5f5; color:var(--text2); }
@media (prefers-color-scheme:dark) { .feat-yes { background:#1b3a1b; } .feat-no { background:#2c2c2e; } }
.card.expanded .card-detail { display:block; }
.card-detail { display:none; padding:0 14px 14px; border-top:1px solid var(--border); margin-top:10px; padding-top:10px; font-size:13px; color:var(--text2); }
.card-detail .row { display:flex; justify-content:space-between; padding:4px 0; }
.card-detail .row strong { color:var(--text); }
.card-gallery { display:flex; gap:6px; overflow-x:auto; padding:4px 0 8px; -webkit-overflow-scrolling:touch; scrollbar-width:none; }
.card-gallery::-webkit-scrollbar { display:none; }
.card-gallery img { width:160px; height:120px; object-fit:cover; border-radius:8px; flex-shrink:0; cursor:pointer; }
.lightbox { display:none; position:fixed; inset:0; z-index:9999; background:rgba(0,0,0,.92); align-items:center; justify-content:center; }
.lightbox.open { display:flex; }
.lightbox img { max-width:95vw; max-height:85vh; object-fit:contain; border-radius:4px; transform-origin:center; touch-action:none; user-select:none; -webkit-user-drag:none; }
.lightbox .close { position:fixed; top:16px; right:16px; color:#fff; font-size:28px; cursor:pointer; width:44px; height:44px; display:flex; align-items:center; justify-content:center; background:rgba(255,255,255,.15); border-radius:50%; }
.lightbox .back { position:fixed; top:16px; left:16px; color:#fff; font-size:26px; cursor:pointer; width:44px; height:44px; display:flex; align-items:center; justify-content:center; background:rgba(255,255,255,.15); border-radius:50%; }
.lightbox .nav { position:absolute; top:50%; transform:translateY(-50%); color:#fff; font-size:36px; cursor:pointer; width:48px; height:48px; display:flex; align-items:center; justify-content:center; background:rgba(255,255,255,.15); border-radius:50%; user-select:none; }
.lightbox .next { right:8px; }
.price-chart { position:relative; height:40px; margin:8px 0; }
.price-chart canvas { width:100%; height:100%; }
.loading { text-align:center; padding:60px 20px; color:var(--text2); font-size:15px; }
.empty { text-align:center; padding:60px 20px; color:var(--text2); font-size:15px; }
.bottom-bar { position:fixed; bottom:0; left:0; right:0; z-index:100; background:var(--bg); border-top:1px solid var(--border); padding:calc(8px + var(--safe-b)) 16px 8px; padding-bottom:calc(8px + var(--safe-b)); display:flex; justify-content:space-around; font-size:12px; color:var(--text2); }
.bottom-bar strong { display:block; font-size:15px; color:var(--text); }
::-webkit-scrollbar { width:0; }
</style>
</head>
<body>
<header>
  <h1>🚗 Octavia, Superb &amp; Enyaq &lt;30k€ · Berlin (<span id="carCount">…</span>)</h1>
  <button id="filterBtn" onclick="toggleFilters()">🔍 Filter</button>
  <button onclick="toggleGone()" id="goneBtn">📦 Alle</button>
  <button onclick="location.reload()">🔄</button>
</header>
<div class="filter-panel" id="filterPanel">
  <div id="fuelFilters"></div>
  <div id="transFilters" style="margin-top:8px"></div>
  <div class="sort-row">
    <label style="display:inline-flex;align-items:center;gap:6px;padding:8px 14px;margin:3px;background:var(--card);border:1px solid var(--border);border-radius:20px;font-size:13px;cursor:pointer;min-height:44px" id="ignoredFilterLabel">
      <input type="checkbox" id="ignoredFilter" onchange="hideIgnored=this.checked;renderCars()" checked> Hide ignored
    </label>
    <select id="sortSelect" onchange="renderCars()">
      <option value="newest" selected>Neueste Inserate</option>
      <option value="price_asc">Preis ↑</option>
      <option value="price_desc">Preis ↓</option>
      <option value="km_asc">Km ↑</option>
      <option value="km_desc">Km ↓</option>
      <option value="date_desc">Neueste Erstzulassung</option>
      <option value="date_asc">Älteste Erstzulassung</option>
      <option value="power_desc">PS ↑</option>
    </select>
  </div>
</div>
<div class="grid" id="grid"><div class="loading">Lade Fahrzeuge…</div></div>
<div class="bottom-bar" id="bottomBar"></div>
<div class="lightbox" id="lightbox" onclick="closeLightbox()">
  <div class="close" onclick="closeLightbox()">✕</div>
  <div class="back" onclick="event.stopPropagation();navLightbox(-1)">←</div>
  <img id="lightboxImg" src="" onclick="event.stopPropagation()" ondblclick="toggleZoom(event)" onload="cacheCenter()">
  <div class="nav next" onclick="event.stopPropagation();navLightbox(1)">›</div>
</div>

<script>
let allCars=[],carFlags={};
let showGone=false, hideIgnored=true;
const dealerNames={545:'Spandau',546:'Charlottenburg',558:'Tempelhof',21824:'Zehlendorf',22617:'Potsdam',22631:'Marzahn',22673:'Audi Berlin',22676:'Audi Berlin',25894:'Weißensee'};
const dealerPhones={546:'03089081255',545:'03089081100',558:'03089081200',22617:'03316487333',25894:'0309627620'};
function dealerName(id){return dealerNames[id]||'Händler '+id;}
function dealerPhone(id){return dealerPhones[id]||'';}
function getFeatureText(c,feat){
  if(c._acc!==null&&c._acc!==undefined){
    if(feat==='AHK')return c._ahk?'Anhängerkupplung':'';
    if(feat==='ACC')return c._acc?'ACC':'';
    if(feat==='RFK')return c._rfk?'Rückfahrkamera':'';
    if(feat==='Automatik')return (c.transmission_name||'').toLowerCase().includes('automatik')?'Automatik':'';
  }
  const d=(c.shortdescription||'');
  if(feat==='AHK')return /AHK|AHZV|Anhänger/i.test(d)?'Anhängerkupplung':'';
  if(feat==='ACC')return /ACC|Abstand|Adaptive/i.test(d)?'ACC':'';
  if(feat==='RFK')return /RFK|Kamera|RearView|AreaView|360°/i.test(d)?'Rückfahrkamera':'';
  if(feat==='Automatik')return /DSG|Automatik|Automatic|S.tronic/i.test(d)?'Automatik':'';
  return '';
}
function renderFeatureBadges(c){
  const feats={ACC:'ACC',Anhängerkupplung:'AHK',Rückfahrkamera:'RFK',Automatik:'Automatik'};
  let h='';
  for(const [label,key] of Object.entries(feats)){
    const t=getFeatureText(c,key);
    h+=`<span class=\"feat-badge ${t?'feat-yes':'feat-no'}\">${t||label}</span>`;
  }
  return h;
}
function renderFeatures(c){
  return renderFeatureBadges(c);
}
async function toggleCalled(vid){
  const btn=document.getElementById('calledBtn-'+vid);
  const isActive=btn.classList.contains('active');
  btn.classList.toggle('active');
  btn.textContent=(isActive?'📞':'📞 ✓');
  await fetch('/api/flags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vehicleid:vid,flag:'called',value:!isActive})});
}
function copyPhone(did){
  const num=dealerPhones[did]||'Keine Nummer';
  if(num==='Keine Nummer'){alert('Keine Telefonnummer hinterlegt');return;}
  const ta=document.createElement('textarea');
  ta.value=num;ta.style.position='fixed';ta.style.left='-9999px';
  document.body.appendChild(ta);
  ta.select();document.execCommand('copy');
  document.body.removeChild(ta);
}
async function copyOffer(vid,btn){
  try{
    const r=await fetch('/api/equipment?vehicleid='+vid);
    const eq=await r.json();
    const text=eq.text||'';
    const ta=document.createElement('textarea');
    ta.value=text;ta.style.position='fixed';ta.style.left='-9999px';
    document.body.appendChild(ta);
    ta.select();document.execCommand('copy');
    document.body.removeChild(ta);
    if(btn){const orig=btn.textContent;btn.textContent='✓';setTimeout(()=>btn.textContent=orig,1000);}
  }catch(e){}
}
async function toggleIgnored(vid){
  const btn=document.getElementById('ignoredBtn-'+vid);
  const isActive=btn.classList.contains('active');
  btn.classList.toggle('active');
  btn.textContent=(isActive?'🙈':'🙈 ✓');
  carFlags[vid]={...carFlags[vid],ignored:!isActive};
  if(hideIgnored) renderCars();
  await fetch('/api/flags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vehicleid:vid,flag:'ignored',value:!isActive})});
}
async function saveNote(vid,text){
  await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vehicleid:vid,note:text})});
}
const imgCache={};
let lbImgs=[],lbIdx=0;
let zoomScale=1,zoomTx=0,zoomTy=0;
const lbImg=()=>document.getElementById('lightboxImg');
function applyZoom(noAnim){
  const el=lbImg();
  if(noAnim){el.style.transition='none';}
  else{el.style.transition='transform .15s ease';}
  el.style.transform=`translate(${zoomTx}px,${zoomTy}px) scale(${zoomScale})`;
}
function resetZoom(){
  zoomScale=1;zoomTx=0;zoomTy=0;
  const el=lbImg();el.style.transition='none';
  el.style.transform=`translate(0px,0px) scale(1)`;
  cacheCenter();
  requestAnimationFrame(()=>{el.style.transition='transform .15s ease';});
}
let lbCx=0,lbCy=0;
function cacheCenter(){
  const rect=lbImg().getBoundingClientRect();
  lbCx=rect.left+rect.width/2;
  lbCy=rect.top+rect.height/2;
}
function zoomAt(px,py,s1){
  const el=lbImg();
  const s0=zoomScale,T0x=zoomTx,T0y=zoomTy;
  const k=s1/s0;
  zoomTx=(1-k)*(px-lbCx)+k*T0x;
  zoomTy=(1-k)*(py-lbCy)+k*T0y;
  zoomScale=s1;
  el.style.transition='none';
  el.style.transform=`translate(${zoomTx}px,${zoomTy}px) scale(${zoomScale})`;
}
function toggleZoom(ev){
  if(zoomScale>1){resetZoom();return;}
  zoomAt(ev.clientX,ev.clientY,2.5);
}
let pinchStartDist=0,pinchStartScale=1,panStartX=0,panStartY=0,panTx0=0,panTy0=0;
function touchStart(e){
  if(e.target!==lbImg())return;
  const t=e.touches;
  if(t.length===2){
    pinchStartDist=Math.hypot(t[0].clientX-t[1].clientX,t[0].clientY-t[1].clientY);
    pinchStartScale=zoomScale;
  }else if(t.length===1){
    panStartX=t[0].clientX;panStartY=t[0].clientY;
    panTx0=zoomTx;panTy0=zoomTy;
  }
}
function touchMove(e){
  if(e.target!==lbImg())return;
  e.preventDefault();
  const t=e.touches;
  if(t.length===2){
    const d=Math.hypot(t[0].clientX-t[1].clientX,t[0].clientY-t[1].clientY);
    if(pinchStartDist>0){
      const ns=Math.min(6,Math.max(1,pinchStartScale*(d/pinchStartDist)));
      const mx=(t[0].clientX+t[1].clientX)/2;
      const my=(t[0].clientY+t[1].clientY)/2;
      zoomAt(mx,my,ns);
    }
  }else if(t.length===1&&zoomScale>1){
    zoomTx=panTx0+(t[0].clientX-panStartX);
    zoomTy=panTy0+(t[0].clientY-panStartY);
    applyZoom(true);
  }
}
function touchEnd(e){
  if(e.target!==lbImg())return;
  if(e.touches.length===1&&zoomScale>1){
    const t=e.touches[0];
    panStartX=t.clientX;panStartY=t.clientY;
    panTx0=zoomTx;panTy0=zoomTy;
    return;
  }
  if(zoomScale<=1){resetZoom();return;}
}
function openLightbox(vid,idx){
  lbImgs=imgCache[vid]||[];
  lbIdx=idx;
  document.getElementById('lightboxImg').src=lbImgs[idx];
  document.getElementById('lightbox').classList.add('open');
  document.body.style.overflow='hidden';
  resetZoom();
}
function closeLightbox(){
  document.getElementById('lightbox').classList.remove('open');
  document.body.style.overflow='';
  resetZoom();
}
function navLightbox(dir){
  lbIdx=(lbIdx+dir+lbImgs.length)%lbImgs.length;
  document.getElementById('lightboxImg').src=lbImgs[lbIdx];
  resetZoom();
}
const lbImgEl=lbImg();
if(lbImgEl){
  lbImgEl.addEventListener('touchstart',touchStart,{passive:false});
  lbImgEl.addEventListener('touchmove',touchMove,{passive:false});
  lbImgEl.addEventListener('touchend',touchEnd,{passive:false});
}
async function toggleGone(){
  showGone=!showGone;
  document.getElementById('goneBtn').textContent=showGone?'📦 Aktiv':'📦 Alle';
  await load();
}
async function load(){
  const r=await fetch(showGone?'/api/cars?all=1':'/api/cars');
  allCars=await r.json();
  document.getElementById('carCount').textContent=allCars.length;
  try{const fr=await fetch('/api/flags/all');carFlags=await fr.json();}catch(e){carFlags={};}
  buildFilters();
  renderCars();
  for(const [vid,flags] of Object.entries(carFlags)){
    if(flags.called){
      const btn=document.getElementById('calledBtn-'+vid);
      if(btn){btn.classList.add('active');btn.textContent='📞 ✓';}
    }
    if(flags.ignored){
      const btn=document.getElementById('ignoredBtn-'+vid);
      if(btn){btn.classList.add('active');btn.textContent='🙈 ✓ Ignoriert';}
    }
  }
  const s=await fetch('/api/stats');
  const stats=await s.json();
  const goneText=stats.gone>0?` · ${stats.gone} verkauft`:'';
  document.getElementById('bottomBar').innerHTML=
    `<div><strong>${stats.active}</strong> aktiv${goneText}</div>
     <div><strong>${(stats.avg_price).toLocaleString('de')}€</strong> Durchschnitt</div>
     <div><strong>${(stats.min_price).toLocaleString('de')}€</strong> ab</div>`;
}
function buildFilters(){
  const fuels=[...new Set(allCars.map(c=>c.fuel_name||'Unbekannt'))];
  let h='';
  fuels.forEach(f=>{h+=`<label><input type=checkbox data-type=fuel value="${f}" onchange="renderCars()" checked> ${f}</label>`;});
  document.getElementById('fuelFilters').innerHTML=h;
  const trans=[...new Set(allCars.map(c=>c.transmission_name||'Unbekannt'))];
  h='';
  trans.forEach(t=>{h+=`<label><input type=checkbox data-type=trans value="${t}" onchange="renderCars()" checked> ${t}</label>`;});
  document.getElementById('transFilters').innerHTML=h;
}
function toggleFilters(){
  document.getElementById('filterPanel').classList.toggle('open');
}
function renderCars(){
  let cars=[...allCars];
  const selFuels=[...document.querySelectorAll('#fuelFilters input:checked')].map(e=>e.value);
  const selTrans=[...document.querySelectorAll('#transFilters input:checked')].map(e=>e.value);
  cars=cars.filter(c=>selFuels.includes(c.fuel_name||'Unbekannt') && selTrans.includes(c.transmission_name||'Unbekannt'));
  const sort=document.getElementById('sortSelect').value;
  const cmps={
    price_asc:(a,b)=>a.customerprice-b.customerprice,
    price_desc:(a,b)=>b.customerprice-a.customerprice,
    km_asc:(a,b)=>a.kilometers-b.kilometers,
    km_desc:(a,b)=>b.kilometers-a.kilometers,
    date_desc:(a,b)=>(b.registrationdate||'').localeCompare(a.registrationdate||''),
    date_asc:(a,b)=>(a.registrationdate||'').localeCompare(b.registrationdate||''),
    power_desc:(a,b)=>(b.power||0)-(a.power||0),
    newest:(a,b)=>(b.first_seen||'').localeCompare(a.first_seen||''),
  };
  cars.sort(cmps[sort]||cmps.newest);
  if(hideIgnored) cars=cars.filter(c=>!carFlags[c.vehicleid]?.ignored);
  const grid=document.getElementById('grid');
  if(!cars.length){grid.innerHTML='<div class="empty">Keine Fahrzeuge gefunden.</div>';return;}
  let html='';
  cars.forEach(c=>{html+=renderCard(c);});
  grid.innerHTML=html;
}
function renderCard(c){
    const imgs=c.images||[];
    const thumb=imgs.length?imgs[0].s:'';
    const km=(c.kilometers||0).toLocaleString('de');
    const price=(c.customerprice||0).toLocaleString('de',{minimumFractionDigits:0,maximumFractionDigits:0});
    const power_kw=c.power||0;
    const power_ps=Math.round(power_kw*1.36);
    const reg=(c.registrationdate||'').slice(0,7);
    const isNew=Date.now()-new Date(c.first_seen).getTime()<14400000;
    const isGone=!c.is_active;
    const badges=[];
    if(isNew)badges.push('<span class="badge badge-new">NEU</span>');
    if(isGone)badges.push('<span class="badge" style="background:var(--red);color:#fff">VERKAUFT</span>');
    const badge=badges.length?' '+badges.join(' '):'';
    const fuel=c.fuel_name||'?';
    const trans=c.transmission_name||'?';
    const body=c.body_name||'?';
    const goneStyle=isGone?'opacity:.55;filter:grayscale(30%)':'';
    imgCache[c.vehicleid]=imgs.map(x=>x.l);
    const firstSeen=new Date(c.first_seen+'Z').toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
    return `<div class="card" onclick="toggleCard(this,${c.vehicleid},event)" style="${goneStyle}">
      ${thumb?`<img class="card-img" src="${thumb}" loading="lazy" alt="">`:`<div class="card-img" style="display:flex;align-items:center;justify-content:center;font-size:32px;color:var(--text2)">🚗</div>`}
      <div class="card-body">
        <div class="card-price">${price}€${badge}</div>
        <div class="card-title">${c.shortdescription||'?'}</div>
        <div style="margin-bottom:4px">${renderFeatureBadges(c)}</div>
        <div class="card-meta">
          <span>📏 ${km} km</span>
          <span>📅 ${reg}</span>
          <span>⛽ ${fuel}</span>
          <span>${power_kw}kW (${power_ps}PS)</span>
          <span>⚙ ${trans}</span>
          <span style="font-size:11px;color:var(--text2)">🕐 ${firstSeen}</span>
        </div>
        <div style="font-size:11px;color:var(--text2);margin-top:2px">${dealerName(c.dealerid)}${c._dealer_address?` · ${c._dealer_address}`:''}</div>
        <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
          <button class="btn-called" id="calledBtn-${c.vehicleid}" onclick="event.stopPropagation();toggleCalled(${c.vehicleid})" title="Angerufen">📞</button>
          <button class="btn-called" id="ignoredBtn-${c.vehicleid}" onclick="event.stopPropagation();toggleIgnored(${c.vehicleid})" title="Ignoriert">🙈</button>
          <a href="https://www.volkswagen-automobile-berlin.de/gebrauchtwagen/fahrzeugsuche/${c.vehicleid}" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;border:2px solid var(--accent);background:transparent;color:var(--accent);text-decoration:none;font-size:18px" onclick="event.stopPropagation()" title="Zum Angebot">🔗</a>
          <button style="display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;border:2px solid #98989d;background:transparent;color:#98989d;font-size:16px;cursor:pointer" onclick="event.stopPropagation();copyOffer(${c.vehicleid},this)" title="Angebot kopieren">📋</button>
          ${dealerPhone(c.dealerid)?`<a href=\"tel:${dealerPhone(c.dealerid)}\" style=\"display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;border:2px solid var(--green);background:transparent;color:var(--green);text-decoration:none;font-size:18px\" onclick=\"event.stopPropagation()\" title=\"Anrufen: ${dealerPhone(c.dealerid)}\">📞</a>`:`<button style=\"display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;border:2px solid #98989d;background:transparent;color:#98989d;font-size:14px;cursor:pointer\" onclick=\"event.stopPropagation();copyPhone(${c.dealerid})\" title=\"Nummer kopieren\">📞</button>`}
        </div>
      </div>
      <div class="card-detail" id="detail-${c.vehicleid}">
        <div class="row"><span>Listenpreis</span><strong>${(c.listprice||0).toLocaleString('de')}€</strong></div>
        <div class="row"><span>Monatsrate</span><strong>${(c.monthlypayment||0).toLocaleString('de')}€</strong></div>
        <div class="row"><span>Vorbesitzer</span><strong>${c.numowners||'?'}</strong></div>
        <div class="row"><span>Sitze</span><strong>${c.seatcover_name||'?'}</strong></div>
        <div class="row"><span>Erstmals gesehen</span><strong>${new Date(c.first_seen+'Z').toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})}</strong></div>
        <div class="row"><span>Zuletzt gesehen</span><strong>${new Date(c.last_seen+'Z').toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})}</strong></div>
        ${isGone?`<div class="row" style="color:var(--red)"><span>Status</span><strong>VERKAUFT / NICHT MEHR VERFÜGBAR</strong></div>`:''}
        <div style="margin-top:8px">
          <textarea placeholder="Notizen…" style="width:100%;min-height:60px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px;font:inherit;font-size:13px;color:var(--text);resize:vertical" id="note-${c.vehicleid}" onclick="event.stopPropagation()" onblur="saveNote(${c.vehicleid},this.value)"></textarea>
        </div>
        ${imgs.length>1?`<div class="card-gallery">${imgs.map((img,i)=>`<img src="${img.s}" loading="lazy" onclick="event.stopPropagation();openLightbox(${c.vehicleid},${i})">`).join('')}</div>`:''}
        <div class="price-chart" id="chart-${c.vehicleid}"></div>
      </div>
    </div>`;
}
async function toggleCard(card,vid,event){
  if(!event)return;
  let el=event.target;
  if(el.nodeType===3)el=el.parentElement;
  if(el.closest('textarea'))return;
  if(el.closest('button')||el.closest('a'))return;
  card.classList.toggle('expanded');
  if(card.classList.contains('expanded')){
    // Load note
    try{
      const nr=await fetch('/api/notes?vehicleid='+vid);
      const nd=await nr.json();
      const ta=document.getElementById('note-'+vid);
      if(ta&&!ta.dataset.loaded){ta.value=nd.note||'';ta.dataset.loaded='1';}
    }catch(e){}
    // Load flags (called)
    try{
      const fr=await fetch('/api/flags?vehicleid='+vid);
      const fd=await fr.json();
      const btn=document.getElementById('calledBtn-'+vid);
      if(btn&&fd.called){btn.classList.add('active');btn.textContent='📞 ✓ Angerufen';}
      const ibtn=document.getElementById('ignoredBtn-'+vid);
      if(ibtn&&fd.ignored){ibtn.classList.add('active');ibtn.textContent='🙈 ✓';}
    }catch(e){}
    // Load price chart
    const chartDiv=document.getElementById('chart-'+vid);
    if(chartDiv&&!chartDiv.dataset.loaded){
      chartDiv.dataset.loaded='1';
      try{
        const r=await fetch('/api/price-history?vehicleid='+vid);
        const hist=await r.json();
        if(hist.length>1){
          const canvas=document.createElement('canvas');
          canvas.style.width='100%';canvas.style.height='80px';
          const dpr=window.devicePixelRatio||1;
          canvas.width=400*dpr;canvas.height=80*dpr;
          chartDiv.appendChild(canvas);
          const ctx=canvas.getContext('2d');
          ctx.scale(dpr,dpr);
          const accent=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()||'#0071e3';
          const prices=hist.map(h=>h.customerprice).reverse();
          const min=Math.min(...prices),max=Math.max(...prices);
          const range=max-min||1;
          const w=400,h=80,pad=10;
          ctx.strokeStyle=accent;ctx.fillStyle=accent;
          ctx.lineWidth=2;
          ctx.beginPath();
          prices.forEach((p,i)=>{
            const x=pad+(w-2*pad)*i/Math.max(prices.length-1,1);
            const y=pad+(h-2*pad)*(1-(p-min)/range);
            if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
          });
          ctx.stroke();
          prices.forEach((p,i)=>{
            const x=pad+(w-2*pad)*i/Math.max(prices.length-1,1);
            const y=pad+(h-2*pad)*(1-(p-min)/range);
            ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);ctx.fill();
          });
          chartDiv.insertAdjacentHTML('beforeend',
            `<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text2);margin-top:4px">
              <span>${min.toLocaleString('de')}€</span><span>Preisverlauf (${hist.length} Änderungen)</span><span>${max.toLocaleString('de')}€</span></div>`);
        }
      }catch(e){}
    }
  }
}
load();
</script>
</body>
</html>"""

# --- API handlers -------------------------------------------------

def handle_api(path: str) -> tuple[int, str, str]:
    """Returns (status, content_type, body)."""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(path).query)
    parsed = urlparse(path)

    if parsed.path == "/api/cars":
        include_inactive = qs.get("all", ["0"])[0] == "1"
        cars = get_cars(include_inactive=include_inactive)
        return 200, "application/json", json.dumps(cars, ensure_ascii=False, default=str)
    elif parsed.path == "/api/stats":
        return 200, "application/json", json.dumps(get_stats(), ensure_ascii=False)
    elif parsed.path.startswith("/api/price-history"):
        vid = int(qs.get("vehicleid", [0])[0])
        history = get_price_history(vid)
        return 200, "application/json", json.dumps(history, ensure_ascii=False)
    elif parsed.path == "/api/equipment" and "vehicleid" in qs:
        vid = int(qs.get("vehicleid", [0])[0])
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""CREATE TABLE IF NOT EXISTS car_equipment (
            vehicleid INTEGER PRIMARY KEY, full_text TEXT, summary TEXT,
            acc INTEGER DEFAULT 0, ahk INTEGER DEFAULT 0, rfk INTEGER DEFAULT 0,
            automatic INTEGER DEFAULT 0, scraped_at TEXT)""")
        try: conn.execute("ALTER TABLE car_equipment ADD COLUMN summary TEXT")
        except: pass
        try: conn.execute("ALTER TABLE car_equipment ADD COLUMN automatic INTEGER DEFAULT 0")
        except: pass
        row = conn.execute("SELECT summary, full_text FROM car_equipment WHERE vehicleid=?", (vid,)).fetchone()
        conn.close()
        text = (row[0] if row and row[0] else (row[1] if row else None)) or ""
        return 200, "application/json", json.dumps({"vehicleid": vid, "text": text})
    elif parsed.path == "/api/notes" and "vehicleid" in qs:
        vid = int(qs.get("vehicleid", [0])[0])
        note = get_notes(vid)
        return 200, "application/json", json.dumps({"vehicleid": vid, "note": note})
    elif parsed.path == "/api/flags/all":
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS car_flags (vehicleid INTEGER PRIMARY KEY, called INTEGER DEFAULT 0, ignored INTEGER DEFAULT 0)")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM car_flags").fetchall()
        conn.close()
        flags = {str(r["vehicleid"]): {"called": bool(r["called"]), "ignored": bool(r["ignored"])} for r in rows}
        return 200, "application/json", json.dumps(flags)
    elif parsed.path == "/api/flags" and "vehicleid" in qs:
        vid = int(qs.get("vehicleid", [0])[0])
        flags = get_flags(vid)
        return 200, "application/json", json.dumps({"vehicleid": vid, **flags})
    else:
        return 404, "text/plain", "not found"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silent

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        if parsed.path == "/api/notes":
            vid = data.get("vehicleid")
            note = data.get("note", "")
            if vid is not None:
                save_note(vid, note)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return

        if parsed.path == "/api/flags":
            vid = data.get("vehicleid")
            flag = data.get("flag")
            value = data.get("value", False)
            if vid is not None and flag:
                set_flag(vid, flag, value)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return

        self.send_response(400)
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/"):
            status, ctype, body = handle_api(self.path)
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(HTML.encode())

    def do_HEAD(self):
        self.do_GET()


def main():
    parser = argparse.ArgumentParser(description="Car listing web server")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default: 127.0.0.1)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}. Run tracker.py first.")
        exit(1)

    server = HTTPServer((args.host, args.port), Handler)
    print(f"🚗 E-Fahrzeuge Browser — http://{args.host}:{args.port}")
    print(f"   Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
