#!/usr/bin/env python3
"""
MiniMax H3 Batch Studio
Batch-render singing / dancing / instrument-playing videos from photos with a
ComfyUI MiniMax H3 reference-to-video workflow. ComfyUI runs in the background.

Install:  pip install flask requests pywebview
Run:      python sarki_arayuz.py

ComfyUI must be running (default http://127.0.0.1:8188).
Workflow: a JSON saved from ComfyUI with "Save (API Format)".
"""
import copy
import datetime
import json
import math
import random
import re
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
# When packaged as an exe, outputs are saved next to the exe
BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OUT = BASE / "outputs"
OUT.mkdir(exist_ok=True)

STATE = {"running": False, "cancel": False, "items": [], "log": [], "host": "", "lang": "en"}
LOCK = threading.Lock()

# ---------------------------------------------------------------- messages (EN / TR)
MSG = {
    "en": {
        "imgs_uploaded": "{n} image(s) uploaded",
        "resolution": "#{i} resolution: {w}x{h}",
        "unfilled": "Template has unfilled field(s): {f}",
        "wf_prompt_head": "Prompt at the start of the workflow: {p}",
        "no_slot": "The prompt template is empty and no lyrics slot was found in the workflow prompt.",
        "sending": "#{i} sending ({d} s, image {a}/{b})",
        "rejected": "/prompt rejected: {t}",
        "no_output": "Output video not found",
        "done": "#{i} done -> {f}",
        "err": "#{i} ERROR: {e}",
        "queue_done": "Queue finished.",
        "queue_cancel": "Cancelled.",
        "general_err": "General error: {e}",
        "comfy_err": "ComfyUI reported an error",
        "already": "Already running",
        "no_imgs": "No images",
        "no_audio": "No audio files",
        "node_missing": "Node '{nid}' ({k}) not found in the workflow",
        "input_err": "Input error: {e}",
    },
    "tr": {
        "imgs_uploaded": "{n} fotoğraf yüklendi",
        "resolution": "#{i} çözünürlük: {w}x{h}",
        "unfilled": "Şablonda doldurulmamış alan var: {f}",
        "wf_prompt_head": "Workflow'daki prompt başı: {p}",
        "no_slot": "Prompt şablonu boş ve workflow'daki prompt'ta söz yeri bulunamadı.",
        "sending": "#{i} gönderiliyor ({d} sn, fotoğraf {a}/{b})",
        "rejected": "/prompt reddetti: {t}",
        "no_output": "Çıktı videosu bulunamadı",
        "done": "#{i} bitti -> {f}",
        "err": "#{i} HATA: {e}",
        "queue_done": "Kuyruk tamamlandı.",
        "queue_cancel": "İptal edildi.",
        "general_err": "Genel hata: {e}",
        "comfy_err": "ComfyUI hata verdi",
        "already": "Zaten çalışıyor",
        "no_imgs": "Fotoğraf yok",
        "no_audio": "Ses dosyası yok",
        "node_missing": "Workflow'da '{nid}' ID'li düğüm yok ({k})",
        "input_err": "Girdi hatası: {e}",
    },
}


def T(key, **kw):
    lang = STATE.get("lang", "en")
    return MSG.get(lang, MSG["en"])[key].format(**kw)


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
                msg = T("comfy_err")
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
    """Fallback used only when no prompt template is set.
    Puts the lyrics into the workflow's own prompt. Returns (new_prompt, method) or (None, None)."""
    prompt = unicodedata.normalize("NFC", str(prompt))
    ph = unicodedata.normalize("NFC", placeholder)
    if ph and ph in prompt:
        return prompt.replace(ph, lyrics), "placeholder"
    # No placeholder (e.g. lyrics were typed into the workflow and saved):
    # replace the text inside:  ...in order, are: "<lyrics>". Use this written text...
    pat = re.compile(r'(in order, are:\s*")(.*?)("\s*\.?\s*Use this written text)', re.S)
    if pat.search(prompt):
        return pat.sub(lambda m: m.group(1) + lyrics + m.group(3), prompt, count=1), "pattern"
    return None, None


def calc_size(w, h, mp, mult=32):
    """Keep the photo's aspect ratio, ~mp megapixels, width/height multiples of 32."""
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
        log(T("imgs_uploaded", n=len(img_names)))
        for i, (aname, adata) in enumerate(audios):
            item = STATE["items"][i]
            if STATE["cancel"]:
                item["status"] = "cancelled"
                continue
            try:
                item["status"] = "uploading"
                aud_name = upload_file(host, f"ui_{uuid.uuid4().hex[:8]}_{aname}", adata)

                wf = copy.deepcopy(wf_template)
                # workflow setting overrides made in the UI (steps, sampler, fps...)
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
                    log(T("resolution", i=i + 1, w=W, h=H))
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
                        final_prompt = final_prompt.replace("{" + k + "}", v)
                    left = re.findall(r"\{[A-Z_]{2,}\}", final_prompt)
                    if left:
                        raise RuntimeError(T("unfilled", f=", ".join(left)))
                else:
                    final_prompt, _how = inject_lyrics(prompt, cfg.get("placeholder", ""), vals.get("LYRICS", ""))
                    if final_prompt is None:
                        log(T("wf_prompt_head", p=str(prompt)[:300].replace("\n", " ")))
                        raise RuntimeError(T("no_slot"))
                wf[n["prompt"]]["inputs"]["value"] = final_prompt
                (run_dir / f"{i+1:02d}_prompt.txt").write_text(final_prompt, encoding="utf-8")
                short = "; ".join(f"{k}={v[:40]}" for k, v in vals.items())
                log(f"#{i+1} [{mode}] {short}")

                item["status"] = "rendering"
                log(T("sending", i=i + 1, d=durations[i], a=img_idx + 1, b=len(img_names)))
                r = requests.post(
                    f"{host}/prompt",
                    json={"prompt": wf, "client_id": uuid.uuid4().hex},
                    timeout=60,
                )
                if r.status_code != 200:
                    raise RuntimeError(T("rejected", t=r.text[:400]))
                pid = r.json()["prompt_id"]

                hist = wait_history(host, pid, item)
                f = find_output(hist, n["output"])
                if not f:
                    raise RuntimeError(T("no_output"))
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
                item["status"] = "done"
                log(T("done", i=i + 1, f=local.name))
            except Cancelled:
                item["status"] = "cancelled"
            except Exception as e:  # noqa: BLE001
                item["status"] = "error"
                item["error"] = str(e)
                log(T("err", i=i + 1, e=e))
        log(T("queue_done") if not STATE["cancel"] else T("queue_cancel"))
    except Exception as e:  # noqa: BLE001
        log(T("general_err", e=e))
        for it in STATE["items"]:
            if it["status"] == "pending":
                it["status"] = "error"
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
        return jsonify(error=T("already")), 400
    try:
        cfg = json.loads(request.form["cfg"])
        STATE["lang"] = cfg.get("lang", "en")
        wf = json.loads(request.files["workflow"].read().decode("utf-8"))
        images = [(f.filename, f.read()) for f in request.files.getlist("images")]
        if not images:
            return jsonify(error=T("no_imgs")), 400
        audio_files = request.files.getlist("audios")
        audios = [(a.filename, a.read()) for a in audio_files]
        values = json.loads(request.form["values"])
        modes = json.loads(request.form["modes"])
        durations = json.loads(request.form["durations"])
        sizes = json.loads(request.form.get("sizes", "[]"))
        if not audios:
            return jsonify(error=T("no_audio")), 400
        for k, nid in cfg["nodes"].items():
            if nid and nid not in wf:
                return jsonify(error=T("node_missing", nid=nid, k=k)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(error=T("input_err", e=e)), 400

    run_dir = OUT / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    with LOCK:
        STATE.update(running=True, cancel=False, log=[])
        STATE["host"] = cfg["host"].rstrip("/")
        STATE["items"] = [
            {"name": a[0], "status": "pending", "video": None, "error": "", "elapsed": 0}
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
<html lang="en"><head><meta charset="utf-8">
<title>MiniMax H3 Batch Studio</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#14161a;--card:#1d2026;--line:#2c313a;--tx:#e6e8eb;--mut:#8b93a1;--ac:#6ea8ff;--ok:#4cc38a;--er:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}
h1{font-size:20px;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
label{color:var(--mut);font-size:12px;display:block;margin-bottom:4px}
input[type=text],input[type=number],textarea{background:#101216;border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:7px 9px;width:100%;font:inherit}
textarea{resize:vertical;min-height:70px}
select{background:#101216;border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:6px 8px;font:inherit;max-width:100%}
button{background:var(--ac);color:#06101f;border:0;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer}
button.sec{background:#2a2f38;color:var(--tx)}button.dan{background:var(--er);color:#fff}
button:disabled{opacity:.5;cursor:default}
.item{display:grid;grid-template-columns:36px 1fr 1.6fr 170px;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
.item:first-child{border-top:0}
.num{font-size:20px;font-weight:700;color:var(--ac)}
.meta{color:var(--mut);font-size:12px;margin-top:4px}
.fld{margin-top:8px}
audio{width:100%;height:34px}
video{width:100%;border-radius:6px;margin-top:6px}
.st{font-weight:600}.st.done{color:var(--ok)}.st.error{color:var(--er)}.st.rendering{color:var(--ac)}
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
@media(max-width:800px){.item{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<div class="top">
 <h1 data-i="title"></h1>
 <div><label data-i="lang_label" style="display:inline;margin-right:6px"></label>
  <select id="lang" onchange="setLang(this.value)"><option value="en">English</option><option value="tr">Türkçe</option></select></div>
</div>

<div class="card">
 <div class="row">
  <div style="flex:1;min-width:220px"><label data-i="host"></label><input type="text" id="host" value="http://127.0.0.1:8188"></div>
  <div style="flex:1;min-width:220px"><label data-i="wf"></label><input type="file" id="wf" accept=".json"></div>
  <div style="flex:2;min-width:260px"><label data-i="photos"></label>
   <input type="file" id="img" accept="image/*" multiple hidden>
   <button class="sec" onclick="img.click()" data-i="add_photo"></button>
   <div id="imgs" class="thumbs"></div></div>
 </div>
 <label style="margin-top:12px;display:flex;gap:8px;align-items:center;color:var(--tx);font-size:13px"><input type="checkbox" id="auto_res" checked> <span data-i="auto_res"></span></label>
 <label style="margin-top:6px;display:flex;gap:8px;align-items:center;color:var(--tx);font-size:13px"><input type="checkbox" id="fast" checked> <span data-i="fast"></span> &nbsp;<span class="meta" data-i="fast_hint"></span></label>
 <details style="margin-top:12px"><summary data-i="adv"></summary>
  <div class="nodes">
   <div><label data-i="n_image"></label><input type="text" id="n_image" value="137"></div>
   <div><label data-i="n_audio"></label><input type="text" id="n_audio" value="200"></div>
   <div><label data-i="n_prompt"></label><input type="text" id="n_prompt" value="138"></div>
   <div><label data-i="n_duration"></label><input type="text" id="n_duration" value="132"></div>
   <div><label data-i="n_seed"></label><input type="text" id="n_seed" value="129"></div>
   <div><label data-i="n_output"></label><input type="text" id="n_output" value="92"></div>
   <div><label data-i="ph"></label><input type="text" id="ph" value="[Lyrics]"></div>
   <div><label data-i="pad"></label><input type="number" id="pad" value="0" step="0.1"></div>
   <div><label data-i="n_gen"></label><input type="text" id="n_gen" value="136"></div>
   <div><label data-i="n_fast"></label><input type="text" id="n_fast" value="146"></div>
   <div><label data-i="mp"></label><input type="number" id="mp" value="0.4" step="0.05"></div>
  </div>
 </details>
</div>

<div class="card">
 <b data-i="tpl_title"></b> <span class="meta" data-i="tpl_hint"></span>
 <div class="row" style="margin-top:8px"><select id="tplmode" onchange="tplLoad()"></select>
  <button class="sec mini" onclick="tplReset()" data-i="tpl_reset"></button></div>
 <textarea id="tpl" style="min-height:190px;margin-top:8px"></textarea>
 <details style="margin-top:12px"><summary data-i="wfs_title"></summary>
  <div class="meta" style="margin:8px 0" data-i="wfs_hint"></div>
  <div id="wfset"></div>
  <button class="sec mini" onclick="ovReset()" data-i="wfs_reset"></button>
 </details>
</div>

<div class="card">
 <div class="row" style="justify-content:space-between">
  <div><b data-i="audio_title"></b> <span class="meta" data-i="audio_hint"></span></div>
  <div><select id="allmode"></select> <button class="sec mini" onclick="setAllMode()" data-i="all_apply"></button>
   <input type="file" id="aud" accept="audio/*" multiple hidden>
   <button class="sec" onclick="aud.click()" data-i="add_audio"></button></div>
 </div>
 <div id="list"></div>
</div>

<div class="row" style="margin-bottom:14px">
 <button id="go" onclick="start()" data-i="start"></button>
 <button id="stop" class="dan" onclick="stopIt()" disabled data-i="stop"></button>
 <span id="msg" class="meta"></span>
</div>
<div class="card"><pre id="log"></pre></div>
</div>
<script>
const $=id=>document.getElementById(id);

// ------------------------------------------------------------ i18n
const I18N={
 en:{
  title:'🎬 MiniMax H3 Batch Studio (sing · dance · instrument)',lang_label:'Language',
  host:'ComfyUI address',wf:'Workflow (API format .json)',
  photos:'Photos (used in order, wraps around when they run out)',add_photo:'+ Add photo',
  auto_res:"Set resolution from each photo's aspect ratio (prevents cropping / face zoom-in)",
  fast:'Fast mode (Lightning LoRA, 4 steps)',fast_hint:'– turn off for higher quality but much slower',
  adv:'Advanced: node IDs',n_image:'Photo (LoadImage)',n_audio:'Audio (LoadAudio)',n_prompt:'Prompt text',
  n_duration:'Duration (Float)',n_seed:'Seed (RandomNoise)',n_output:'Output (SaveVideo)',
  ph:'Lyrics placeholder in workflow (fallback)',pad:'Extra seconds',n_gen:'Generation node (width/height)',
  n_fast:'Lightning switch (Boolean)',mp:'Megapixels (auto resolution)',
  tpl_title:'Prompt templates',
  tpl_hint:'— one per mode. Fields like <b>{LYRICS}</b>, <b>{INSTRUMENT}</b>, <b>{GENRE_TEMPO}</b>, <b>{DANCE}</b> are filled from each audio row. This text replaces the prompt stored in the workflow.',
  tpl_reset:'Reset this mode to default',
  wfs_title:'Workflow settings (steps, sampler, fps…)',
  wfs_hint:'Changed fields get a blue outline and are remembered; the workflow file itself is not modified.',
  wfs_none:'Select a workflow first.',wfs_reset:'Reset all changes',
  audio_title:'Audio files',audio_hint:'(rendered in order; every row has its own mode)',
  all_apply:'Set all rows to this mode',add_audio:'+ Add audio',
  start:'▶ Start',stop:'■ Stop',no_audio_yet:'No audio added yet.',
  duration:'Duration',sec:'s',mode:'Mode',del:'Delete',photo_n:'Photo {a}/{b}',
  m_sing:'🎤 Sing',m_instrument:'🎸 Play instrument',m_dance:'💃 Dance',
  f_LYRICS:'Lyrics (sung in this audio)',p_LYRICS:'Type the lyrics sung in this audio file...',
  f_INSTRUMENT:'Instrument',p_INSTRUMENT:'e.g. baglama/saz, guitar, piano',
  f_GENRE_TEMPO:'Genre / tempo (optional)',p_GENRE_TEMPO:'e.g. Turkish folk, medium tempo, 90 BPM',
  f_DANCE:'Dance style / tempo (optional)',p_DANCE:'e.g. upbeat pop dance, 120 BPM',
  st_pending:'pending',st_uploading:'uploading',st_rendering:'rendering',st_done:'done',st_error:'error',st_cancelled:'cancelled',
  n_need_wf:'Select a workflow JSON file first.',n_need_img:'Add at least one photo.',n_need_aud:'Add at least one audio file.',
  n_dur:'Audio durations are still loading or could not be read; wait a moment or check the file.',
  n_req:'#{i} ({m}): "{f}" is empty.',n_starting:'Starting…',n_sizes:'Reading image sizes, try again.',
  n_err:'Error: {e}',n_wf_loaded:'Workflow loaded: {f}',n_wf_bad:'Could not read workflow JSON: {e}',n_stopping:'Stopping…',
  log_ready:'ready'
 },
 tr:{
  title:'🎬 MiniMax H3 Toplu Render (şarkı · dans · enstrüman)',lang_label:'Dil',
  host:'ComfyUI adresi',wf:'Workflow (API format .json)',
  photos:'Fotoğraflar (sırayla kullanılır, bitince başa döner)',add_photo:'+ Fotoğraf ekle',
  auto_res:'Çözünürlüğü fotoğrafın oranına göre otomatik ayarla (yüzün kırpılmasını / yakınlaşmayı önler)',
  fast:'Hızlı mod (Lightning LoRA, 4 adım)',fast_hint:'– kapatırsan daha kaliteli ama çok daha yavaş',
  adv:"Gelişmiş: düğüm ID'leri",n_image:'Fotoğraf (LoadImage)',n_audio:'Ses (LoadAudio)',n_prompt:'Prompt metni',
  n_duration:'Süre (Float)',n_seed:'Seed (RandomNoise)',n_output:'Çıktı (SaveVideo)',
  ph:"Workflow'daki söz yer tutucusu (yedek)",pad:'Süreye ek (sn)',n_gen:'Üretim düğümü (width/height)',
  n_fast:'Lightning anahtarı (Boolean)',mp:'Megapiksel (oto çözünürlük)',
  tpl_title:'Prompt şablonları',
  tpl_hint:"— her mod için ayrı. <b>{LYRICS}</b>, <b>{INSTRUMENT}</b>, <b>{GENRE_TEMPO}</b>, <b>{DANCE}</b> gibi alanlara her sesin satırında yazdığın değer girer. Buradaki metin workflow'daki prompt'un yerine geçer.",
  tpl_reset:'Bu modu varsayılana sıfırla',
  wfs_title:'Workflow ayarları (adım sayısı, sampler, fps…)',
  wfs_hint:'Değiştirdiğin alanlar mavi çerçeveli olur ve kaydedilir; workflow dosyasına dokunulmaz.',
  wfs_none:'Önce workflow seç.',wfs_reset:'Tüm değişiklikleri sıfırla',
  audio_title:'Ses dosyaları',audio_hint:'(sırayla render edilir, her satırın modu ayrı seçilir)',
  all_apply:'Hepsini bu moda çevir',add_audio:'+ Ses ekle',
  start:'▶ Başlat',stop:'■ Durdur',no_audio_yet:'Henüz ses eklenmedi.',
  duration:'Süre',sec:'sn',mode:'Mod',del:'Sil',photo_n:'Fotoğraf {a}/{b}',
  m_sing:'🎤 Şarkı söyle',m_instrument:'🎸 Enstrüman çal',m_dance:'💃 Dans et',
  f_LYRICS:'Şarkı sözü (bu sesteki)',p_LYRICS:'Bu ses dosyasında söylenen sözleri yaz...',
  f_INSTRUMENT:'Enstrüman',p_INSTRUMENT:'örn. baglama/saz, guitar, piano',
  f_GENRE_TEMPO:'Tür / tempo (isteğe bağlı)',p_GENRE_TEMPO:'örn. Turkish folk, medium tempo, 90 BPM',
  f_DANCE:'Dans tarzı / tempo (isteğe bağlı)',p_DANCE:'örn. upbeat pop dance, 120 BPM',
  st_pending:'bekliyor',st_uploading:'yükleniyor',st_rendering:'render',st_done:'bitti',st_error:'hata',st_cancelled:'iptal',
  n_need_wf:'Önce workflow JSON dosyasını seç.',n_need_img:'En az bir fotoğraf ekle.',n_need_aud:'En az bir ses dosyası ekle.',
  n_dur:'Ses süreleri okunuyor veya okunamadı; biraz bekle ya da dosyayı kontrol et.',
  n_req:'#{i} ({m}): "{f}" boş.',n_starting:'Başlatılıyor…',n_sizes:'Fotoğraf boyutları okunuyor, tekrar dene.',
  n_err:'Hata: {e}',n_wf_loaded:'Workflow yüklendi: {f}',n_wf_bad:'Workflow JSON okunamadı: {e}',n_stopping:'Durduruluyor…',
  log_ready:'hazır'
 }
};
let lang=localStorage.getItem('ls_lang')||'en';if(!I18N[lang])lang='en';
function t(k,v){let s=(I18N[lang]&&I18N[lang][k])||I18N.en[k]||k;if(v)s=s.replace(/\{(\w+)\}/g,(m,x)=>x in v?v[x]:m);return s}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function note(m,ok){const e=$('msg');e.textContent=m;e.style.color=ok?'':'#ff6b6b'}

// ------------------------------------------------------------ state
let items=[];            // {file,url,duration,mode,values,status,video,error,elapsed}
let images=[];           // {file,url,w,h}
let running=false;
let wfData=null,wfName='',ov={};

// remember settings
const KEYS=['host','n_image','n_audio','n_prompt','n_duration','n_seed','n_output','ph','pad','n_gen','n_fast','mp'];
KEYS.forEach(k=>{const v=localStorage.getItem('ls_'+k);if(v!==null)$(k).value=v;$(k).addEventListener('input',()=>localStorage.setItem('ls_'+k,$(k).value))});

// ------------------------------------------------------------ modes & templates
const MODES={
 sing:{fields:[{k:'LYRICS',area:true,req:true}],
  tpl:`Use <Picture 1> exactly as the starting scene: keep the same framing, camera angle, lighting, background, and composition throughout the entire shot, static camera, no cuts, no scene change. The person in <Picture 1> is singing, lip-syncing precisely to <Audio 1>, mouth movements matching every syllable of the vocals in <Audio 1>. Subtle natural head and body movement only, everything else in the frame stays as close as possible to the original photo. Audio: use <Audio 1> exactly as it is, unmodified.\n\nThe exact lyrics being sung, in order, are: "{LYRICS}". Use this written text as the primary reference for which phonemes and mouth shapes to produce at each moment, since it is more reliable than the audio alone. Align the mouth shapes to these words and their syllables as the vocals in <Audio 1> progress.`},
 instrument:{fields:[{k:'INSTRUMENT',req:true},{k:'GENRE_TEMPO',def:'the rhythm and mood of the music in <Audio 1>'}],
  tpl:`Use <Picture 1> exactly as the starting scene: keep the same framing, camera angle, lighting, background, and composition throughout the entire shot, static camera, no cuts, no scene change. The person in <Picture 1> is playing the {INSTRUMENT} they are holding, performing live in perfect rhythm with the music in <Audio 1>. Finger and hand movements must match the notes, tempo, rhythm and accents of <Audio 1> precisely: the strumming/picking hand hits the strings on every beat and rhythmic pattern, the fretting hand moves along the neck in time with the melody changes. The instrument stays in the same position and is never swapped or replaced. Natural body sway and head nodding with the beat, subtle facial expression following the mood of the music, mouth closed or relaxed (not singing) unless the music has vocals. Everything else in the frame stays as close as possible to the original photo. Audio: use <Audio 1> exactly as it is, unmodified.\n\nMusical context: {GENRE_TEMPO}. Use the rhythm and tempo of <Audio 1> as the primary reference for when each strum, pluck or finger movement happens.`},
 dance:{fields:[{k:'DANCE',def:'energetic dance'}],
  tpl:`Use <Picture 1> exactly as the starting scene and character identity: keep the same framing, camera angle, lighting, background, and composition throughout the entire shot, static camera, no cuts, no scene change. The person in <Picture 1> is dancing, performing full-body choreography in {DANCE} style, in perfect rhythm with the music in <Audio 1>. Every step, hip sway, arm movement and head nod lands precisely on the beats of <Audio 1>, following its tempo, accents and changes. Natural fluid motion; the outfit and appearance stay the same as in <Picture 1>. Everything else in the frame stays as close as possible to the original photo. Audio: use <Audio 1> exactly as it is, unmodified.`}
};
// older versions used Turkish placeholder names; migrate saved templates
const LEGACY={'{SÖZ}':'{LYRICS}','{ENSTRÜMAN}':'{INSTRUMENT}','{TÜR_TEMPO}':'{GENRE_TEMPO}','{DANS}':'{DANCE}'};
function tplOf(m){
  let s=localStorage.getItem('tplm_'+m);
  if(s===null)return MODES[m].tpl;
  for(const [a,b] of Object.entries(LEGACY))s=s.split(a).join(b);
  return s;
}
function tplLoad(){$('tpl').value=tplOf($('tplmode').value)}
function tplReset(){const m=$('tplmode').value;localStorage.removeItem('tplm_'+m);tplLoad()}
$('tpl').oninput=()=>localStorage.setItem('tplm_'+$('tplmode').value,$('tpl').value);
function modeOpts(sel){return Object.keys(MODES).map(k=>`<option value="${k}" ${k===sel?'selected':''}>${esc(t('m_'+k))}</option>`).join('')}
function setAllMode(){const m=$('allmode').value;items.forEach(it=>it.mode=m);render()}
function setMode(i,m){items[i].mode=m;render()}

function fieldsHtml(it,i){
  const dis=running?'disabled':'';
  return `<label>${esc(t('mode'))}</label><select ${dis} onchange="setMode(${i},this.value)">${modeOpts(it.mode)}</select>`+
   MODES[it.mode].fields.map(f=>{
    const v=esc(it.values[f.k]||'');
    const inp=f.area
      ?`<textarea ${dis} oninput="items[${i}].values['${f.k}']=this.value" placeholder="${esc(t('p_'+f.k))}">${v}</textarea>`
      :`<input type="text" ${dis} value="${v}" oninput="items[${i}].values['${f.k}']=this.value" placeholder="${esc(t('p_'+f.k))}">`;
    return `<div class="fld"><label>${esc(t('f_'+f.k))}</label>${inp}</div>`;
   }).join('');
}

// ------------------------------------------------------------ language
function applyLang(){
  document.documentElement.lang=lang;
  document.title='MiniMax H3 Batch Studio';
  document.querySelectorAll('[data-i]').forEach(e=>{e.innerHTML=t(e.dataset.i)});
  const tm=$('tplmode').value||'sing',am=$('allmode').value||'sing';
  $('tplmode').innerHTML=modeOpts(tm);$('allmode').innerHTML=modeOpts(am);
  $('lang').value=lang;
  buildWfSettings();renderImgs();
  if(!running&&!$('log').dataset.used)$('log').textContent=t('log_ready');
}
function setLang(l){lang=l;localStorage.setItem('ls_lang',l);applyLang()}

// ------------------------------------------------------------ photos
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

// ------------------------------------------------------------ audio rows
$('aud').onchange=e=>{
  [...e.target.files].sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true})).forEach(f=>{
    const it={file:f,url:URL.createObjectURL(f),duration:null,mode:items.length?items[items.length-1].mode:'sing',values:{},status:'pending',video:null,error:'',elapsed:0};
    const a=new Audio(it.url);
    a.onloadedmetadata=()=>{it.duration=Math.round(a.duration*100)/100;render()};
    items.push(it);
  });
  e.target.value='';render();
};
function stLabel(it){return t('st_'+it.status)+(it.status==='rendering'?' ('+it.elapsed+' '+t('sec')+')':'')}
function render(){
  const L=$('list');
  L.innerHTML='';
  if(!items.length){L.innerHTML=`<div class="meta" style="padding:14px 0">${esc(t('no_audio_yet'))}</div>`;return}
  items.forEach((it,i)=>{
    const d=document.createElement('div');d.className='item';
    d.innerHTML=`<div class="num">${i+1}</div>
     <div><div><b>${esc(it.file.name)}</b></div>
      <audio controls src="${it.url}"></audio>
      <div class="meta">${esc(t('duration'))}: ${it.duration??'…'} ${esc(t('sec'))}</div>
      <div style="margin-top:6px">
       <button class="sec mini" ${running?'disabled':''} onclick="mv(${i},-1)">↑</button>
       <button class="sec mini" ${running?'disabled':''} onclick="mv(${i},1)">↓</button>
       <button class="sec mini" ${running?'disabled':''} onclick="rm(${i})">${esc(t('del'))}</button></div></div>
     <div>${fieldsHtml(it,i)}</div>
     <div>${images.length?`<div class="meta" style="margin:0 0 4px">${esc(t('photo_n',{a:i%images.length+1,b:images.length}))}</div><img class="prev" style="max-height:60px;margin:0 0 6px" src="${images[i%images.length].url}">`:''}<div class="st ${it.status}" id="st${i}">${esc(stLabel(it))}</div>
      <div class="meta" id="er${i}">${esc(it.error||'')}</div>
      ${it.video?`<video controls src="${it.video}"></video>`:''}</div>`;
    L.appendChild(d);
  });
}
function mv(i,d){const j=i+d;if(j<0||j>=items.length)return;[items[i],items[j]]=[items[j],items[i]];render()}
function rm(i){items.splice(i,1);render()}

// ------------------------------------------------------------ workflow settings editor
$('wf').onchange=async e=>{
  const f=e.target.files[0];if(!f)return;
  try{wfData=JSON.parse(await f.text());wfName=f.name}catch(err){wfData=null;return note(t('n_wf_bad',{e:err.message}))}
  try{ov=JSON.parse(localStorage.getItem('ov_'+wfName)||'{}')}catch(err){ov={}}
  buildWfSettings();note(t('n_wf_loaded',{f:f.name}),true);
};
function ovReset(){ov={};if(wfName)localStorage.setItem('ov_'+wfName,'{}');buildWfSettings()}
function buildWfSettings(){
  const box=$('wfset');box.innerHTML='';
  if(!wfData){box.innerHTML=`<div class="meta">${esc(t('wfs_none'))}</div>`;return}
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

// ------------------------------------------------------------ run
async function start(){
 try{
  if(!$('wf').files[0])return note(t('n_need_wf'));
  if(!images.length)return note(t('n_need_img'));
  if(!items.length)return note(t('n_need_aud'));
  if(items.some(i=>i.duration==null))return note(t('n_dur'));
  for(let i=0;i<items.length;i++){
    const it=items[i],tp=tplOf(it.mode);
    for(const f of MODES[it.mode].fields){
      if(f.req&&tp.includes('{'+f.k+'}')&&!(it.values[f.k]||'').trim())return note(t('n_req',{i:i+1,m:t('m_'+it.mode),f:t('f_'+f.k)}));
    }
  }
  note(t('n_starting'),true);
  if($('auto_res').checked&&images.some(m=>!m.w))return note(t('n_sizes'));
  const pad=parseFloat($('pad').value)||0;
  const cfg={lang:lang,templates:Object.fromEntries(Object.keys(MODES).map(m=>[m,tplOf(m)])),overrides:ov,host:$('host').value,placeholder:$('ph').value,auto_res:$('auto_res').checked,fast:$('fast').checked,mp:parseFloat($('mp').value)||0.4,
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
  items.forEach(i=>{i.status='pending';i.video=null;i.error=''});
  const r=await fetch('/api/start',{method:'POST',body:fd});
  const j=await r.json();
  if(!r.ok)return note(j.error);
  running=true;setBtns();renderImgs();poll();
 }catch(e){note(t('n_err',{e:e.message}))}
}
async function stopIt(){await fetch('/api/stop',{method:'POST'});note(t('n_stopping'),true)}
function setBtns(){$('go').disabled=running;$('stop').disabled=!running}

async function poll(){
  const j=await (await fetch('/api/status')).json();
  const was=running;running=j.running;
  const changed=j.items.some((s,i)=>items[i]&&(s.video!==items[i].video||s.status!==items[i].status));
  j.items.forEach((s,i)=>{if(items[i])Object.assign(items[i],{status:s.status,video:s.video,error:s.error,elapsed:s.elapsed})});
  if(was&&!running)renderImgs();else if(changed)render();
  else j.items.forEach((s,i)=>{const e=$('st'+i);if(e&&s.status==='rendering')e.textContent=stLabel(items[i])});
  if(j.log.length){$('log').dataset.used='1';$('log').textContent=j.log.join('\n');$('log').scrollTop=1e9}
  setBtns();
  if(running)setTimeout(poll,1500);else $('msg').textContent='';
}

// ------------------------------------------------------------ init
$('tplmode').innerHTML=modeOpts('sing');$('allmode').innerHTML=modeOpts('sing');tplLoad();
applyLang();
fetch('/api/status').then(r=>r.json()).then(j=>{if(j.running){running=true;setBtns();poll()}});
</script></body></html>"""


def main():
    from werkzeug.serving import make_server

    try:
        srv = make_server("127.0.0.1", 7860, app, threaded=True)
    except OSError:  # port in use -> pick a free one
        srv = make_server("127.0.0.1", 0, app, threaded=True)
    url = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        import webview  # pip install pywebview
    except ImportError:
        import webbrowser

        print("pywebview not installed, opening in the browser:", url)
        webbrowser.open(url)
        threading.Event().wait()
        return

    webview.create_window("MiniMax H3 Batch Studio", url, width=1200, height=860, min_size=(900, 600))
    # private_mode=False: keep settings (ComfyUI address, node IDs, templates) after closing
    webview.start(private_mode=False, storage_path=str(BASE / "webview_data"))

    # window closed -> stop any running job
    if STATE["running"]:
        STATE["cancel"] = True
        try:
            requests.post(f"{STATE['host']}/interrupt", timeout=5)
        except Exception:  # noqa: BLE001
            pass
    srv.shutdown()


if __name__ == "__main__":
    main()
