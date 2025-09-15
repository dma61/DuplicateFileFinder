import os
import sys
import stat
import argparse
import hashlib
import base64
import threading
import time
from collections import defaultdict, namedtuple
from datetime import datetime
from typing import Optional, Callable

from flask import Flask, request, jsonify, render_template_string
from send2trash import send2trash
import webbrowser

# ===================== MODELLEN & HELPERS =====================
Group = namedtuple("Group", ["size", "sha256", "files"])  # files: list[str]
CHUNK = 1024 * 1024  # 1 MB

def human(nbytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    x = float(nbytes)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.1f} {units[i]}" if i else f"{int(x)} {units[i]}"

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def encode_path(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii")

def decode_path(token: str) -> str:
    return base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")

def is_within_excludes(path: str, excludes: list[str]) -> bool:
    p = os.path.normpath(path).lower()
    for ex in excludes:
        if p.startswith(os.path.normpath(ex).lower()):
            return True
    return False

# ---------- OneDrive-detectie & cloud placeholders ----------
def onedrive_paths() -> list[str]:
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

# ===================== GLOBALE STATUS =====================
STATUS_LOCK = threading.Lock()
STATUS = {
    "state": "scanning",      # scanning | hashing | needs_tuning | done | error
    "started": None,          # isoformat string
    "message": "",
    "walk_scanned": 0,
    "walk_skipped": 0,
    "hash_total": 0,
    "hash_done": 0,
    "eta_seconds": None,
    "budget_seconds": None,
    "elapsed_seconds": 0,
    "min_size": 10 * 1024 * 1024,
    "suggest_min_size": None,
    "error": None,
}

def set_status(**kwargs):
    with STATUS_LOCK:
        STATUS.update(kwargs)

def get_status():
    with STATUS_LOCK:
        return dict(STATUS)

# ===================== SCANNER =====================
DEFAULT_EXCLUDES = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\Recovery",
    r"C:\PerfLogs",
]
DEFAULT_EXCLUDES += onedrive_paths()  # OneDrive standaard uitsluiten

RESUME_EVENT = threading.Event()

def find_duplicates_streaming(
    root: str,
    min_size_ref: Callable[[], int],
    excludes: list[str],
    include_cloud: bool,
    time_budget_sec: int
) -> list[Group]:
    # ---- WALK ----
    size_buckets: dict[int, list[str]] = defaultdict(list)
    walk_scanned = 0
    walk_skipped = 0
    started = time.time()
    set_status(state="scanning", started=datetime.now().isoformat(),
               budget_seconds=time_budget_sec, elapsed_seconds=0)

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if excludes:
            if is_within_excludes(dirpath, excludes):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not is_within_excludes(os.path.join(dirpath, d), excludes)]

        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if not os.path.isfile(full) or os.path.islink(full):
                    walk_skipped += 1
                    continue
                if not include_cloud and is_cloud_placeholder(full):
                    walk_skipped += 1
                    continue
                size = os.path.getsize(full)
                walk_scanned += 1
                if size < min_size_ref():
                    walk_skipped += 1
                    continue
                size_buckets[size].append(full)
            except (PermissionError, FileNotFoundError, OSError):
                walk_skipped += 1
                continue

        set_status(walk_scanned=walk_scanned, walk_skipped=walk_skipped,
                   elapsed_seconds=int(time.time() - started))

    # ---- HASH ----
    jobs_all = [(s, p) for s, p in size_buckets.items() if len(p) >= 2]
    total_to_hash = sum(len(p) for _, p in jobs_all)
    done = 0
    set_status(state="hashing", hash_total=total_to_hash, hash_done=0)

    groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    hash_start = time.time()
    avg_per_file = None

    for size, paths in jobs_all:
        # Als minsize inmiddels hoger is dan deze bucket, alles in deze bucket als 'afgehandeld' tellen
        if size < min_size_ref():
            done += len(paths)
            set_status(hash_done=done,
                       elapsed_seconds=int(time.time() - started),
                       eta_seconds=max(int((total_to_hash - done) * (avg_per_file or 0)), 0) if (avg_per_file and total_to_hash > done) else None)
            continue

        for p in paths:
            elapsed = time.time() - started
            remaining_budget = time_budget_sec - elapsed

            if done > 5:
                avg_per_file = (time.time() - hash_start) / done
            predicted_remaining = (total_to_hash - done) * (avg_per_file or 0)
            eta = int(predicted_remaining) if predicted_remaining else None
            set_status(hash_done=done, eta_seconds=eta, elapsed_seconds=int(elapsed))

            if remaining_budget > 0 and eta and eta > remaining_budget:
                cur = min_size_ref()
                suggest = max(cur * 2, 50 * 1024 * 1024)
                set_status(state="needs_tuning", suggest_min_size=suggest, message="ETA overschrijdt tijdbudget")
                RESUME_EVENT.clear()
                RESUME_EVENT.wait()
                set_status(state="hashing", message="")
                hash_start = time.time() - done * (avg_per_file or 0)

                if size < min_size_ref():
                    remaining_in_bucket = 1 + (len(paths) - (paths.index(p) + 1))
                    done += remaining_in_bucket
                    break

            try:
                digest = sha256_file(p)
                groups[(size, digest)].append(p)
            except (PermissionError, FileNotFoundError, OSError):
                pass
            finally:
                done += 1

    dup_groups: list[Group] = []
    for (size, digest), files in groups.items():
        if len(files) >= 2 and size >= min_size_ref():
            dup_groups.append(Group(size=size, sha256=digest, files=sorted(files)))
    dup_groups.sort(key=lambda g: g.size * (len(g.files) - 1), reverse=True)

    set_status(state="done", eta_seconds=0, elapsed_seconds=int(time.time() - started))
    return dup_groups

# ===================== FLASK APP (UI) =====================
app = Flask(__name__)

SCAN_ROOT = "C:\\"
CURRENT_MIN_SIZE = 10 * 1024 * 1024
TIME_BUDGET_SEC = 60 * 60
EXCLUDES: list[str] = []
INCLUDE_CLOUD = False
GROUPS: list[Group] = []
STARTED_AT: datetime = datetime.now()

README_HTML = """
<b>README (kort)</b><br>
• Zoekt <i>exacte</i> duplicaten (zelfde grootte + SHA-256) binnen <code>{{root}}</code>.<br>
• Verwijderen gaat <b>naar de prullenbak</b> (Send2Trash).<br>
• Standaard uitgesloten: systeemmappen + OneDrive-mappen. Cloud-placeholders worden <i>niet</i> gehashed (tenzij <code>--include-cloud</code>).<br>
• Tijdbudget: {{budget}} min. Als de ETA het budget overschrijdt kun je de <b>minimale bestandsgrootte</b> verhogen en direct hervatten.<br>
• CLI: <code>dup_finder.py --root C:\\ --min-size 10485760 --time-budget-min 60 [--no-excludes] [--add-exclude "pad"] [--include-cloud]</code>
"""

def min_size_ref() -> int:
    return CURRENT_MIN_SIZE

# --------- Pagina's ---------
PAGE_WAIT = """
<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<title>Duplicate Finder — Bezig…</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial;margin:1.5rem;}
 .card{border:1px solid #e5e5e5;border-radius:.6rem;padding:1rem;}
 .muted{color:#666}
 .mono{font-family:ui-monospace,Consolas,monospace;}
 .row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
 .bar{height:12px;background:#eee;border-radius:6px;overflow:hidden;margin:.4rem 0;position:relative}
 .bar>div{height:100%;background:#7da7ff;transition:width .3s ease}
 .bar.over>div{background:#ff8a8a}
 .indet {background: linear-gradient(90deg, #e5e9ff 0%, #cfd8ff 50%, #e5e9ff 100%); background-size:200% 100%; animation: move 1.2s linear infinite;}
 @keyframes move {0%{background-position:0% 0%}100%{background-position:-200% 0%}}
 .panel{border:1px solid #ffd7b5;background:#fff5ea;padding:.8rem;border-radius:.5rem;margin-top:1rem;}
 input[type=number]{width:12rem;padding:.3rem}
 button{padding:.35rem .65rem;border-radius:.35rem;border:1px solid #777;background:#f7f7f7;cursor:pointer}
 button.primary{background:#e8f0ff;border-color:#7da7ff}
</style>
<script>
async function tick(){
  const r = await fetch('/status'); const s = await r.json();
  // Tijdbalk (verbruik vs budget)
  const timePct = Math.floor(100 * (s.elapsed_seconds||0) / (s.budget_seconds||1));
  const timeBar = document.getElementById('timepct');
  timeBar.style.width = Math.min(timePct, 100) + '%';
  const timeWrap = document.getElementById('timebar');
  if (timePct > 100) timeWrap.classList.add('over'); else timeWrap.classList.remove('over');
  document.getElementById('timelabel').textContent = (timePct>100? '100+': timePct) + ' % van budget';

  // Hash balk
  const hashPct = (s.hash_total>0)? Math.floor(100*s.hash_done/s.hash_total) : 0;
  const hashBar = document.getElementById('hashpct');
  hashBar.style.width = hashPct + '%';
  document.getElementById('hashlabel').textContent = hashPct + ' %';

  // Walk-info
  document.getElementById('walk').textContent = s.walk_scanned.toLocaleString('nl-NL');
  document.getElementById('skip').textContent = s.walk_skipped.toLocaleString('nl-NL');
  // ETA
  let eta = '—';
  if (s.eta_seconds !== null && s.eta_seconds !== undefined){
    const m = Math.floor(s.eta_seconds/60), sec = s.eta_seconds%60;
    eta = (m>0? (m+' min '):'') + sec + ' s';
  } else if (s.state==='scanning') {
    eta = 'schatting volgt zodra hash-fase start…';
  }
  document.getElementById('eta').textContent = eta;
  // Min size
  document.getElementById('minsize').textContent = s.min_size.toLocaleString('nl-NL');
  // Tuning-panel
  const tuning = document.getElementById('tuning');
  if (s.state === 'needs_tuning') {
    tuning.style.display = 'block';
    document.getElementById('suggest').value = s.suggest_min_size || s.min_size*2;
  } else {
    tuning.style.display = 'none';
  }
  // Klaar?
  if (s.state === 'done') window.location.reload();
  else setTimeout(tick, 1000);
}

async function resume(action){
  let body = {action};
  if(action==='raise'){
    const val = parseInt(document.getElementById('suggest').value, 10);
    if (isNaN(val) || val < 1024*1024) { alert('Ongeldige waarde'); return; }
    body.new_min_size = val;
  }
  const r = await fetch('/resume', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const s = await r.json();
  if(!s.ok){ alert(s.error || 'Actie mislukt'); }
}
window.addEventListener('load', tick);
</script>
</head>
<body>
<h1>🔍 Bezig met scannen duplicate files (op grootte)…</h1>
<div class="card">
  <p class="muted">Root: <span class="mono">{{root}}</span> · Budget: {{budget_min}} min · Min. grootte: <b id="minsize">{{min_size}}</b> bytes</p>

  <div>
    <div class="row"><b>Tijd</b><span id="timelabel" class="muted">0 % van budget</span></div>
    <div id="timebar" class="bar"><div id="timepct" style="width:0%"></div></div>
  </div>

  <div style="margin-top:.5rem">
    <div class="row"><b>Hash-voortgang</b><span id="hashlabel" class="muted">0 %</span></div>
    <div class="bar"><div id="hashpct" style="width:0%"></div></div>
  </div>

  <div style="margin-top:.5rem">
    <div class="row"><b>Scan/walk</b><span class="muted">indeterminate</span></div>
    <div class="bar"><div class="indet" style="width:30%"></div></div>
  </div>

  <ul>
    <li>Bestanden beoordeeld: <b id="walk">0</b> · Overgeslagen: <b id="skip">0</b></li>
    <li>ETA: <b id="eta">—</b></li>
  </ul>

  <div id="tuning" class="panel" style="display:none">
    <div class="row">
      <div><b>ETA overschrijdt tijdbudget.</b> Opties:</div>
      <button onclick="resume('continue')">Toch doorgaan</button>
      <div class="row">
        <span>Of verhoog minimum naar (bytes):</span>
        <input id="suggest" type="number" step="1048576" min="1048576" value="">
        <button class="primary" onclick="resume('raise')">Verhoog & hervat</button>
      </div>
    </div>
    <div class="muted">Tip: 10 MB = 10 485 760 · 50 MB = 52 428 800 bytes.</div>
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
<title>Duplicaten gevonden</title>
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
function delAllSelected(){
  const picks = document.querySelectorAll('input.pick:checked');
  if(!picks.length){ alert('Geen selectie'); return; }
  if(!confirm('ALLE geselecteerde naar prullenbak verplaatsen?')) return;
  fetch('/delete-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:Array.from(picks).map(p=>p.value)})})
    .then(r=>r.json()).then(d=>{ if(d.ok){ picks.forEach(p=>p.closest('li').remove()); } else alert('Deels mislukt: '+(d.error||'')); });
}
</script>
</head>
<body>
<h1>✔️ Duplicaten binnen {{root}}</h1>
<p class="muted">Min. grootte: {{min_size}} bytes · Tijdsverbruik: {{elapsed_min}} min · Budget: {{budget_min}} min</p>

<h3>README</h3>
<div class="muted" style="margin-bottom:.75rem;">{{ readme|safe }}</div>

<table>
  <thead>
    <tr>
      <th style="width:12rem;">Grootte</th>
      <th style="width:7rem;">Aantal</th>
      <th>Bestanden</th>
    </tr>
  </thead>
  <tbody>
  {% for g in groups %}
    <tr>
      <td><b>{{ human(g.size) }}</b><div class="muted">{{ g.size }} bytes</div></td>
      <td><span class="badge">{{ g.files|length }}</span></td>
      <td>
        <details open>
          <summary class="group-head">SHA-256: <code>{{ g.sha256[:12] }}…</code> · Winst bij verwijderen {{ g.files|length - 1 }}x: <b>{{ human(g.size*(g.files|length-1)) }}</b></summary>
          <ul style="list-style:none;padding-left:0;margin:.35rem 0;">
          {% for f in g.files %}
            <li style="margin:.25rem 0;">
              <label><input type="checkbox" class="pick" value="{{ enc(f) }}"> {{ f }}</label>
              <div class="muted">
                <button class="btn" onclick="openFolder('{{ enc(f) }}')">Open map</button>
                <button class="btn danger" onclick="delOne('{{ enc(f) }}', this)">Naar prullenbak</button>
              </div>
            </li>
          {% endfor %}
          </ul>
          <button class="btn danger" onclick="delSelected(this.closest('details'))">Geselecteerde in deze groep verwijderen</button>
        </details>
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>

<div class="sticky">
  <button class="btn danger" onclick="delAllSelected()">Verwijder ALLE geselecteerde</button>
</div>
</body>
</html>
"""

app = Flask(__name__)

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
            budget_min=int(TIME_BUDGET_SEC/60),
            readme=render_template_string(README_HTML, root=SCAN_ROOT, budget=int(TIME_BUDGET_SEC/60)),
            human=human,
            enc=encode_path
        )
    else:
        return render_template_string(
            PAGE_WAIT,
            root=SCAN_ROOT,
            budget_min=int(TIME_BUDGET_SEC/60),
            min_size=s["min_size"],
            readme=render_template_string(README_HTML, root=SCAN_ROOT, budget=int(TIME_BUDGET_SEC/60))
        )

@app.get("/status")
def status():
    return jsonify(get_status())

@app.post("/resume")
def resume():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    global CURRENT_MIN_SIZE
    if action == "continue":
        RESUME_EVENT.set()
        return jsonify(ok=True)
    elif action == "raise":
        new_min = int(data.get("new_min_size", 0))
        if new_min < 1024*1024:
            return jsonify(ok=False, error="minsize te laag"), 400
        CURRENT_MIN_SIZE = new_min
        set_status(min_size=CURRENT_MIN_SIZE)
        RESUME_EVENT.set()
        return jsonify(ok=True)
    return jsonify(ok=False, error="onbekende actie"), 400

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

# ===================== MAIN =====================
SCAN_ROOT = "C:\\"
CURRENT_MIN_SIZE = 10 * 1024 * 1024
TIME_BUDGET_SEC = 60 * 60
EXCLUDES: list[str] = []
INCLUDE_CLOUD = False
GROUPS: list[Group] = []
STARTED_AT: datetime = datetime.now()

def main():
    parser = argparse.ArgumentParser(description="Zoek en beheer duplicaatbestanden (Windows).")
    parser.add_argument("--root", default="C:\\", help="Startmap (standaard: C:\\)")
    parser.add_argument("--min-size", type=int, default=10 * 1024 * 1024,
                        help="Minimum grootte (bytes). Standaard 10 MB.")
    parser.add_argument("--time-budget-min", type=int, default=60,
                        help="Maximale doorlooptijd in minuten (default 60).")
    parser.add_argument("--no-excludes", action="store_true", help="Systeem/OneDrive niet uitsluiten.")
    parser.add_argument("--add-exclude", action="append", default=[], help="Extra map uitsluiten (kan herhaald).")
    parser.add_argument("--include-cloud", action="store_true",
                        help="Ook OneDrive online-only bestanden hashen (kan download triggeren).")
    args = parser.parse_args()

    root = os.path.normpath(args.root)
    if not os.path.exists(root):
        print(f"Pad bestaat niet: {root}")
        sys.exit(1)

    excludes = [] if args.no_excludes else list(DEFAULT_EXCLUDES)
    excludes += args.add_exclude

    global SCAN_ROOT, CURRENT_MIN_SIZE, TIME_BUDGET_SEC, EXCLUDES, INCLUDE_CLOUD, STARTED_AT, GROUPS
    SCAN_ROOT = root
    CURRENT_MIN_SIZE = int(args.min_size)
    TIME_BUDGET_SEC = int(args.time_budget_min) * 60
    EXCLUDES = excludes
    INCLUDE_CLOUD = bool(args.include_cloud)
    STARTED_AT = datetime.now()
    GROUPS = []

    set_status(state="scanning", started=STARTED_AT.isoformat(),
               min_size=CURRENT_MIN_SIZE, budget_seconds=TIME_BUDGET_SEC,
               walk_scanned=0, walk_skipped=0, hash_total=0, hash_done=0, eta_seconds=None)

    def worker():
        global GROUPS
        try:
            GROUPS = find_duplicates_streaming(
                root=SCAN_ROOT,
                min_size_ref=lambda: CURRENT_MIN_SIZE,
                excludes=EXCLUDES,
                include_cloud=INCLUDE_CLOUD,
                time_budget_sec=TIME_BUDGET_SEC,
            )
        except Exception as e:
            set_status(state="error", error=str(e))

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    webbrowser.open("http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)

if __name__ == "__main__":
    if os.name != "nt":
        print("Let op: dit script is geoptimaliseerd voor Windows.")
    main()
