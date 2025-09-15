import os
import sys
import re
import stat
import argparse
import base64
import threading
import time
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Tuple

from flask import Flask, request, jsonify, render_template_string
from send2trash import send2trash
import webbrowser

# =============== Helpers ===============

CHUNK = 1024 * 1024  # niet gebruikt hier (we lezen geen content), maar handig als je later uitbreidt

def human(nbytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    x = float(nbytes)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.1f} {units[i]}" if i else f"{int(x)} {units[i]}"

def encode_path(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii")

def decode_path(token: str) -> str:
    return base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")

def onedrive_paths() -> List[str]:
    paths = []
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        p = os.environ.get(var)
        if p:
            paths.append(os.path.normpath(p))
    user = os.environ.get("USERNAME", "")
    base = rf"C:\Users\{user}"
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.lower().startswith("onedrive"):
                paths.append(os.path.join(base, name))
    seen = set()
    out = []
    for p in paths:
        k = os.path.normpath(p).lower()
        if k not in seen:
            out.append(p)
            seen.add(k)
    return out

def is_cloud_placeholder(path: str) -> bool:
    try:
        st = os.stat(path, follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        recall_on_data = getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", 0)
        recall_on_open = getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_OPEN", 0)
        flags = (stat.FILE_ATTRIBUTE_OFFLINE | recall_on_data | recall_on_open)
        return bool(attrs & flags)
    except Exception:
        return False

def is_within_excludes(path: str, excludes: List[str]) -> bool:
    p = os.path.normpath(path).lower()
    for ex in excludes:
        if p.startswith(os.path.normpath(ex).lower()):
            return True
    return False

# Normalisatie van naam:
# - Leading timestamp: eejjmmdd (8 cijfers), optioneel gevolgd door -/_/spatie en mmss (4 cijfers)
# - Daarna eventuele scheidingstekens strippen
# - Extensie verwijderen als ignore_ext=True
# - Case-insensitief groeperen (Windows)
TS_REGEX = re.compile(r"""
    ^(?P<ts>\d{8})            # eejjmmdd: 8 cijfers
    (?:[-_ ]?\d{4})?          # optioneel mmss (4 cijfers) met optionele separator
    [-_ .]*                   # eventueel restscheidingstekens
""", re.VERBOSE)

def normalized_name(path: str, ignore_ext: bool) -> str:
    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    candidate = name if ignore_ext else base
    m = TS_REGEX.match(candidate)
    if m:
        candidate = candidate[m.end():]  # alles ná de timestamp
    # opschonen: underscores/dashes naar spatie, multi-spaces reduceren
    candidate = re.sub(r"[_\-\.]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    # casefold voor robuuste case-insensitieve vergelijking
    return candidate.casefold()

# =============== Status ===============
STATUS_LOCK = threading.Lock()
STATUS = {
    "state": "scanning",            # scanning | done | error
    "started": None,                # isoformat
    "elapsed_seconds": 0,
    "budget_seconds": None,
    "walk_scanned": 0,
    "walk_skipped": 0,
    "min_size": 10 * 1024 * 1024,
    "ignore_ext": True,
    "error": None,
}

def set_status(**kwargs):
    with STATUS_LOCK:
        STATUS.update(kwargs)

def get_status():
    with STATUS_LOCK:
        return dict(STATUS)

# =============== Scan ===============
DEFAULT_EXCLUDES = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\Recovery",
    r"C:\PerfLogs",
]
DEFAULT_EXCLUDES += onedrive_paths()

# resultaatstructuur: key -> list[(path, size, mtime)]
GROUPS: Dict[str, List[Tuple[str, int, float]]] = {}

def scan_by_name(
    root: str,
    min_size: int,
    ignores: List[str],
    include_cloud: bool,
    ignore_ext: bool,
    time_budget_sec: int,
):
    global GROUPS
    groups: Dict[str, List[Tuple[str, int, float]]] = defaultdict(list)
    started = time.time()
    scanned = 0
    skipped = 0

    set_status(state="scanning", started=datetime.now().isoformat(),
               budget_seconds=time_budget_sec, elapsed_seconds=0,
               walk_scanned=0, walk_skipped=0,
               min_size=min_size, ignore_ext=ignore_ext)

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if ignores:
            if is_within_excludes(dirpath, ignores):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not is_within_excludes(os.path.join(dirpath, d), ignores)]

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                if not os.path.isfile(fpath) or os.path.islink(fpath):
                    skipped += 1
                    continue
                if not include_cloud and is_cloud_placeholder(fpath):
                    skipped += 1
                    continue
                size = os.path.getsize(fpath)
                if size < min_size:
                    skipped += 1
                    continue
                key = normalized_name(fpath, ignore_ext=ignore_ext)
                mtime = os.path.getmtime(fpath)
                groups[key].append((fpath, size, mtime))
                scanned += 1
            except (PermissionError, FileNotFoundError, OSError):
                skipped += 1
                continue

            # voortgang updaten
            set_status(walk_scanned=scanned, walk_skipped=skipped,
                       elapsed_seconds=int(time.time() - started))

            # hard time budget bewaken (alleen waarschuwing/balk; we stoppen niet hard)
            if time.time() - started > time_budget_sec:
                # niets: UI kleurt tijdsbalk rood, je kunt scan laten doorlopen
                pass

    # alleen groepen met >=2 bestanden tonen
    groups = {k: v for k, v in groups.items() if len(v) >= 2 and k}

    # sorteer: eerst op aantal (desc), dan op totale grootte (desc), dan alfabetisch
    def grp_sort(item):
        k, files = item
        total = sum(s for _, s, _ in files)
        return (-len(files), -total, k)
    GROUPS = dict(sorted(groups.items(), key=grp_sort))

    set_status(state="done", elapsed_seconds=int(time.time() - started))

# =============== Flask UI ===============
app = Flask(__name__)

SCAN_ROOT = "C:\\"
MIN_SIZE = 10 * 1024 * 1024
TIME_BUDGET_SEC = 60 * 60
EXCLUDES: List[str] = []
INCLUDE_CLOUD = False
IGNORE_EXT = True

README_HTML = """
<b>README (kort)</b><br>
• Deze tool zoekt duplicaten op <i>naam</i>. Als een bestand begint met een timestamp <code>eejjmmdd</code> (optioneel gevolgd door <code>-mmss</code>), dan wordt de tekst erna als naam gebruikt.<br>
• Vergelijken gebeurt standaard <b>zonder extensie</b> (CLI <code>--ignore-ext</code>), maar kan ook <b>met</b> extensie (CLI <code>--keep-ext</code>).<br>
• Standaard uitgesloten: systeemmappen & OneDrive. Cloud-placeholders worden genegeerd.<br>
• Verwijderen gaat <b>naar de prullenbak</b> (Send2Trash).<br>
• Her-scan met andere modus? Gebruik de knoppen hieronder om snel te wisselen.
"""

PAGE_WAIT = """
<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<title>Naam-duplicaten — Scannen…</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial;margin:1.5rem;}
 .card{border:1px solid #e5e5e5;border-radius:.6rem;padding:1rem;}
 .muted{color:#666}
 .mono{font-family:ui-monospace,Consolas,monospace;}
 .bar{height:12px;background:#eee;border-radius:6px;overflow:hidden;margin:.4rem 0;position:relative}
 .bar>div{height:100%;background:#7da7ff;transition:width .3s ease}
 .bar.over>div{background:#ff8a8a}
 .indet{background:linear-gradient(90deg,#e5e9ff 0%,#cfd8ff 50%,#e5e9ff 100%);background-size:200% 100%;animation:move 1.2s linear infinite;}
 @keyframes move{0%{background-position:0% 0%}100%{background-position:-200% 0%}}
 button{padding:.35rem .65rem;border-radius:.35rem;border:1px solid #777;background:#f7f7f7;cursor:pointer}
 .primary{background:#e8f0ff;border-color:#7da7ff}
 .row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
</style>
<script>
async function tick(){
  const r = await fetch('/status'); const s = await r.json();
  // Tijd-balk
  const timePct = Math.floor(100 * (s.elapsed_seconds||0) / (s.budget_seconds||1));
  const timeBar = document.getElementById('timepct');
  timeBar.style.width = Math.min(timePct, 100) + '%';
  const timeWrap = document.getElementById('timebar');
  if (timePct > 100) timeWrap.classList.add('over'); else timeWrap.classList.remove('over');
  document.getElementById('timelabel').textContent = (timePct>100? '100+': timePct) + ' % van budget';

  document.getElementById('walk').textContent = s.walk_scanned.toLocaleString('nl-NL');
  document.getElementById('skip').textContent = s.walk_skipped.toLocaleString('nl-NL');
  document.getElementById('minsize').textContent = s.min_size.toLocaleString('nl-NL');
  document.getElementById('mode').textContent = s.ignore_ext ? 'zonder extensie' : 'met extensie';

  if (s.state === 'done') { window.location.reload(); }
  else { setTimeout(tick, 1000); }
}
async function rescan(mode){
  const r = await fetch('/rescan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ignore_ext: mode==='ignore'})});
  const s = await r.json();
  if(!s.ok){ alert(s.error || 'Rescan mislukt'); }
}
window.addEventListener('load', tick);
</script>
</head>
<body>
<h1>🔍 Naam-duplicaten: scannen…</h1>
<div class="card">
  <p class="muted">Root: <span class="mono">{{root}}</span> · Tijd-budget: {{budget_min}} min · Min. grootte: <b id="minsize">{{min_size}}</b> bytes · Modus: <b id="mode">—</b></p>

  <div class="row"><b>Tijd</b><span id="timelabel" class="muted">0 % van budget</span></div>
  <div id="timebar" class="bar"><div id="timepct" style="width:0%"></div></div>

  <div style="margin-top:.5rem">
    <div class="row"><b>Scan/walk</b><span class="muted">indeterminate</span></div>
    <div class="bar"><div class="indet" style="width:30%"></div></div>
  </div>

  <ul>
    <li>Bestanden beoordeeld: <b id="walk">0</b> · Overgeslagen: <b id="skip">0</b></li>
  </ul>

  <div class="row" style="margin-top:.5rem">
    <button class="primary" onclick="rescan('ignore')">Her-scan (zonder extensie)</button>
    <button onclick="rescan('keep')">Her-scan (met extensie)</button>
  </div>
</div>

<h3>README</h3>
<div class="card" style="background:#fafbff;">{{ readme|safe }}</div>
</body>
</html>
"""

PAGE_RESULTS = """
<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<title>Naam-duplicaten — Resultaten</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial;margin:1.5rem;}
 table{border-collapse:collapse;width:100%;}
 th,td{border-bottom:1px solid #eee;padding:.5rem .5rem;vertical-align:top;}
 th{background:#fafafa;text-align:left;}
 .badge{background:#eef;border:1px solid #99c;padding:0 .4rem;border-radius:.5rem;}
 .btn{padding:.3rem .55rem;border:1px solid #777;background:#f7f7f7;border-radius:.35rem;cursor:pointer;}
 .btn.danger{background:#fbecec;border-color:#b55;color:#700;}
 .muted{color:#666}
 .group-head{background:#f8f9ff;border:1px solid #e6e8ff;padding:.35rem .5rem;border-radius:.35rem;}
 code{background:#f5f5f7;padding:.05rem .25rem;border-radius:.25rem;}
 .sticky{position:fixed;right:1rem;bottom:1rem;background:#fff;border:1px solid #ddd;padding:.5rem .75rem;border-radius:.5rem;box-shadow:0 2px 6px rgba(0,0,0,.08);}
 .mono{font-family:ui-monospace,Consolas,monospace;}
</style>
<script>
function openFolder(token){ fetch('/open?path='+encodeURIComponent(token)); }
function delOne(token, btn){
  if(!confirm('Naar prullenbak verplaatsen?')) return;
  fetch('/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:token})})
    .then(r=>r.json()).then(d=>{ if(d.ok){ const li=btn.closest('li'); if(li) li.remove(); } else alert('Mislukt: '+(d.error||'')); });
}
function delSelected(scope){
  const picks = scope.querySelectorAll('input.pick:checked');
  if(!picks.length){ alert('Geen selectie'); return; }
  if(!confirm('Geselecteerde naar prullenbak verplaatsen?')) return;
  fetch('/delete-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:Array.from(picks).map(p=>p.value)})})
    .then(r=>r.json()).then(d=>{ if(d.ok){ picks.forEach(p=>p.closest('li').remove()); } else alert('Deels mislukt: '+(d.error||'')); });
}
async function rescan(mode){
  const r = await fetch('/rescan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ignore_ext: mode==='ignore'})});
  const s = await r.json();
  if(!s.ok){ alert(s.error || 'Rescan mislukt'); }
}
</script>
</head>
<body>
<h1>✔️ Naam-duplicaten in {{root}}</h1>
<p class="muted">Min. grootte: {{min_size}} bytes · Tijd: {{elapsed_min}} min · Modus: <b>{{ 'zonder extensie' if ignore_ext else 'met extensie' }}</b></p>

<div class="muted" style="margin-bottom:.75rem;">{{ readme|safe }}</div>

<div style="margin:.5rem 0 1rem 0;">
  <button class="btn" onclick="rescan('ignore')">Her-scan (zonder extensie)</button>
  <button class="btn" onclick="rescan('keep')">Her-scan (met extensie)</button>
</div>

<table>
  <thead>
    <tr>
      <th style="width:40%;">Genormaliseerde naam (timestamp gestript)</th>
      <th>Aantal</th>
      <th>Bestanden</th>
    </tr>
  </thead>
  <tbody>
  {% for key, files in groups.items() %}
    <tr>
      <td class="mono">{{ key if key else '(leeg na timestamp)' }}</td>
      <td><span class="badge">{{ files|length }}</span></td>
      <td>
        <ul style="list-style:none;padding-left:0;margin:.35rem 0;">
        {% for f, size, mtime in files %}
          <li style="margin:.25rem 0;">
            <label><input type="checkbox" class="pick" value="{{ enc(f) }}"> {{ f }}</label>
            <div class="muted">{{ human(size) }} · {{ mtime|datetime }}</div>
            <div class="muted">
              <button class="btn" onclick="openFolder('{{ enc(f) }}')">Open map</button>
              <button class="btn danger" onclick="delOne('{{ enc(f) }}', this)">Naar prullenbak</button>
            </div>
          </li>
        {% endfor %}
        </ul>
        <button class="btn danger" onclick="delSelected(this.closest('td'))">Geselecteerde in deze groep verwijderen</button>
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>

<div class="sticky">
  <button class="btn danger" onclick="(async()=>{const picks=document.querySelectorAll('input.pick:checked');if(!picks.length){alert('Geen selectie');return;}if(!confirm('ALLE geselecteerde naar prullenbak verplaatsen?'))return;const r=await fetch('/delete-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:Array.from(picks).map(p=>p.value)})});const d=await r.json();if(d.ok){picks.forEach(p=>p.closest('li').remove());}else alert('Deels mislukt: '+(d.error||''));})();">Verwijder ALLE geselecteerde</button>
</div>
</body>
</html>
"""

# =============== Routes ===============
@app.template_filter("datetime")
def fmt_datetime(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"

@app.route("/")
def index():
    s = get_status()
    if s["state"] == "done":
        elapsed_min = max(1, int(s["elapsed_seconds"] / 60))
        return render_template_string(
            PAGE_RESULTS,
            groups=GROUPS,
            root=SCAN_ROOT,
            min_size=s["min_size"],
            elapsed_min=elapsed_min,
            ignore_ext=s["ignore_ext"],
            readme=README_HTML,
            human=human,
            enc=encode_path
        )
    else:
        return render_template_string(
            PAGE_WAIT,
            root=SCAN_ROOT,
            budget_min=int(TIME_BUDGET_SEC/60),
            min_size=s["min_size"],
            readme=README_HTML
        )

@app.get("/status")
def status():
    return jsonify(get_status())

@app.post("/rescan")
def rescan():
    data = request.get_json(silent=True) or {}
    ignore = bool(data.get("ignore_ext", True))
    global IGNORE_EXT
    IGNORE_EXT = ignore

    # herstart scan in thread
    def worker():
        try:
            scan_by_name(
                root=SCAN_ROOT,
                min_size=MIN_SIZE,
                ignores=EXCLUDES,
                include_cloud=INCLUDE_CLOUD,
                ignore_ext=IGNORE_EXT,
                time_budget_sec=TIME_BUDGET_SEC,
            )
        except Exception as e:
            set_status(state="error", error=str(e))

    set_status(state="scanning", elapsed_seconds=0, walk_scanned=0, walk_skipped=0,
               ignore_ext=IGNORE_EXT, started=datetime.now().isoformat(),
               budget_seconds=TIME_BUDGET_SEC)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return jsonify(ok=True)

@app.get("/open")
def open_folder():
    token = request.args.get("path", "")
    try:
        path = decode_path(token)
        folder = os.path.dirname(path) if os.path.isfile(path) else path
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, folder])
        return ("", 204)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/delete")
def delete_one():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("path")
    if not token:
        return jsonify(ok=False, error="no path"), 400
    try:
        send2trash(decode_path(token))
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.post("/delete-batch")
def delete_batch():
    data = request.get_json(force=True, silent=True) or {}
    tokens = data.get("paths", [])
    errors = []
    ok = 0
    for t in tokens:
        try:
            send2trash(decode_path(t))
            ok += 1
        except Exception as e:
            errors.append(str(e))
    return jsonify(ok=(ok == len(tokens)), deleted=ok, error="; ".join(errors))

# =============== Main ===============
SCAN_ROOT = "C:\\"
MIN_SIZE = 10 * 1024 * 1024
TIME_BUDGET_SEC = 60 * 60
EXCLUDES: List[str] = []
INCLUDE_CLOUD = False
IGNORE_EXT = True

def main():
    parser = argparse.ArgumentParser(description="Zoek naam-duplicaten met timestamp-strip (eejjmmdd[-mmss]).")
    parser.add_argument("--root", default="C:\\", help="Startmap (default C:\\)")
    parser.add_argument("--min-size", type=int, default=10 * 1024 * 1024,
                        help="Minimum grootte in bytes (default 10 MB).")
    parser.add_argument("--time-budget-min", type=int, default=60,
                        help="Tijdbudget in minuten (default 60).")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--ignore-ext", action="store_true", default=True,
                   help="Vergelijk zonder extensie (default).")
    g.add_argument("--keep-ext", action="store_true",
                   help="Vergelijk mét extensie.")
    parser.add_argument("--no-excludes", action="store_true",
                        help="Geen systeem/OneDrive-uitsluitingen.")
    parser.add_argument("--add-exclude", action="append", default=[],
                        help="Extra map uitsluiten (kan herhaald).")
    parser.add_argument("--include-cloud", action="store_true",
                        help="OneDrive placeholders mee-nemen (niet nodig: we lezen geen content).")

    args = parser.parse_args()

    root = os.path.normpath(args.root)
    if not os.path.exists(root):
        print(f"Pad bestaat niet: {root}")
        sys.exit(1)

    excludes = [] if args.no_excludes else list(DEFAULT_EXCLUDES)
    excludes += args.add_exclude

    global SCAN_ROOT, MIN_SIZE, TIME_BUDGET_SEC, EXCLUDES, INCLUDE_CLOUD, IGNORE_EXT
    SCAN_ROOT = root
    MIN_SIZE = int(args.min_size)
    TIME_BUDGET_SEC = int(args.time_budget_min) * 60
    EXCLUDES = excludes
    INCLUDE_CLOUD = bool(args.include_cloud)
    IGNORE_EXT = False if args.keep_ext else True

    # start eerste scan in achtergrond
    set_status(state="scanning", started=datetime.now().isoformat(),
               budget_seconds=TIME_BUDGET_SEC, elapsed_seconds=0,
               walk_scanned=0, walk_skipped=0, min_size=MIN_SIZE, ignore_ext=IGNORE_EXT)

    def worker():
        try:
            scan_by_name(
                root=SCAN_ROOT,
                min_size=MIN_SIZE,
                ignores=EXCLUDES,
                include_cloud=INCLUDE_CLOUD,
                ignore_ext=IGNORE_EXT,
                time_budget_sec=TIME_BUDGET_SEC,
            )
        except Exception as e:
            set_status(state="error", error=str(e))

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    webbrowser.open("http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False)

if __name__ == "__main__":
    if os.name != "nt":
        print("Let op: dit script is geoptimaliseerd voor Windows.")
    main()
