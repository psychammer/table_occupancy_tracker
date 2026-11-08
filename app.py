import json
import math
import time
import queue
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta
from contextlib import contextmanager, asynccontextmanager
import cv2
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from utilities.pipeline import run_pipeline, DEFAULT_SETTINGS

DB_PATH = "restaurant.db"
SNAPSHOT_EVERY_SEC = 60   # occupancy snapshot interval, feeds the busy-hours analytics

# ---------- shared state ----------
current_frame_jpeg = None
frame_version = 0
current_stats = {
    "frame": 0, "occupied_tables": 0, "needs_clearing_tables": 0, "total_tables": 0,
    "total_guests": 0, "total_seats": 0, "utilization": 0.0, "avg_seated_sec": 0, "tables": {}
}
current_settings = DEFAULT_SETTINGS.copy()
state_lock = threading.Lock()           # guards frame, stats and settings
restart_event = threading.Event()       # set => pipeline stops and is restarted with new settings
commands = queue.Queue()                # dashboard -> pipeline ("cleared", table)

events_buf = deque(maxlen=200)          # recent staff alerts
events_lock = threading.Lock()
event_seq = 0


# ---------- database ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT, guests INTEGER, duration_sec REAL, timestamp TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, occupied INTEGER, needs_clearing INTEGER,
                guests INTEGER, total_tables INTEGER
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(visits)")}
        for col in ("start_ts", "end_ts"):          # migrate older databases
            if col not in cols:
                conn.execute(f"ALTER TABLE visits ADD COLUMN {col} TEXT")

def log_visit(v: dict):
    with db() as conn:
        conn.execute(
            "INSERT INTO visits (table_name, guests, duration_sec, timestamp, start_ts, end_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (v["table_name"], v["guests"], v["duration_sec"], v["end_ts"], v["start_ts"], v["end_ts"]))

def push_event(e: dict):
    global event_seq
    with events_lock:
        event_seq += 1
        e["id"] = event_seq
        events_buf.append(e)


# ---------- background video worker ----------
def video_worker():
    global current_frame_jpeg, current_stats, frame_version
    while True:
        # Clear BEFORE snapshotting settings: a restart requested from now on is never lost.
        restart_event.clear()
        with state_lock:
            settings = current_settings.copy()
        try:
            for frame, info in run_pipeline(
                settings=settings,
                on_visit_end=log_visit,
                on_event=push_event,
                should_stop=restart_event.is_set,
                commands=commands,
            ):
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    with state_lock:
                        current_frame_jpeg = buf.tobytes()
                        current_stats = info
                        frame_version += 1
        except Exception as exc:   # keep the worker alive on bad settings / model errors
            print(f"[pipeline] crashed: {exc!r}; retrying in 3s")
            time.sleep(3)

def snapshot_worker():
    """Visits alone can't say 'how full were we at 7pm', so sample occupancy on a fixed interval."""
    while True:
        time.sleep(SNAPSHOT_EVERY_SEC)
        with state_lock:
            s = current_stats
            ready = s.get("frame", 0) > 0
            row = (s.get("occupied_tables", 0), s.get("needs_clearing_tables", 0),
                   s.get("total_guests", 0), s.get("total_tables", 0))
        if ready:
            try:
                with db() as conn:
                    conn.execute("INSERT INTO snapshots (ts, occupied, needs_clearing, guests, total_tables) "
                                 "VALUES (?, ?, ?, ?, ?)",
                                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *row))
            except sqlite3.Error as exc:
                print(f"[snapshot] failed: {exc!r}")

@asynccontextmanager
async def lifespan(app):
    init_db()
    threading.Thread(target=video_worker, daemon=True).start()
    threading.Thread(target=snapshot_worker, daemon=True).start()
    yield

app = FastAPI(title="TablePulse Restaurant Occupancy System", lifespan=lifespan)


# ---------- API ----------
@app.get("/video_feed")
def video_feed():
    def generate():
        last = -1
        while True:
            with state_lock:
                frame_bytes, ver = current_frame_jpeg, frame_version
            if frame_bytes and ver != last:     # only send new frames
                last = ver
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
            time.sleep(0.03)
    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/api/stats")
def get_stats():
    with state_lock:
        return current_stats

@app.get("/api/history")
def get_history():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, table_name, guests, duration_sec, COALESCE(end_ts, timestamp) "
            "FROM visits ORDER BY id DESC LIMIT 50").fetchall()
    return {"history": [{"id": r[0], "table_name": r[1], "guests": r[2],
                         "duration_sec": r[3], "timestamp": r[4]} for r in rows]}

@app.get("/api/analytics")
def analytics(days: int = Query(30, ge=1, le=365), tables_per_server: int = Query(4, ge=1, le=50)):
    """Busy-hours data: weekday x hour heatmap of average occupied tables + staffing hint."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    vts = "COALESCE(start_ts, timestamp)"
    with db() as conn:
        snaps = conn.execute(
            "SELECT CAST(strftime('%w', ts) AS INTEGER), CAST(strftime('%H', ts) AS INTEGER), "
            "AVG(occupied), AVG(guests), COUNT(*) FROM snapshots WHERE ts >= ? GROUP BY 1, 2", (since,)).fetchall()
        visits = conn.execute(
            f"SELECT CAST(strftime('%w', {vts}) AS INTEGER), CAST(strftime('%H', {vts}) AS INTEGER), COUNT(*) "
            f"FROM visits WHERE {vts} >= ? GROUP BY 1, 2", (since,)).fetchall()
        v_tot = conn.execute(
            f"SELECT COUNT(*), AVG(duration_sec), AVG(guests) FROM visits WHERE {vts} >= ?", (since,)).fetchone()
        s_tot = conn.execute(
            "SELECT MAX(occupied), MAX(total_tables), COUNT(*) FROM snapshots WHERE ts >= ?", (since,)).fetchone()

    heat = [[0.0] * 24 for _ in range(7)]       # avg occupied tables, index [weekday 0=Sun][hour]
    guests = [[0.0] * 24 for _ in range(7)]
    visit_counts = [[0] * 24 for _ in range(7)]
    for wd, hr, occ, g, _n in snaps:
        heat[wd][hr], guests[wd][hr] = round(occ, 2), round(g, 1)
    for wd, hr, n in visits:
        visit_counts[wd][hr] = n

    by_hour = []
    for hr in range(24):
        col = [heat[wd][hr] for wd in range(7) if heat[wd][hr] > 0]
        by_hour.append(round(sum(col) / len(col), 2) if col else 0.0)

    slots = sorted(((heat[wd][hr], wd, hr) for wd in range(7) for hr in range(24) if heat[wd][hr] > 0),
                   reverse=True)[:3]
    busiest = [{"weekday": wd, "hour": hr, "avg_occupied": occ,
                "servers_needed": math.ceil(occ / tables_per_server)} for occ, wd, hr in slots]
    return {
        "days": days, "tables_per_server": tables_per_server,
        "summary": {
            "visits": v_tot[0] or 0,
            "avg_duration_sec": round(v_tot[1] or 0, 1),
            "avg_party_size": round(v_tot[2] or 0, 1),
            "peak_occupied": s_tot[0] or 0,
            "total_tables": s_tot[1] or 0,
            "snapshots": s_tot[2] or 0,
        },
        "heatmap": heat, "guests": guests, "visit_counts": visit_counts,
        "by_hour": by_hour, "busiest": busiest,
    }

@app.get("/api/events/stream")
def events_stream(since: int | None = None, last_event_id: str | None = Header(default=None)):
    """Server-Sent Events. Browsers resend Last-Event-ID on reconnect, so no alert is missed."""
    with events_lock:
        latest = event_seq
    if last_event_id and last_event_id.isdigit():
        last = int(last_event_id)
    else:
        last = latest if since is None else since

    def generate():
        nonlocal last
        while True:
            with events_lock:
                new = [e for e in events_buf if e["id"] > last]
            for e in new:
                last = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e)}\n\n"
            if not new:
                yield ": keepalive\n\n"
            time.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/tables/{table_name}/cleared")
def mark_cleared(table_name: str):
    with state_lock:
        known = current_stats["tables"]
        if table_name not in known:
            raise HTTPException(404, "Unknown table")
    commands.put(("cleared", table_name))
    return {"status": "ok"}

class SettingsUpdate(BaseModel):
    video_path: str
    seats_per_table: int = Field(ge=1, le=20)
    occupy_seconds: float = Field(ge=0, le=600)
    free_seconds: float = Field(ge=1, le=3600)
    up_lock_seconds: float = Field(ge=0, le=600)
    down_lock_seconds: float = Field(ge=0, le=600)
    confidence: float = Field(gt=0, lt=1)
    check_after_seconds: float = Field(default=300, ge=10, le=7200)
    infer_fps: float = Field(default=4, ge=0.5, le=30)
    stabilize_camera: bool = Field(default=True)

@app.get("/api/settings")
def get_settings():
    with state_lock:
        return current_settings

@app.post("/api/settings")
def update_settings(new_s: SettingsUpdate):
    with state_lock:
        current_settings.update(new_s.model_dump())
        snapshot = current_settings.copy()
    restart_event.set()
    return {"status": "ok", "settings": snapshot}

@app.post("/api/restart")
def restart_video():
    restart_event.set()
    return {"status": "restarted"}


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>TablePulse — Restaurant Occupancy System</title>
  <style>
    :root { --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --green: #22c55e; --red: #ef4444; --accent: #38bdf8; }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: system-ui, -apple-system, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 20px; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid #334155; padding-bottom: 12px; }
    .tabs button { background: var(--card); color: var(--text); border: 1px solid #334155; padding: 8px 16px; border-radius: 6px; cursor: pointer; margin-left: 8px; }
    .tabs button.active { background: var(--accent); color: #0f172a; font-weight: 600; }
    .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
    .card { background: var(--card); padding: 16px; border-radius: 10px; border: 1px solid #334155; }
    .kpi-title { font-size: 0.85rem; color: var(--muted); margin-bottom: 6px; }
    .kpi-value { font-size: 1.8rem; font-weight: 700; }
    .main-grid { display: grid; grid-template-columns: 1.5fr 1fr; gap: 20px; }
    .video-box img { width: 100%; border-radius: 8px; display: block; background: #000; }
    .floor-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; padding: 16px 0; }
    .table-widget { background: #0f172a; border: 2px solid var(--green); border-radius: 12px; padding: 16px; text-align: center; position: relative; }
    .table-widget.occupied { border-color: var(--red); background: rgba(239, 68, 68, 0.12); }
    .seats-row { display: flex; justify-content: center; gap: 8px; margin: 8px 0; }
    .seat-dot { width: 14px; height: 14px; border-radius: 50%; background: #334155; }
    .seat-dot.taken { background: var(--red); box-shadow: 0 0 6px var(--red); }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; }
    .hidden { display: none; }
    .form-group { margin-bottom: 12px; }
    .form-group label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 0.9rem; }
    .form-group input { width: 100%; padding: 8px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #fff; }
    .an-kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 12px 0 18px; }
    .an-kpis .card { background: #0f172a; }
    .hour-chart { display: flex; align-items: flex-end; gap: 3px; height: 120px; margin: 10px 0 4px; }
    .hour-chart .bar { flex: 1; background: var(--accent); border-radius: 3px 3px 0 0; min-height: 2px; }
    .hour-labels { display: flex; gap: 3px; font-size: 0.65rem; color: var(--muted); }
    .hour-labels span { flex: 1; text-align: center; }
    .heat { display: grid; grid-template-columns: 38px repeat(24, 1fr); gap: 2px; margin-top: 10px; font-size: 0.7rem; color: var(--muted); }
    .heat .cell { height: 22px; border-radius: 3px; background: #0f172a; }
    #an-hint { margin: 12px 0; padding: 10px 12px; border-left: 4px solid var(--accent); background: #0f172a; border-radius: 4px; }
    .table-widget.needs-clearing { border-color: #f59e0b; background: rgba(245, 158, 11, 0.14); }
    .btn.small { padding: 4px 10px; font-size: 0.8rem; margin-top: 8px; }
    .right-col { display: flex; flex-direction: column; gap: 20px; }
    #alerts { list-style: none; max-height: 240px; overflow-y: auto; margin-top: 10px; }
    #alerts li { padding: 8px 10px; border-left: 4px solid #334155; margin-bottom: 6px; background: #0f172a; border-radius: 4px; font-size: 0.9rem; }
    #alerts li.check { border-color: #f59e0b; }
    #alerts li.clear { border-color: var(--red); }
    #alerts li.ok { border-color: var(--green); }
    #alerts .t { color: var(--muted); margin-right: 8px; font-size: 0.8rem; }
    .btn { background: var(--accent); color: #0f172a; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; }
  </style>
</head>
<body>
  <header>
    <h2>🍽️ TablePulse — Live Restaurant Occupancy</h2>
    <div class="tabs">
      <button id="tab-live" class="active" onclick="showTab('live')">Live Floor</button>
      <button id="tab-history" onclick="showTab('history')">History & Analytics</button>
      <button id="tab-settings" onclick="showTab('settings')">Settings</button>
      <button onclick="restartVideo()">↻ Restart Video</button>
    </div>
  </header>

  <div class="kpi-grid">
    <div class="card"><div class="kpi-title">Occupied Tables</div><div class="kpi-value" id="kpi-tables">0 / 0</div></div>
    <div class="card"><div class="kpi-title">Guests Seated</div><div class="kpi-value" id="kpi-guests">0</div></div>
    <div class="card"><div class="kpi-title">Seat Utilization</div><div class="kpi-value" id="kpi-util">0%</div></div>
    <div class="card"><div class="kpi-title">Avg Seated Time</div><div class="kpi-value" id="kpi-time">0s</div></div>
  </div>

  <!-- LIVE TAB -->
  <div id="view-live" class="main-grid">
    <div class="card video-box">
      <h3 style="margin-bottom:10px;">Live CCTV Stream</h3>
      <img src="/video_feed" alt="Live stream" />
    </div>
    <div class="right-col">
      <div class="card">
        <h3>Interactive Floor Plan</h3>
        <div id="floor-plan" class="floor-grid"></div>
      </div>
      <div class="card">
        <h3>Staff Alerts</h3>
        <button class="btn small" onclick="enableNotifications()">🔔 Enable sound &amp; notifications</button>
        <ul id="alerts"></ul>
      </div>
    </div>
  </div>

  <!-- HISTORY TAB -->
  <div id="view-history" class="card hidden">
    <h3>Busy Hours &amp; Staffing <span style="color:var(--muted);font-weight:400;font-size:0.8rem;">(last 30 days)</span></h3>
    <div class="an-kpis" id="an-kpis"></div>
    <div id="an-hint"></div>
    <div style="color:var(--muted);font-size:0.85rem;">Average occupied tables by hour of day</div>
    <div class="hour-chart" id="an-hours"></div>
    <div class="hour-labels" id="an-hour-labels"></div>
    <div style="color:var(--muted);font-size:0.85rem;margin-top:18px;">Weekday &times; hour heatmap (darker = busier)</div>
    <div class="heat" id="an-heat"></div>
    <h3 style="margin-top:26px;">Completed Table Visits</h3>
    <table>
      <thead><tr><th>ID</th><th>Table</th><th>Guests</th><th>Duration (s)</th><th>Timestamp</th></tr></thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>

  <!-- SETTINGS TAB -->
  <div id="view-settings" class="card hidden" style="max-width: 550px;">
    <h3 style="margin-bottom: 14px;">Pipeline & CCTV Settings</h3>
    <div class="form-group"><label><input id="set-stab" type="checkbox" style="width:auto; margin-right:8px;"> Lock Polygons to Camera Motion (ORB Stabilizer)</label></div>
    <div class="form-group"><label>Video Path or RTSP/HTTP CCTV URL</label><input id="set-video" type="text"></div>
    <div class="form-group"><label>Seats Per Table</label><input id="set-seats" type="number"></div>
    <div class="form-group"><label>Occupy Timer (seconds)</label><input id="set-occ" type="number" step="0.5"></div>
    <div class="form-group"><label>Table Empty Timer before Guests Count as Left (seconds)</label><input id="set-free" type="number" step="0.5"></div>
    <div class="form-group"><label>Check-on-Table Alert After (seconds)</label><input id="set-check" type="number" step="10"></div>
    <div class="form-group"><label>Inference Rate (frames per second)</label><input id="set-infer" type="number" step="0.5"></div>
    <div class="form-group"><label>Time to Accept Higher Count (seconds)</label><input id="set-up" type="number" step="0.5"></div>
    <div class="form-group"><label>Time to Accept Lower Count (seconds)</label><input id="set-down" type="number" step="0.5"></div>
    <div class="form-group"><label>YOLO Confidence Threshold</label><input id="set-conf" type="number" step="0.05"></div>
    <button class="btn" onclick="saveSettings()">Save & Apply Settings</button>
  </div>

  <script>
    function showTab(name) {
      ['live', 'history', 'settings'].forEach(t => {
        document.getElementById('view-' + t).classList.toggle('hidden', t !== name);
        document.getElementById('tab-' + t).classList.toggle('active', t === name);
      });
      if (name === 'history') { loadHistory(); loadAnalytics(); }
      if (name === 'settings') loadSettings();
    }

    async function pollStats() {
      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        document.getElementById('kpi-tables').innerText = `${data.occupied_tables} / ${data.total_tables}`;
        document.getElementById('kpi-guests').innerText = data.total_guests;
        document.getElementById('kpi-util').innerText = `${data.utilization}%`;
        document.getElementById('kpi-time').innerText = `${data.avg_seated_sec}s`;

        const floor = document.getElementById('floor-plan');
        floor.innerHTML = '';
        Object.entries(data.tables).forEach(([tName, t]) => {
          let seatsHtml = '<div class="seats-row">';
          for (let i = 1; i <= t.total_seats; i++) {
            const taken = t.taken_seats.includes(i) ? 'taken' : '';
            seatsHtml += `<div class="seat-dot ${taken}" title="Seat ${i}"></div>`;
          }
          seatsHtml += '</div>';
          floor.innerHTML += `
            <div class="table-widget ${t.occupied ? 'occupied' : ''} ${t.state === 'needs_clearing' ? 'needs-clearing' : ''}">
              <strong>${tName}</strong>
              ${seatsHtml}
              <div style="font-size:0.85rem;color:#cbd5e1;">
                ${t.occupied ? `👥 ${t.guests} Guests<br>⏱️ ${fmtDur(t.duration_sec)}${t.check_due ? ' ⚠️ check in' : ''}`
                  : t.state === 'needs_clearing' ? `🧹 Needs clearing (${fmtDur(t.dirty_sec)})<br><button class="btn small" data-cleared="${encodeURIComponent(tName)}">Mark cleared</button>`
                  : 'Available'}
              </div>
            </div>`;
        });
      } catch (e) {}
    }

    async function loadHistory() {
      const res = await fetch('/api/history');
      const data = await res.json();
      const tbody = document.getElementById('history-body');
      tbody.innerHTML = data.history.map(r =>
        `<tr><td>#${r.id}</td><td>${r.table_name}</td><td>${r.guests}</td><td>${r.duration_sec}s</td><td>${r.timestamp}</td></tr>`
      ).join('');
    }

    const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const hourLabel = h => `${String(h).padStart(2, '0')}:00`;
    function el(tag, cls, text) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text !== undefined) e.textContent = text;
      return e;
    }

    async function loadAnalytics() {
      const a = await (await fetch('/api/analytics')).json();
      const s = a.summary;

      const kpis = document.getElementById('an-kpis');
      kpis.replaceChildren();
      [['Visits', s.visits], ['Avg Seated Time', `${Math.round(s.avg_duration_sec / 60)} min`],
       ['Avg Party Size', s.avg_party_size], ['Peak Tables Used', `${s.peak_occupied} / ${s.total_tables}`]]
        .forEach(([title, val]) => {
          const c = el('div', 'card');
          c.append(el('div', 'kpi-title', title), el('div', 'kpi-value', String(val)));
          kpis.append(c);
        });

      const hint = document.getElementById('an-hint');
      if (a.busiest.length) {
        const parts = a.busiest.map(b =>
          `${DAYS[b.weekday]} ${hourLabel(b.hour)} (avg ${b.avg_occupied} tables → ${b.servers_needed} server${b.servers_needed > 1 ? 's' : ''})`);
        hint.textContent = `Busiest slots: ${parts.join('; ')}. Assumes ${a.tables_per_server} tables per server.`;
      } else {
        hint.textContent = 'Not enough data yet. Occupancy is sampled every minute, so check back after the camera has run for a while.';
      }

      const maxH = Math.max(...a.by_hour, 0.01);
      const bars = document.getElementById('an-hours'), labels = document.getElementById('an-hour-labels');
      bars.replaceChildren(); labels.replaceChildren();
      a.by_hour.forEach((v, h) => {
        const b = el('div', 'bar'); b.style.height = `${(v / maxH) * 100}%`;
        b.title = `${hourLabel(h)}: ${v} tables on average`;
        bars.append(b);
        labels.append(el('span', '', h % 3 === 0 ? String(h) : ''));
      });

      const maxHeat = Math.max(...a.heatmap.flat(), 0.01);
      const heat = document.getElementById('an-heat');
      heat.replaceChildren(el('div'));
      for (let h = 0; h < 24; h++) heat.append(el('div', '', h % 3 === 0 ? String(h) : ''));
      a.heatmap.forEach((row, wd) => {
        heat.append(el('div', '', DAYS[wd]));
        row.forEach((v, h) => {
          const c = el('div', 'cell');
          if (v > 0) c.style.background = `rgba(56, 189, 248, ${0.15 + 0.85 * v / maxHeat})`;
          c.title = `${DAYS[wd]} ${hourLabel(h)}: ${v} tables avg, ${a.visit_counts[wd][h]} visits started`;
          heat.append(c);
        });
      });
    }

    async function loadSettings() {
      const res = await fetch('/api/settings');
      const s = await res.json();
      document.getElementById('set-video').value = s.video_path;
      document.getElementById('set-seats').value = s.seats_per_table;
      document.getElementById('set-occ').value = s.occupy_seconds;
      document.getElementById('set-free').value = s.free_seconds;
      document.getElementById('set-up').value = s.up_lock_seconds;
      document.getElementById('set-down').value = s.down_lock_seconds;
      document.getElementById('set-conf').value = s.confidence;
      document.getElementById('set-check').value = s.check_after_seconds;
      document.getElementById('set-infer').value = s.infer_fps;
      document.getElementById('set-stab').checked = s.stabilize_camera !== false;
    }

    async function saveSettings() {
      const payload = {
        video_path: document.getElementById('set-video').value,
        seats_per_table: parseInt(document.getElementById('set-seats').value),
        occupy_seconds: parseFloat(document.getElementById('set-occ').value),
        free_seconds: parseFloat(document.getElementById('set-free').value),
        up_lock_seconds: parseFloat(document.getElementById('set-up').value),
        down_lock_seconds: parseFloat(document.getElementById('set-down').value),
        confidence: parseFloat(document.getElementById('set-conf').value),
        check_after_seconds: parseFloat(document.getElementById('set-check').value),
        infer_fps: parseFloat(document.getElementById('set-infer').value),
        stabilize_camera: document.getElementById('set-stab').checked
      };
      await fetch('/api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
      alert('Settings applied & stream restarted!');
      showTab('live');
    }

    async function restartVideo() {
      await fetch('/api/restart', { method: 'POST' });
    }

    function fmtDur(sec) { const m = Math.floor(sec / 60), s = sec % 60; return `${m}:${String(s).padStart(2, '0')}`; }

    document.getElementById('floor-plan').addEventListener('click', async (ev) => {
      const name = ev.target.dataset && ev.target.dataset.cleared;
      if (name) await fetch(`/api/tables/${name}/cleared`, { method: 'POST' });
    });

    // ---- staff alerts (Server-Sent Events) ----
    let audioCtx = null;
    function enableNotifications() {
      try { audioCtx = audioCtx || new AudioContext(); } catch (e) {}
      if ('Notification' in window) Notification.requestPermission();
    }
    function beep() {
      if (!audioCtx) return;
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.connect(g); g.connect(audioCtx.destination);
      g.gain.value = 0.1; o.frequency.value = 880;
      o.start(); o.stop(audioCtx.currentTime + 0.25);
    }
    const ALERT_TEXT = {
      SEATED: e => ['ok', `${e.table}: guests seated (${e.guests})`],
      CHECK_ON_TABLE: e => ['check', `Check on ${e.table} (${Math.round(e.seated_sec / 60)} min seated)`],
      NEEDS_CLEARING: e => ['clear', `${e.table} needs clearing`],
      CLEARED: e => ['ok', `${e.table} cleared`],
    };
    function addAlert(e) {
      const fn = ALERT_TEXT[e.type];
      if (!fn) return;
      const [cls, text] = fn(e);
      const li = document.createElement('li');
      li.className = cls;
      const t = document.createElement('span'); t.className = 't'; t.textContent = e.time;
      li.append(t, document.createTextNode(text));
      const list = document.getElementById('alerts');
      list.prepend(li);
      while (list.children.length > 50) list.lastChild.remove();
      const fresh = Date.now() / 1000 - e.ts < 30;   // don't notify for replayed history
      if (fresh && (e.type === 'CHECK_ON_TABLE' || e.type === 'NEEDS_CLEARING')) {
        beep();
        if ('Notification' in window && Notification.permission === 'granted') new Notification('TablePulse', { body: text });
      }
    }
    const es = new EventSource('/api/events/stream?since=0');
    es.onmessage = ev => addAlert(JSON.parse(ev.data));

    setInterval(pollStats, 1000);
    pollStats();
  </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return DASHBOARD_HTML

if __name__ == "__main__":
    # Bind to localhost until authentication is added (this serves a live camera feed).
    uvicorn.run(app, host="127.0.0.1", port=8020)