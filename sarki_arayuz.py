#!/usr/bin/env python3
"""
MiniMax H3 LipSync - Toplu Render Arayüzü (ComfyUI arka planda çalışır)

Kurulum:   pip install flask requests
Çalıştır:  python sarki_arayuz.py
Tarayıcı:  http://127.0.0.1:7860

ComfyUI açık olmalı (varsayılan http://127.0.0.1:8188).
Workflow: ComfyUI'de "Save (API Format)" ile kaydedilmiş JSON.
"""
import copy
import datetime
import json
import math
import random
import re
import unicodedata
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
# exe olarak paketlenince çıktılar exe'nin yanına kaydedilir
BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OUT = BASE / "outputs"
OUT.mkdir(exist_ok=True)

STATE = {"running": False, "cancel": False, "items": [], "log": [], "host": ""}
LOCK = threading.Lock()


class Cancelled(Exception):
    pass


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    with LOCK:
        STATE["log"].append(line)
        STATE["log"] = STATE["log"][-200:]


# ---------------------------------------------------------------- ComfyUI
def upload_file(host, name, data):
    r = requests.post(
        f"{host}/upload/image",
        files={"image": (name, data)},
        data={"overwrite": "true"},
        timeout=120,
    )
    r.raise_for_status()
    j = r.json()
    sub = j.get("subfolder", "")
    return f"{sub}/{j['name']}" if sub else j["name"]


def wait_history(host, pid, item):
    t0 = time.time()
    while True:
        if STATE["cancel"]:
            raise Cancelled()
        r = requests.get(f"{host}/history/{pid}", timeout=30).json()
        if pid in r:
            h = r[pid]
            st = h.get("status", {})
            if st.get("status_str") == "error":
                msg = "ComfyUI hata verdi"
                for ev, d in st.get("messages", []):
                    if ev == "execution_error":
                        msg = f"{d.get('node_type')}: {d.get('exception_message')}"
                raise RuntimeError(msg)
            if st.get("completed") or h.get("outputs"):
                return h
        item["elapsed"] = int(time.time() - t0)
        time.sleep(1.5)


def find_output(history, out_node):
    outs = history.get("outputs", {})
    order = [str(out_node)] + [k for k in outs if k != str(out_node)]
    for nid in order:
        for key, val in outs.get(nid, {}).items():
            if isinstance(val, list):
                for f in val:
                    if isinstance(f, dict) and f.get("filename"):
                        return f
    return None


def inject_lyrics(prompt, placeholder, lyrics):
    """Sözleri prompt'a yerleştirir. (yeni_prompt, yöntem) döner; olmazsa (None, None)."""
    prompt = unicodedata.normalize("NFC", str(prompt))
    ph = unicodedata.normalize("NFC", placeholder)
    if ph and ph in prompt:
        return prompt.replace(ph, lyrics), "yer tutucu"
    # yer tutucu yoksa (örn. workflow'a daha önce söz yazılıp kaydedilmişse):
    # "...in order, are: "<söz>". Use this written text" kalıbındaki sözü değiştir
    pat = re.compile(r'(in order, are:\s*")(.*?)("\s*\.?\s*Use this written text)', re.S)
    if pat.search(prompt):
        return pat.sub(lambda m: m.group(1) + lyrics + m.group(3), prompt, count=1), "kalıp"
    return None, None


def calc_size(w, h, mp, mult=32):
    """Fotoğraf oranını koruyarak ~mp megapiksel, 32'nin katı genişlik/yükseklik."""
    ar = w / h
    hh = math.sqrt(mp * 1e6 / ar)
    ww = hh * ar
    return max(mult, round(ww / mult) * mult), max(mult, round(hh / mult) * mult)


def run_batch(cfg, wf_template, images, audios, values, modes, durations, run_dir, sizes):
    host = cfg["host"].rstrip("/")
    n = cfg["nodes"]
    try:
        img_names = []
        for iname, idata in images:
            img_names.append(upload_file(host, f"ui_{uuid.uuid4().hex[:8]}_{iname}", idata))
        log(f"{len(img_names)} fotoğraf yüklendi")
        for i, (aname, adata) in enumerate(audios):
            item = STATE["items"][i]
            if STATE["cancel"]:
                item["status"] = "iptal"
                continue
            try:
                item["status"] = "ses yükleniyor"
                aud_name = upload_file(host, f"ui_{uuid.uuid4().hex[:8]}_{aname}", adata)

                wf = copy.deepcopy(wf_template)
                # arayüzden yapılan workflow ayar değişiklikleri (adım sayısı, sampler, fps...)
                for ov_id, ov_vals in (cfg.get("overrides") or {}).items():
                    if ov_id in wf:
                        for k, v in ov_vals.items():
                            if k in wf[ov_id]["inputs"] and not isinstance(wf[ov_id]["inputs"][k], list):
                                wf[ov_id]["inputs"][k] = v
                img_idx = i % len(img_names)
                wf[n["image"]]["inputs"]["image"] = img_names[img_idx]
                if cfg.get("auto_res") and n.get("gen") and sizes and sizes[img_idx][0]:
                    W, H = calc_size(sizes[img_idx][0], sizes[img_idx][1], float(cfg.get("mp", 0.4)))
                    wf[n["gen"]]["inputs"]["width"] = W
                    wf[n["gen"]]["inputs"]["height"] = H
                    log(f"#{i+1} çözünürlük: {W}x{H}")
                if n.get("fast") and n["fast"] in wf:
                    wf[n["fast"]]["inputs"]["value"] = bool(cfg.get("fast", True))
                wf[n["audio"]]["inputs"]["audio"] = aud_name
                wf[n["duration"]]["inputs"]["value"] = float(durations[i])
                if n.get("seed") and n["seed"] in wf:
                    wf[n["seed"]]["inputs"]["noise_seed"] = random.randint(0, 2**50)

                prompt = wf[n["prompt"]]["inputs"]["value"]
                mode = modes[i]
                vals = {
                    k: str(v).strip().replace('"', "'").replace("\n", " ")
                    for k, v in (values[i] or {}).items()
                }
                tpl = unicodedata.normalize("NFC", (cfg.get("templates") or {}).get(mode, "") or "")
                if tpl.strip():
                    final_prompt = tpl
                    for k, v in vals.items():
                        final_prompt = final_prompt.replace("{" + unicodedata.normalize("NFC", k) + "}", v)
                    left = re.findall(r"\{[A-ZÇĞİÖŞÜ_]{2,}\}", final_prompt)
                    if left:
                        raise RuntimeError(f"Şablonda doldurulmamış alan var: {', '.join(left)}")
                else:
                    final_prompt, how = inject_lyrics(prompt, cfg["placeholder"], vals.get("SÖZ", ""))
                    if final_prompt is None:
                        log("Workflow'daki prompt başı: " + str(prompt)[:300].replace("\n", " "))
                        raise RuntimeError("Prompt şablonu boş ve workflow'daki prompt'ta söz yeri bulunamadı.")
                wf[n["prompt"]]["inputs"]["value"] = final_prompt
                (run_dir / f"{i+1:02d}_prompt.txt").write_text(final_prompt, encoding="utf-8")
                short = "; ".join(f"{k}={v[:40]}" for k, v in vals.items())
                log(f"#{i+1} [{mode}] {short}")

                item["status"] = "render"
                log(f"#{i+1} gönderiliyor ({durations[i]} sn, fotoğraf {img_idx+1}/{len(img_names)})")
                r = requests.post(
                    f"{host}/prompt",
                    json={"prompt": wf, "client_id": uuid.uuid4().hex},
                    timeout=60,
                )
                if r.status_code != 200:
                    raise RuntimeError(f"/prompt reddetti: {r.text[:400]}")
                pid = r.json()["prompt_id"]

                hist = wait_history(host, pid, item)
                f = find_output(hist, n["output"])
                if not f:
                    raise RuntimeError("Çıktı videosu bulunamadı")
                v = requests.get(
                    f"{host}/view",
                    params={
                        "filename": f["filename"],
                        "subfolder": f.get("subfolder", ""),
                        "type": f.get("type", "output"),
                    },
                    timeout=300,
                )
                v.raise_for_status()
                ext = Path(f["filename"]).suffix or ".mp4"
                stem = Path(aname).stem
                local = run_dir / f"{i+1:02d}_{stem}{ext}"
                local.write_bytes(v.content)
                item["video"] = f"/out/{run_dir.name}/{local.name}"
                item["status"] = "bitti"
                log(f"#{i+1} bitti -> {local.name}")
            except Cancelled:
                item["status"] = "iptal"
            except Exception as e:  # noqa: BLE001
                item["status"] = "hata"
                item["error"] = str(e)
                log(f"#{i+1} HATA: {e}")
        log("Kuyruk tamamlandı." if not STATE["cancel"] else "İptal edildi.")
    except Exception as e:  # noqa: BLE001
        log(f"Genel hata: {e}")
        for it in STATE["items"]:
            if it["status"] == "bekliyor":
                it["status"] = "hata"
                it["error"] = str(e)
    finally:
        STATE["running"] = False


# ---------------------------------------------------------------- API
@app.route("/")
def index():
    return HTML


@app.route("/out/<path:p>")
def out(p):
    return send_from_directory(OUT, p)


@app.route("/api/start", methods=["POST"])
def start():
    if STATE["running"]:
        return jsonify(error="Zaten çalışıyor"), 400
    try:
        cfg = json.loads(request.form["cfg"])
        wf = json.loads(request.files["workflow"].read().decode("utf-8"))
        images = [(f.filename, f.read()) for f in request.files.getlist("images")]
        if not images:
            return jsonify(error="Fotoğraf yok"), 400
        audio_files = request.files.getlist("audios")
        audios = [(a.filename, a.read()) for a in audio_files]
        values = json.loads(request.form["values"])
        modes = json.loads(request.form["modes"])
        durations = json.loads(request.form["durations"])
        sizes = json.loads(request.form.get("sizes", "[]"))
        if not audios:
            return jsonify(error="Ses dosyası yok"), 400
        for k, nid in cfg["nodes"].items():
            if nid and nid not in wf:
                return jsonify(error=f"Workflow'da '{nid}' ID'li düğüm yok ({k})"), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Girdi hatası: {e}"), 400

    run_dir = OUT / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    with LOCK:
        STATE.update(running=True, cancel=False, log=[])
        STATE["host"] = cfg["host"].rstrip("/")
        STATE["items"] = [
            {"name": a[0], "status": "bekliyor", "video": None, "error": "", "elapsed": 0}
            for a in audios
        ]
    threading.Thread(
        target=run_batch,
        args=(cfg, wf, images, audios, values, modes, durations, run_dir, sizes),
        daemon=True,
    ).start()
    return jsonify(ok=True)


@app.route("/api/stop", methods=["POST"])
def stop():
    STATE["cancel"] = True
    try:
        requests.post(f"{STATE['host']}/interrupt", timeout=10)
    except Exception:  # noqa: BLE001
        pass
    return jsonify(ok=True)


@app.route("/api/status")
def status():
    return jsonify(running=STATE["running"], items=STATE["items"], log=STATE["log"][-30:])


# ---------------------------------------------------------------- UI
HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<title>MiniMax H3 Toplu Render</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#14161a;--card:#1d2026;--line:#2c313a;--tx:#e6e8eb;--mut:#8b93a1;--ac:#6ea8ff;--ok:#4cc38a;--er:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
label{color:var(--mut);font-size:12px;display:block;margin-bottom:4px}
input[type=text],input[type=number],textarea{background:#101216;border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:7px 9px;width:100%;font:inherit}
textarea{resize:vertical;min-height:70px}
button{background:var(--ac);color:#06101f;border:0;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer}
button.sec{background:#2a2f38;color:var(--tx)}button.dan{background:var(--er);color:#fff}
button:disabled{opacity:.5;cursor:default}
.item{display:grid;grid-template-columns:36px 1fr 1.6fr 170px;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
.item:first-child{border-top:0}
.num{font-size:20px;font-weight:700;color:var(--ac)}
.meta{color:var(--mut);font-size:12px;margin-top:4px}
audio{width:100%;height:34px}
video{width:100%;border-radius:6px;margin-top:6px}
.st{font-weight:600}.st.bitti{color:var(--ok)}.st.hata{color:var(--er)}.st.render{color:var(--ac)}
.mini{padding:3px 8px;font-size:12px;margin-right:4px}
details summary{cursor:pointer;color:var(--mut)}
.nodes{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-top:10px}
pre{background:#0c0e11;border-radius:6px;padding:10px;max-height:160px;overflow:auto;font-size:12px;color:var(--mut);margin:0}
img.prev{max-height:110px;border-radius:6px;margin-top:8px}
.wfn{border-top:1px solid var(--line);padding:10px 0}.wfn h4{margin:0;font-size:13px}
.wfn .nodes{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
.thumbs{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.th{position:relative}.th img{height:70px;border-radius:6px;display:block}
.th b{position:absolute;left:4px;top:4px;background:#000a;border-radius:4px;padding:0 6px;font-size:12px}
.th span{position:absolute;right:3px;top:3px;background:#000a;border-radius:50%;width:18px;height:18px;text-align:center;line-height:18px;cursor:pointer;font-size:12px}
select{background:#101216;border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:6px 8px;font:inherit;max-width:100%}
.fld{margin-top:8px}
@media(max-width:800px){.item{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>🎬 MiniMax H3 – Toplu Render (şarkı · dans · enstrüman)</h1>

<div class="card">
 <div class="row">
  <div style="flex:1;min-width:220px"><label>ComfyUI adresi</label><input type="text" id="host" value="http://127.0.0.1:8188"></div>
  <div style="flex:1;min-width:220px"><label>Workflow (API format .json)</label><input type="file" id="wf" accept=".json"></div>
  <div style="flex:2;min-width:260px"><label>Fotoğraflar (sırayla kullanılır, bitince başa döner)</label>
   <input type="file" id="img" accept="image/*" multiple hidden>
   <button class="sec" onclick="img.click()">+ Fotoğraf ekle</button>
   <div id="imgs" class="thumbs"></div></div>
 </div>
 <label style="margin-top:12px;display:flex;gap:8px;align-items:center;color:var(--tx);font-size:13px"><input type="checkbox" id="auto_res" checked> Çözünürlüğü fotoğrafın oranına göre otomatik ayarla (yüzün kırpılmasını / yakınlaşmayı önler)</label>
 <label style="margin-top:6px;display:flex;gap:8px;align-items:center;color:var(--tx);font-size:13px"><input type="checkbox" id="fast" checked> Hızlı mod (Lightning LoRA, 4 adım) &nbsp;<span class="meta">– kapatırsan daha kaliteli ama çok daha yavaş</span></label>
 <details style="margin-top:12px"><summary>Gelişmiş: düğüm ID'leri</summary>
  <div class="nodes">
   <div><label>Fotoğraf (LoadImage)</label><input type="text" id="n_image" value="137"></div>
   <div><label>Ses (LoadAudio)</label><input type="text" id="n_audio" value="200"></div>
   <div><label>Prompt metni</label><input type="text" id="n_prompt" value="138"></div>
   <div><label>Süre (Float)</label><input type="text" id="n_duration" value="132"></div>
   <div><label>Seed (RandomNoise)</label><input type="text" id="n_seed" value="129"></div>
   <div><label>Çıktı (SaveVideo)</label><input type="text" id="n_output" value="92"></div>
   <div><label>Söz yer tutucusu</label><input type="text" id="ph" value="[Şarkı Sözü]"></div>
   <div><label>Süreye ek (sn)</label><input type="number" id="pad" value="0" step="0.1"></div>
   <div><label>Üretim düğümü (width/height)</label><input type="text" id="n_gen" value="136"></div>
   <div><label>Lightning anahtarı (Boolean)</label><input type="text" id="n_fast" value="146"></div>
   <div><label>Megapiksel (oto çözünürlük)</label><input type="number" id="mp" value="0.4" step="0.05"></div>
  </div>
 </details>
</div>

<div class="card">
 <b>Prompt şablonları</b> <span class="meta">— her mod için ayrı. <b>{SÖZ}</b>, <b>{ENSTRÜMAN}</b>, <b>{TÜR_TEMPO}</b>, <b>{DANS}</b> gibi {…} alanlarına her sesin satırında yazdığın değer girer. Buradaki metin workflow'daki prompt'un yerine geçer.</span>
 <div class="row" style="margin-top:8px"><select id="tplmode" onchange="tplLoad()"></select>
  <button class="sec mini" onclick="tplReset()">Bu modu varsayılana sıfırla</button></div>
 <textarea id="tpl" style="min-height:190px;margin-top:8px"></textarea>
 <details style="margin-top:12px"><summary>Workflow ayarları (adım sayısı, sampler, fps…)</summary>
  <div class="meta" style="margin:8px 0">Değiştirdiğin alanlar mavi çerçeveli olur ve kaydedilir; workflow dosyasına dokunulmaz.</div>
  <div id="wfset"><div class="meta">Önce workflow seç.</div></div>
  <button class="sec mini" onclick="ovReset()">Tüm değişiklikleri sıfırla</button>
 </details>
</div>

<div class="card">
 <div class="row" style="justify-content:space-between">
  <div><b>Ses dosyaları</b> <span class="meta">(sırayla render edilir, her satırın modu ayrı seçilir)</span></div>
  <div><select id="allmode"></select> <button class="sec mini" onclick="setAllMode()">Hepsini bu moda çevir</button>
   <input type="file" id="aud" accept="audio/*" multiple hidden>
   <button class="sec" onclick="aud.click()">+ Ses ekle</button></div>
 </div>
 <div id="list"></div>
</div>

<div class="row" style="margin-bottom:14px">
 <button id="go" onclick="start()">▶ Başlat</button>
 <button id="stop" class="dan" onclick="stopIt()" disabled>■ Durdur</button>
 <span id="msg" class="meta"></span>
</div>
<div class="card"><pre id="log">hazır</pre></div>
</div>
<script>
const $=id=>document.getElementById(id);
let items=[];            // {file,url,duration,lyrics}
let running=false;

// ayarları hatırla
const KEYS=['host','n_image','n_audio','n_prompt','n_duration','n_seed','n_output','ph','pad','n_gen','n_fast','mp'];
KEYS.forEach(k=>{const v=localStorage.getItem('ls_'+k);if(v!==null)$(k).value=v;$(k).addEventListener('input',()=>localStorage.setItem('ls_'+k,$(k).value))});

let images=[];           // {file,url}
$('img').onchange=e=>{
  [...e.target.files].forEach(f=>{
    const m={file:f,url:URL.createObjectURL(f),w:0,h:0};
    const im=new Image();im.onload=()=>{m.w=im.naturalWidth;m.h=im.naturalHeight};im.src=m.url;
    images.push(m);
  });
  e.target.value='';renderImgs();
};
function renderImgs(){
  const T=$('imgs');T.innerHTML='';
  images.forEach((m,i)=>{
    const d=document.createElement('div');d.className='th';
    d.innerHTML=`<img src="${m.url}"><b>${i+1}</b>${running?'':`<span onclick="rmImg(${i})">✕</span>`}`;
    T.appendChild(d);
  });
  render();
}
function rmImg(i){images.splice(i,1);renderImgs()}

$('aud').onchange=e=>{
  [...e.target.files].sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true})).forEach(f=>{
    const it={file:f,url:URL.createObjectURL(f),duration:null,mode:items.length?items[items.length-1].mode:'sing',values:{},status:'bekliyor',video:null,error:'',elapsed:0};
    const a=new Audio(it.url);
    a.onloadedmetadata=()=>{it.duration=Math.round(a.duration*100)/100;render()};
    items.push(it);
  });
  e.target.value='';render();
};

function render(){
  const L=$('list');
  L.innerHTML='';
  if(!items.length){L.innerHTML='<div class="meta" style="padding:14px 0">Henüz ses eklenmedi.</div>';return}
  items.forEach((it,i)=>{
    const d=document.createElement('div');d.className='item';
    d.innerHTML=`<div class="num">${i+1}</div>
     <div><div><b>${esc(it.file.name)}</b></div>
      <audio controls src="${it.url}"></audio>
      <div class="meta">Süre: ${it.duration??'…'} sn</div>
      <div style="margin-top:6px">
       <button class="sec mini" ${running?'disabled':''} onclick="mv(${i},-1)">↑</button>
       <button class="sec mini" ${running?'disabled':''} onclick="mv(${i},1)">↓</button>
       <button class="sec mini" ${running?'disabled':''} onclick="rm(${i})">Sil</button></div></div>
     <div>${fieldsHtml(it,i)}</div>
     <div>${images.length?`<div class="meta" style="margin:0 0 4px">Fotoğraf ${i%images.length+1}/${images.length}</div><img class="prev" style="max-height:60px;margin:0 0 6px" src="${images[i%images.length].url}">`:''}<div class="st ${it.status}" id="st${i}">${it.status}${it.status==='render'?' ('+it.elapsed+' sn)':''}</div>
      <div class="meta" id="er${i}">${esc(it.error||'')}</div>
      ${it.video?`<video controls src="${it.video}"></video>`:''}</div>`;
    L.appendChild(d);
  });
}
function note(m,ok){const e=$('msg');e.textContent=m;e.style.color=ok?'':'#ff6b6b'}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function mv(i,d){const j=i+d;if(j<0||j>=items.length)return;[items[i],items[j]]=[items[j],items[i]];render()}
function rm(i){items.splice(i,1);render()}



const MODES={
 sing:{label:'🎤 Şarkı söyle',fields:[{k:'SÖZ',label:'Şarkı sözü (bu sesteki)',area:true,req:true,ph:'Bu ses dosyasında söylenen sözleri yaz...'}],
  tpl:`Use <Picture 1> exactly as the starting scene: keep the same framing, camera angle, lighting, background, and composition throughout the entire shot, static camera, no cuts, no scene change. The person in <Picture 1> is singing, lip-syncing precisely to <Audio 1>, mouth movements matching every syllable of the vocals in <Audio 1>. Subtle natural head and body movement only, everything else in the frame stays as close as possible to the original photo. Audio: use <Audio 1> exactly as it is, unmodified.\n\nThe exact lyrics being sung, in order, are: "{SÖZ}". Use this written text as the primary reference for which phonemes and mouth shapes to produce at each moment, since it is more reliable than the audio alone. Align the mouth shapes to these words and their syllables as the vocals in <Audio 1> progress.`},
 instrument:{label:'🎸 Enstrüman çal',fields:[
   {k:'ENSTRÜMAN',label:'Enstrüman',req:true,ph:'örn. baglama/saz, guitar, piano'},
   {k:'TÜR_TEMPO',label:'Tür / tempo (isteğe bağlı)',ph:'örn. Turkish folk, medium tempo, 90 BPM',def:'the rhythm and mood of the music in <Audio 1>'}],
  tpl:`Use <Picture 1> exactly as the starting scene: keep the same framing, camera angle, lighting, background, and composition throughout the entire shot, static camera, no cuts, no scene change. The person in <Picture 1> is playing the {ENSTRÜMAN} they are holding, performing live in perfect rhythm with the music in <Audio 1>. Finger and hand movements must match the notes, tempo, rhythm and accents of <Audio 1> precisely: the strumming/picking hand hits the strings on every beat and rhythmic pattern, the fretting hand moves along the neck in time with the melody changes. The instrument stays in the same position and is never swapped or replaced. Natural body sway and head nodding with the beat, subtle facial expression following the mood of the music, mouth closed or relaxed (not singing) unless the music has vocals. Everything else in the frame stays as close as possible to the original photo. Audio: use <Audio 1> exactly as it is, unmodified.\n\nMusical context: {TÜR_TEMPO}. Use the rhythm and tempo of <Audio 1> as the primary reference for when each strum, pluck or finger movement happens.`},
 dance:{label:'💃 Dans et',fields:[{k:'DANS',label:'Dans tarzı / tempo (isteğe bağlı)',ph:'örn. upbeat pop dance, 120 BPM',def:'energetic dance'}],
  tpl:`Use <Picture 1> exactly as the starting scene and character identity: keep the same framing, camera angle, lighting, background, and composition throughout the entire shot, static camera, no cuts, no scene change. The person in <Picture 1> is dancing, performing full-body choreography in {DANS} style, in perfect rhythm with the music in <Audio 1>. Every step, hip sway, arm movement and head nod lands precisely on the beats of <Audio 1>, following its tempo, accents and changes. Natural fluid motion; the outfit and appearance stay the same as in <Picture 1>. Everything else in the frame stays as close as possible to the original photo. Audio: use <Audio 1> exactly as it is, unmodified.`}
};
function tplOf(m){const s=localStorage.getItem('tplm_'+m);return s!==null?s:MODES[m].tpl}
function tplLoad(){$('tpl').value=tplOf($('tplmode').value)}
function tplReset(){const m=$('tplmode').value;localStorage.removeItem('tplm_'+m);tplLoad()}
$('tpl').oninput=()=>localStorage.setItem('tplm_'+$('tplmode').value,$('tpl').value);
function modeOpts(sel){return Object.entries(MODES).map(([k,v])=>`<option value="${k}" ${k===sel?'selected':''}>${v.label}</option>`).join('')}
function fieldsHtml(it,i){
  const dis=running?'disabled':'';
  return `<label>Mod</label><select ${dis} onchange="setMode(${i},this.value)">${modeOpts(it.mode)}</select>`+
   MODES[it.mode].fields.map(f=>{
    const v=esc(it.values[f.k]||'');
    const inp=f.area
      ?`<textarea ${dis} oninput="items[${i}].values['${f.k}']=this.value" placeholder="${esc(f.ph||'')}">${v}</textarea>`
      :`<input type="text" ${dis} value="${v}" oninput="items[${i}].values['${f.k}']=this.value" placeholder="${esc(f.ph||'')}">`;
    return `<div class="fld"><label>${esc(f.label)}</label>${inp}</div>`;
   }).join('');
}
function setMode(i,m){items[i].mode=m;render()}
function setAllMode(){const m=$('allmode').value;items.forEach(it=>it.mode=m);render()}
$('tplmode').innerHTML=modeOpts('sing');$('allmode').innerHTML=modeOpts('sing');tplLoad();

let wfData=null,wfName='',ov={};
$('wf').onchange=async e=>{
  const f=e.target.files[0];if(!f)return;
  try{wfData=JSON.parse(await f.text());wfName=f.name}catch(err){wfData=null;return note('Workflow JSON okunamadı: '+err.message)}
  try{ov=JSON.parse(localStorage.getItem('ov_'+wfName)||'{}')}catch(err){ov={}}
  buildWfSettings();note('Workflow yüklendi: '+f.name,true);
};
function ovReset(){ov={};localStorage.setItem('ov_'+wfName,'{}');buildWfSettings()}
function buildWfSettings(){
  const box=$('wfset');box.innerHTML='';
  if(!wfData)return;
  const hide=new Set(['n_image','n_audio','n_prompt','n_duration','n_seed','n_fast'].map(k=>$(k).value.trim()));
  Object.entries(wfData).forEach(([id,node])=>{
    if(hide.has(id))return;
    const ins=Object.entries(node.inputs||{}).filter(([k,v])=>!Array.isArray(v)&&['string','number','boolean'].includes(typeof v));
    if(!ins.length)return;
    const d=document.createElement('div');d.className='wfn';
    d.innerHTML=`<h4>${esc((node._meta&&node._meta.title)||node.class_type)} <span class="meta">#${id} · ${esc(node.class_type)}</span></h4><div class="nodes" style="margin-top:6px"></div>`;
    const g=d.querySelector('.nodes');
    ins.forEach(([k,orig])=>{
      const w=document.createElement('div');
      const lab=document.createElement('label');lab.textContent=k;w.appendChild(lab);
      const cur=(ov[id]&&k in ov[id])?ov[id][k]:orig;
      const inp=document.createElement('input');
      if(typeof orig==='boolean'){inp.type='checkbox';inp.checked=cur}
      else if(typeof orig==='number'){inp.type='number';inp.step='any';inp.value=cur}
      else{inp.type='text';inp.value=cur}
      const mark=()=>{inp.style.outline=(ov[id]&&k in ov[id])?'2px solid var(--ac)':''};
      inp.onchange=()=>{
        const v=typeof orig==='boolean'?inp.checked:typeof orig==='number'?Number(inp.value):inp.value;
        if(typeof orig==='number'&&!isFinite(v))return;
        if(v===orig){if(ov[id]){delete ov[id][k];if(!Object.keys(ov[id]).length)delete ov[id]}}
        else{(ov[id]=ov[id]||{})[k]=v}
        localStorage.setItem('ov_'+wfName,JSON.stringify(ov));mark();
      };
      mark();w.appendChild(inp);g.appendChild(w);
    });
    box.appendChild(d);
  });
}

async function start(){
 try{
  if(!$('wf').files[0])return note('Önce workflow JSON dosyasını seç.');
  if(!images.length)return note('En az bir fotoğraf ekle.');
  if(!items.length)return note('En az bir ses dosyası ekle.');
  if(items.some(i=>i.duration==null))return note('Ses süreleri okunamadı/okunuyor, biraz bekle veya dosyayı kontrol et.');
  for(let i=0;i<items.length;i++){
    const it=items[i],t=tplOf(it.mode);
    for(const f of MODES[it.mode].fields){
      if(f.req&&t.includes('{'+f.k+'}')&&!(it.values[f.k]||'').trim())return note(`#${i+1} (${MODES[it.mode].label}) için "${f.label}" boş.`);
    }
  }
  note('Başlatılıyor…',true);
  if($('auto_res').checked&&images.some(m=>!m.w))return note('Fotoğraf boyutları okunuyor, tekrar dene.');
  const pad=parseFloat($('pad').value)||0;
  const cfg={templates:Object.fromEntries(Object.keys(MODES).map(m=>[m,tplOf(m)])),overrides:ov,host:$('host').value,placeholder:$('ph').value,auto_res:$('auto_res').checked,fast:$('fast').checked,mp:parseFloat($('mp').value)||0.4,
    nodes:{image:$('n_image').value.trim(),audio:$('n_audio').value.trim(),prompt:$('n_prompt').value.trim(),
           duration:$('n_duration').value.trim(),seed:$('n_seed').value.trim(),output:$('n_output').value.trim(),
           gen:$('n_gen').value.trim(),fast:$('n_fast').value.trim()}};
  const fd=new FormData();
  fd.append('cfg',JSON.stringify(cfg));
  fd.append('workflow',$('wf').files[0]);
  images.forEach(m=>fd.append('images',m.file,m.file.name));
  items.forEach(i=>fd.append('audios',i.file,i.file.name));
  fd.append('sizes',JSON.stringify(images.map(m=>[m.w,m.h])));
  fd.append('modes',JSON.stringify(items.map(i=>i.mode)));
  fd.append('values',JSON.stringify(items.map(i=>Object.fromEntries(MODES[i.mode].fields.map(f=>[f.k,((i.values[f.k]||'').trim())||f.def||''])))));
  fd.append('durations',JSON.stringify(items.map(i=>Math.round((i.duration+pad)*100)/100)));
  items.forEach(i=>{i.status='bekliyor';i.video=null;i.error=''});
  const r=await fetch('/api/start',{method:'POST',body:fd});
  const j=await r.json();
  if(!r.ok)return note(j.error);
  running=true;setBtns();renderImgs();poll();
 }catch(e){note('Hata: '+e.message)}
}
async function stopIt(){await fetch('/api/stop',{method:'POST'});$('msg').textContent='Durduruluyor…'}
function setBtns(){$('go').disabled=running;$('stop').disabled=!running}

async function poll(){
  const j=await (await fetch('/api/status')).json();
  const was=running;running=j.running;
  const changed=j.items.some((s,i)=>items[i]&&(s.video!==items[i].video||s.status!==items[i].status));
  j.items.forEach((s,i)=>{if(items[i])Object.assign(items[i],{status:s.status,video:s.video,error:s.error,elapsed:s.elapsed})});
  if(was&&!running)renderImgs();else if(changed)render();
  else j.items.forEach((s,i)=>{const e=$('st'+i);if(e&&s.status==='render')e.textContent='render ('+s.elapsed+' sn)'});
  $('log').textContent=j.log.join('\n')||'…';$('log').scrollTop=1e9;
  setBtns();
  if(running)setTimeout(poll,1500);else $('msg').textContent='';
}
render();
renderImgs();
fetch('/api/status').then(r=>r.json()).then(j=>{if(j.running){running=true;setBtns();poll()}});
</script></body></html>"""

def main():
    from werkzeug.serving import make_server

    try:
        srv = make_server("127.0.0.1", 7860, app, threaded=True)
    except OSError:  # port doluysa rastgele boş port
        srv = make_server("127.0.0.1", 0, app, threaded=True)
    url = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        import webview  # pip install pywebview
    except ImportError:
        import webbrowser
        print("pywebview yok, tarayıcıda açılıyor:", url)
        webbrowser.open(url)
        threading.Event().wait()
        return

    webview.create_window("LipSync Toplu Render", url, width=1200, height=860, min_size=(900, 600))
    # private_mode=False: ayarlar (ComfyUI adresi, düğüm ID'leri) kapanınca silinmesin
    webview.start(private_mode=False, storage_path=str(BASE / "webview_data"))

    # pencere kapandı -> çalışan iş varsa durdur
    if STATE["running"]:
        STATE["cancel"] = True
        try:
            requests.post(f"{STATE['host']}/interrupt", timeout=5)
        except Exception:  # noqa: BLE001
            pass
    srv.shutdown()


if __name__ == "__main__":
    main()
