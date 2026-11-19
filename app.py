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
from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
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
                table_name TEXT, guests INTEGER, duration_sec REAL, timestamp TEXT,
                start_ts TEXT, end_ts TEXT, clear_duration_sec REAL DEFAULT 0
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clearings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                clear_duration_sec REAL NOT NULL,
                auto_cleared INTEGER DEFAULT 0,
                ts TEXT NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS idle_times (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                vacant_duration_sec REAL NOT NULL,
                ts TEXT NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, occupied INTEGER, needs_clearing INTEGER,
                guests INTEGER, total_tables INTEGER
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_idle_ts ON idle_times(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clearings_ts ON clearings(ts)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(visits)")}
        for col, col_type in (("start_ts", "TEXT"), ("end_ts", "TEXT"), ("clear_duration_sec", "REAL DEFAULT 0")):
            if col not in cols:
                conn.execute(f"ALTER TABLE visits ADD COLUMN {col} {col_type}")

def log_visit(v: dict):
    with db() as conn:
        conn.execute(
            "INSERT INTO visits (table_name, guests, duration_sec, timestamp, start_ts, end_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (v["table_name"], v["guests"], v["duration_sec"], v["end_ts"], v["start_ts"], v["end_ts"]))

def log_clearing(c: dict):
    with db() as conn:
        conn.execute(
            "INSERT INTO clearings (table_name, clear_duration_sec, auto_cleared, ts) VALUES (?, ?, ?, ?)",
            (c["table_name"], c["clear_duration_sec"], 1 if c.get("auto") else 0, c["ts"])
        )
        # Also attach clear_duration_sec to the most recent visit for that table
        conn.execute("""
            UPDATE visits
            SET clear_duration_sec = ?
            WHERE id = (
                SELECT id FROM visits
                WHERE table_name = ?
                ORDER BY id DESC LIMIT 1
            )
        """, (c["clear_duration_sec"], c["table_name"]))

def push_event(e: dict):
    global event_seq
    with events_lock:
        event_seq += 1
        e["id"] = event_seq
        events_buf.append(e)
    if e.get("type") == "SEATED" and e.get("vacant_duration_sec", 0) > 0:
        with db() as conn:
            conn.execute(
                "INSERT INTO idle_times (table_name, vacant_duration_sec, ts) VALUES (?, ?, ?)",
                (e["table"], e["vacant_duration_sec"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )


# ---------- background video worker ----------
def video_worker():
    global current_frame_jpeg, current_stats, frame_version
    while True:
        restart_event.clear()
        with state_lock:
            settings = current_settings.copy()
        try:
            for frame, info in run_pipeline(
                settings=settings,
                on_visit_end=log_visit,
                on_table_cleared=log_clearing,
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
        except Exception as exc:
            print(f"[pipeline] crashed: {exc!r}; retrying in 3s")
            time.sleep(3)

def snapshot_worker():
    """Sample occupancy on a fixed interval to feed busy-hours analytics."""
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- API ----------
@app.get("/video_feed")
def video_feed():
    def generate():
        last = -1
        while True:
            with state_lock:
                frame_bytes, ver = current_frame_jpeg, frame_version
            if frame_bytes and ver != last:
                last = ver
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
            time.sleep(0.03)
    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/api/snapshot.jpg")
def get_snapshot():
    with state_lock:
        frame_bytes = current_frame_jpeg
    if not frame_bytes:
        return Response(status_code=204)
    return Response(content=frame_bytes, media_type="image/jpeg")

@app.get("/api/stats")
def get_stats():
    with state_lock:
        return current_stats

@app.get("/api/history")
def get_history():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, table_name, guests, duration_sec, COALESCE(clear_duration_sec, 0), COALESCE(end_ts, timestamp) "
            "FROM visits ORDER BY id DESC LIMIT 50").fetchall()
    return {"history": [{"id": r[0], "table_name": r[1], "guests": r[2],
                         "duration_sec": r[3], "clear_duration_sec": round(r[4] or 0, 1),
                         "timestamp": r[5]} for r in rows]}

@app.get("/api/analytics")
def analytics(days: int = Query(30, ge=1, le=365), tables_per_server: int = Query(4, ge=1, le=50)):
    """
    Returns busy-hours heatmap, staffing hints, per-table turnover speed rankings,
    seating fill efficiency, and the PHP Turnover & Revenue Simulator.
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    vts = "COALESCE(start_ts, timestamp)"

    with state_lock:
        cfg = current_settings.copy()
        live_total_tables = current_stats.get("total_tables", 0) or 10

    seats_per_table = cfg.get("seats_per_table", 4)
    target_dining_min = cfg.get("target_dining_minutes", 75.0)
    avg_spend_php = cfg.get("avg_spend_php", 500.0)
    operating_hours = cfg.get("operating_hours", 10.0)

    with db() as conn:
        snaps = conn.execute(
            "SELECT CAST(strftime('%w', ts) AS INTEGER), CAST(strftime('%H', ts) AS INTEGER), "
            "AVG(occupied), AVG(guests), COUNT(*) FROM snapshots WHERE ts >= ? GROUP BY 1, 2", (since,)).fetchall()
        visits = conn.execute(
            f"SELECT CAST(strftime('%w', {vts}) AS INTEGER), CAST(strftime('%H', {vts}) AS INTEGER), COUNT(*) "
            f"FROM visits WHERE {vts} >= ? GROUP BY 1, 2", (since,)).fetchall()
        v_tot = conn.execute(
            f"SELECT COUNT(*), AVG(duration_sec), AVG(guests), AVG(COALESCE(clear_duration_sec, 0)) "
            f"FROM visits WHERE {vts} >= ?", (since,)).fetchone()
        c_tot = conn.execute(
            "SELECT COUNT(*), AVG(clear_duration_sec), MIN(clear_duration_sec), MAX(clear_duration_sec) "
            "FROM clearings WHERE ts >= ?", (since,)).fetchone()
        s_tot = conn.execute(
            "SELECT MAX(occupied), MAX(total_tables), COUNT(*) FROM snapshots WHERE ts >= ?", (since,)).fetchone()

        # Per-table turnover & seating efficiency breakdown
        per_table_rows = conn.execute(f"""
            SELECT
                table_name,
                COUNT(*) AS turns,
                AVG(duration_sec) AS avg_dining_sec,
                AVG(COALESCE(clear_duration_sec, 0)) AS avg_clear_sec,
                AVG(guests) AS avg_guests,
                SUM(CASE WHEN guests <= ? THEN 1 ELSE 0 END) AS under_seated_visits
            FROM visits
            WHERE {vts} >= ?
            GROUP BY table_name
        """, (max(1, seats_per_table // 2), since)).fetchall()

        active_days = conn.execute(
            f"SELECT COUNT(DISTINCT DATE({vts})) FROM visits WHERE {vts} >= ?", (since,)
        ).fetchone()[0] or 1

        idle_rows = conn.execute(
            "SELECT table_name, AVG(vacant_duration_sec) FROM idle_times WHERE ts >= ? GROUP BY table_name",
            (since,),
        ).fetchall()
        idle_by_table = {r[0]: round(r[1] or 0.0, 1) for r in idle_rows}
        overall_avg_idle_sec = round(
            conn.execute("SELECT AVG(vacant_duration_sec) FROM idle_times WHERE ts >= ?", (since,)).fetchone()[0] or 0.0, 1
        )

    heat = [[0.0] * 24 for _ in range(7)]
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

    # Build per-table turnover list (sorted slowest cycle time first)
    table_turnover = []
    seating_opportunities = []
    for row in per_table_rows:
        t_name, turns, avg_din, avg_clr, avg_g, under_visits = row
        avg_din = round(avg_din or 0.0, 1)
        avg_clr = round(avg_clr or 0.0, 1)
        cycle_sec = round(avg_din + avg_clr, 1)
        avg_g = round(avg_g or 0.0, 2)
        seat_eff_pct = round((avg_g / max(1, seats_per_table)) * 100.0, 1)
        under_pct = round(((under_visits or 0) / max(1, turns)) * 100.0, 1)
        turns_per_day = round(turns / active_days, 2)
        turns_per_hour = round(turns_per_day / max(1.0, operating_hours), 2)
        avg_idle_sec = idle_by_table.get(t_name, 0.0)

        item = {
            "table_name": t_name,
            "turns": turns,
            "avg_dining_sec": avg_din,
            "avg_clear_sec": avg_clr,
            "avg_cycle_sec": cycle_sec,
            "avg_guests": avg_g,
            "seat_efficiency_pct": seat_eff_pct,
            "small_party_pct": under_pct,
            "turns_per_day": turns_per_day,
            "turns_per_hour": turns_per_hour,
            "avg_idle_sec": avg_idle_sec,
        }
        table_turnover.append(item)

        if seat_eff_pct < 60.0 and turns >= 1:
            seating_opportunities.append(
                f"{t_name} averages {avg_g} guests on a {seats_per_table}-seat table ({seat_eff_pct}% seat fill). "
                f"Consider splitting into 2-tops or prioritizing larger parties here."
            )

    table_turnover.sort(key=lambda x: x["avg_cycle_sec"], reverse=True)
    slowest_tables = table_turnover[:3]

    # Turnover & Revenue Impact Simulator (PHP)
    # Uses observed average cycle time if available, or defaults to 90 min baseline
    observed_dining_min = round((v_tot[1] or 5400.0) / 60.0, 1) if (v_tot[0] or 0) > 0 else 90.0
    observed_clear_min = round((c_tot[1] or v_tot[3] or 0.0) / 60.0, 1)
    baseline_cycle_min = max(15.0, observed_dining_min + observed_clear_min) if (v_tot[0] or 0) > 0 else 90.0
    target_cycle_min = max(10.0, target_dining_min)

    num_tables = s_tot[1] or live_total_tables or 10
    avg_party = round(v_tot[2] or 2.5, 2)
    operating_minutes = operating_hours * 60.0

    current_turns_per_table = round(operating_minutes / baseline_cycle_min, 2)
    target_turns_per_table = round(operating_minutes / target_cycle_min, 2)

    current_daily_sessions = round(num_tables * current_turns_per_table, 1)
    target_daily_sessions = round(num_tables * target_turns_per_table, 1)
    extra_daily_sessions = round(max(0.0, target_daily_sessions - current_daily_sessions), 1)
    extra_daily_guests = round(extra_daily_sessions * avg_party, 1)

    # Revenue calculation: each customer spends avg_spend_php
    current_daily_rev_php = round(current_daily_sessions * avg_party * avg_spend_php, 2)
    target_daily_rev_php = round(target_daily_sessions * avg_party * avg_spend_php, 2)
    extra_daily_rev_php = round(max(0.0, target_daily_rev_php - current_daily_rev_php), 2)
    extra_monthly_rev_php = round(extra_daily_rev_php * 30.0, 2)

    overall_seat_eff_pct = round(((v_tot[2] or 0) / max(1, seats_per_table)) * 100.0, 1) if (v_tot[0] or 0) > 0 else 0.0

    return {
        "days": days,
        "tables_per_server": tables_per_server,
        "summary": {
            "visits": v_tot[0] or 0,
            "avg_duration_sec": round(v_tot[1] or 0, 1),
            "avg_clear_sec": round(c_tot[1] or v_tot[3] or 0, 1),
            "fastest_clear_sec": round(c_tot[2] or 0, 1),
            "slowest_clear_sec": round(c_tot[3] or 0, 1),
            "total_clearings": c_tot[0] or 0,
            "avg_party_size": round(v_tot[2] or 0, 1),
            "seat_efficiency_pct": overall_seat_eff_pct,
            "peak_occupied": s_tot[0] or 0,
            "total_tables": num_tables,
            "snapshots": s_tot[2] or 0,
            "avg_idle_sec": overall_avg_idle_sec,
            "turnover_rate_per_day": round((v_tot[0] or 0) / max(1, num_tables) / active_days, 2),
            "turnover_rate_per_hour": round(((v_tot[0] or 0) / max(1, num_tables) / active_days) / max(1.0, operating_hours), 2),
        },
        "turnover_ranking": table_turnover,
        "slowest_tables": slowest_tables,
        "seating_opportunities": seating_opportunities,
        "turnover_simulator": {
            "tables": num_tables,
            "operating_hours": operating_hours,
            "avg_spend_php": avg_spend_php,
            "avg_party_size": avg_party,
            "current_cycle_min": baseline_cycle_min,
            "target_cycle_min": target_cycle_min,
            "current_daily_sessions": current_daily_sessions,
            "target_daily_sessions": target_daily_sessions,
            "extra_daily_sessions": extra_daily_sessions,
            "extra_daily_guests": extra_daily_guests,
            "current_daily_revenue_php": current_daily_rev_php,
            "target_daily_revenue_php": target_daily_rev_php,
            "extra_daily_revenue_php": extra_daily_rev_php,
            "extra_monthly_revenue_php": extra_monthly_rev_php,
        },
        "heatmap": heat,
        "guests": guests,
        "visit_counts": visit_counts,
        "by_hour": by_hour,
        "busiest": busiest,
    }

@app.get("/api/events")
def get_recent_events(since: int = Query(0, ge=0)):
    """Lightweight JSON polling endpoint for the Flutter mobile app."""
    with events_lock:
        new_events = [e for e in events_buf if e["id"] > since]
    return {"events": new_events}

@app.get("/api/events/stream")
def events_stream(since: int | None = None, last_event_id: str | None = Header(default=None)):
    """Server-Sent Events for real-time staff notifications."""
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
    clear_alert_seconds: float = Field(default=180, ge=10, le=3600)
    vacant_alert_seconds: float = Field(default=900, ge=30, le=14400)
    target_dining_minutes: float = Field(default=75, ge=15, le=300)
    avg_spend_php: float = Field(default=500, ge=1, le=100000)
    operating_hours: float = Field(default=10, ge=1, le=24)
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
  <title>TablePulse — Restaurant Occupancy & Turnover System</title>
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
    .table-widget.needs-clearing { border-color: #f59e0b; background: rgba(245, 158, 11, 0.14); }
    .seats-row { display: flex; justify-content: center; gap: 8px; margin: 8px 0; }
    .seat-dot { width: 14px; height: 14px; border-radius: 50%; background: #334155; }
    .seat-dot.taken { background: var(--red); box-shadow: 0 0 6px var(--red); }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; }
    .hidden { display: none; }
    .form-group { margin-bottom: 12px; }
    .form-group label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 0.9rem; }
    .form-group input { width: 100%; padding: 8px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #fff; }
    .an-kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin: 12px 0 18px; }
    .an-kpis .card { background: #0f172a; }
    .sim-box { background: rgba(34, 197, 94, 0.1); border: 1px solid var(--green); padding: 14px; border-radius: 8px; margin: 14px 0; }
    .hour-chart { display: flex; align-items: flex-end; gap: 3px; height: 120px; margin: 10px 0 4px; }
    .hour-chart .bar { flex: 1; background: var(--accent); border-radius: 3px 3px 0 0; min-height: 2px; }
    .hour-labels { display: flex; gap: 3px; font-size: 0.65rem; color: var(--muted); }
    .hour-labels span { flex: 1; text-align: center; }
    .heat { display: grid; grid-template-columns: 38px repeat(24, 1fr); gap: 2px; margin-top: 10px; font-size: 0.7rem; color: var(--muted); }
    .heat .cell { height: 22px; border-radius: 3px; background: #0f172a; }
    #an-hint { margin: 12px 0; padding: 10px 12px; border-left: 4px solid var(--accent); background: #0f172a; border-radius: 4px; }
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
    <h2>🍽️ TablePulse — Live Restaurant Occupancy &amp; Turnover</h2>
    <div class="tabs">
      <button id="tab-live" class="active" onclick="showTab('live')">Live Floor</button>
      <button id="tab-history" onclick="showTab('history')">Turnover &amp; Analytics</button>
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

  <!-- HISTORY & TURNOVER TAB -->
  <div id="view-history" class="card hidden">
    <h3>Table Turnover, Bussing Speed &amp; Revenue Impact <span style="color:var(--muted);font-weight:400;font-size:0.8rem;">(last 30 days)</span></h3>
    <div class="an-kpis" id="an-kpis"></div>
    <div class="sim-box" id="an-sim"></div>
    <div id="an-hint"></div>

    <h3 style="margin-top:20px;">Table Turnover Ranking (Slowest First) &amp; Seating Efficiency</h3>
    <table>
        <thead>
            <tr>
                <th>Table</th>
                <th>Turns (Rate)</th>
                <th>Avg Dining</th>
                <th>Avg Clear</th>
                <th>Avg Idle</th>
                <th>Total Cycle</th>
                <th>Avg Guests</th>
                <th>Seat Fill %</th>
            </tr>
        </thead>
      <tbody id="turnover-body"></tbody>
    </table>

    <div style="color:var(--muted);font-size:0.85rem;margin-top:20px;">Average occupied tables by hour of day</div>
    <div class="hour-chart" id="an-hours"></div>
    <div class="hour-labels" id="an-hour-labels"></div>
    <div style="color:var(--muted);font-size:0.85rem;margin-top:18px;">Weekday &times; hour heatmap (darker = busier)</div>
    <div class="heat" id="an-heat"></div>
    <h3 style="margin-top:26px;">Completed Table Visits</h3>
    <table>
      <thead><tr><th>ID</th><th>Table</th><th>Guests</th><th>Dining Duration</th><th>Clear Speed</th><th>Timestamp</th></tr></thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>

  <!-- SETTINGS TAB -->
  <div id="view-settings" class="card hidden" style="max-width: 550px;">
    <h3 style="margin-bottom: 14px;">Pipeline, Turnover Alerts &amp; Simulator Settings</h3>
    <div class="form-group"><label><input id="set-stab" type="checkbox" style="width:auto; margin-right:8px;"> Lock Polygons to Camera Motion (ORB Stabilizer)</label></div>
    <div class="form-group"><label>Video Path or RTSP/HTTP CCTV URL</label><input id="set-video" type="text"></div>
    <div class="form-group"><label>Seats Per Table</label><input id="set-seats" type="number"></div>
    <div class="form-group"><label>Occupy Timer (seconds)</label><input id="set-occ" type="number" step="0.5"></div>
    <div class="form-group"><label>Table Empty Timer before Guests Count as Left (seconds)</label><input id="set-free" type="number" step="0.5"></div>
    <div class="form-group"><label>Check-on-Table Alert After (seconds)</label><input id="set-check" type="number" step="10"></div>
    <div class="form-group"><label>Alert When Dirty Table Uncleared After (seconds)</label><input id="set-clear-alert" type="number" step="10"></div>
    <div class="form-group"><label>Alert When Free Table Vacant Too Long (seconds)</label><input id="set-vacant-alert" type="number" step="30"></div>
    <div class="form-group"><label>Target Dining Duration for Simulator (minutes)</label><input id="set-target-min" type="number" step="5"></div>
    <div class="form-group"><label>Average Customer Spend (PHP)</label><input id="set-spend" type="number" step="50"></div>
    <div class="form-group"><label>Daily Operating Hours</label><input id="set-hours" type="number" step="1"></div>
    <div class="form-group"><label>Inference Rate (frames per second)</label><input id="set-infer" type="number" step="0.5"></div>
    <div class="form-group"><label>Time to Accept Higher Count (seconds)</label><input id="set-up" type="number" step="0.5"></div>
    <div class="form-group"><label>Time to Accept Lower Count (seconds)</label><input id="set-down" type="number" step="0.5"></div>
    <div class="form-group"><label>YOLO Confidence Threshold</label><input id="set-conf" type="number" step="0.05"></div>
    <button class="btn" onclick="saveSettings()">Save &amp; Apply Settings</button>
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
                  : t.state === 'needs_clearing' ? `🧹 Needs clearing (${fmtDur(t.dirty_sec)})${t.clearing_overdue ? ' 🚨 OVERDUE' : ''}<br><button class="btn small" data-cleared="${encodeURIComponent(tName)}">Mark cleared</button>`
                  : `Available (${fmtDur(t.vacant_sec || 0)})${t.vacant_overdue ? ' ⏳ Idle' : ''}`}
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
        `<tr><td>#${r.id}</td><td>${r.table_name}</td><td>${r.guests}</td><td>${r.duration_sec}s</td><td>${r.clear_duration_sec}s</td><td>${r.timestamp}</td></tr>`
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
      const sim = a.turnover_simulator;

      const kpis = document.getElementById('an-kpis');
      kpis.replaceChildren();
      [
        ['Visits', s.visits],
        ['Avg Dining Time', `${Math.round(s.avg_duration_sec / 60)} min (${s.avg_duration_sec}s)`],
        ['Avg Clear Speed', `${s.avg_clear_sec}s`],
        ['Seat Fill Efficiency', `${s.seat_efficiency_pct}%`],
        ['Peak Tables Used', `${s.peak_occupied} / ${s.total_tables}`]
      ].forEach(([title, val]) => {
        const c = el('div', 'card');
        c.append(el('div', 'kpi-title', title), el('div', 'kpi-value', String(val)));
        kpis.append(c);
      });

      const simBox = document.getElementById('an-sim');
      simBox.innerHTML = `
        <strong> Table Turnover &amp; Revenue Impact Simulator (PHP)</strong><br>
        <span style="font-size:0.9rem;color:#cbd5e1;">
          With <strong>${sim.tables} tables</strong> over <strong>${sim.operating_hours} hrs/day</strong> (avg party: ${sim.avg_party_size}, avg spend: ₱${sim.avg_spend_php.toLocaleString()}/guest):<br>
          • Reducing cycle time from <strong>${sim.current_cycle_min} min</strong> to <strong>${sim.target_cycle_min} min</strong> increases daily sessions from <strong>${sim.current_daily_sessions}</strong> to <strong>${sim.target_daily_sessions}</strong> (+${sim.extra_daily_sessions} sessions / +${sim.extra_daily_guests} guests/day).<br>
          • Projected Revenue Gain: <strong>+₱${sim.extra_daily_revenue_php.toLocaleString()} / day</strong> (<strong>+₱${sim.extra_monthly_revenue_php.toLocaleString()} / month</strong>).
        </span>
      `;

      const tbody = document.getElementById('turnover-body');
      tbody.innerHTML = a.turnover_ranking.map(t =>
        `<tr>
          <td><strong>${t.table_name}</strong></td>
          <td>${t.turns} (${t.turns_per_day}/day)</td>
          <td>${t.avg_dining_sec}s</td>
          <td>${t.avg_clear_sec}s</td>
          <td>${t.avg_idle_sec}s</td>
          <td><strong>${t.avg_cycle_sec}s</strong></td>
          <td>${t.avg_guests}</td>
          <td>${t.seat_efficiency_pct}%</td>
        </tr>`
      ).join('') || '<tr><td colspan="8">No completed table turns recorded yet.</td></tr>';

      const hint = document.getElementById('an-hint');
      const opps = a.seating_opportunities.length ? ` | Seating tips: ${a.seating_opportunities.join(' ')}` : '';
      if (a.busiest.length) {
        const parts = a.busiest.map(b =>
          `${DAYS[b.weekday]} ${hourLabel(b.hour)} (avg ${b.avg_occupied} tables → ${b.servers_needed} server${b.servers_needed > 1 ? 's' : ''})`);
        hint.textContent = `Busiest slots: ${parts.join('; ')}.${opps}`;
      } else {
        hint.textContent = `Occupancy is sampled every minute.${opps}`;
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
      document.getElementById('set-clear-alert').value = s.clear_alert_seconds || 180;
      document.getElementById('set-vacant-alert').value = s.vacant_alert_seconds || 900;
      document.getElementById('set-target-min').value = s.target_dining_minutes || 75;
      document.getElementById('set-spend').value = s.avg_spend_php || 500;
      document.getElementById('set-hours').value = s.operating_hours || 10;
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
        clear_alert_seconds: parseFloat(document.getElementById('set-clear-alert').value),
        vacant_alert_seconds: parseFloat(document.getElementById('set-vacant-alert').value),
        target_dining_minutes: parseFloat(document.getElementById('set-target-min').value),
        avg_spend_php: parseFloat(document.getElementById('set-spend').value),
        operating_hours: parseFloat(document.getElementById('set-hours').value),
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
      GUESTS_LEFT: e => ['clear', `${e.table} is now vacant (${e.guests} guests left after ${e.duration_sec}s)`],
      CHECK_ON_TABLE: e => ['check', `Check on ${e.table} (${Math.round(e.seated_sec / 60)} min seated)`],
      NEEDS_CLEARING: e => ['clear', `${e.table} needs clearing`],
      CLEARING_OVERDUE: e => ['clear', `🚨 ${e.table} unbussed for ${Math.round(e.dirty_sec / 60)} min!`],
      CLEARED: e => ['ok', `${e.table} cleared & ready in ${e.clear_duration_sec || 0}s`],
      VACANT_TOO_LONG: e => ['check', `⏳ ${e.table} has been vacant for ${Math.round(e.vacant_sec / 60)} min`],
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
      const fresh = Date.now() / 1000 - e.ts < 30;
      if (fresh && ['CHECK_ON_TABLE', 'NEEDS_CLEARING', 'CLEARING_OVERDUE', 'VACANT_TOO_LONG'].includes(e.type)) {
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
    uvicorn.run(app, host="0.0.0.0", port=8020)