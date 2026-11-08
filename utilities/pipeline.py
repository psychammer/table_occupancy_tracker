import os
import sys
import json
import time
import queue
import threading
import cv2
import numpy as np
from ultralytics import YOLO

from utilities.duplicates import remove_duplicates
from utilities.standing_filter import remove_standing
from utilities.tables import people_at_table, draw_tabletops, get_taken_seats
from utilities.table_state import TableState, TableConfig, State

DEFAULT_SETTINGS = {
    "video_path": "videos/PlaceE_reduced.mp4",
    "yolo_model": "yolo26l.pt",
    "pose_model": "yolo26s-pose.pt",
    "confidence": 0.30,
    "infer_fps": 4.0,              # inference runs this many times per second, not per frame
    "occupy_seconds": 3.0,
    "free_seconds": 20.0,
    "up_lock_seconds": 2.0,
    "down_lock_seconds": 10.0,
    "check_after_seconds": 300.0,  # "check on table" alert
    "min_visit_seconds": 20.0,
    "require_clearing": True,
    "seats_per_table": 4,
    "display_size": (1280, 720)
}

GREEN = (0, 200, 0)
RED = (0, 0, 255)
AMBER = (0, 165, 255)

# ---------- models are loaded once and reused across restarts ----------
_models = {}

def get_models(cfg):
    key = (cfg["yolo_model"], cfg["pose_model"])
    if key not in _models:
        _models.clear()
        _models[key] = (YOLO(key[0]), YOLO(key[1]))
    return _models[key]


# ---------- video source: files are paced + looped, streams are drained + reconnected ----------
def parse_source(path):
    """'0', '1', ... mean a local webcam; anything else is a file path or a stream URL."""
    text = str(path).strip()
    return int(text) if text.isdigit() else text


def open_capture(src):
    if isinstance(src, int):
        # DirectShow opens faster and more reliably on Windows
        cap = cv2.VideoCapture(src, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(src)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)   # keep the 16:9 shape the table polygons assume
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        return cap
    return cv2.VideoCapture(src)


class VideoSource:
    def __init__(self, path):
        self.path = path
        self.src = parse_source(path)
        self.is_file = isinstance(self.src, str) and os.path.isfile(self.src)
        self._cap = None
        self._fps = 30.0
        self._next_t = 0.0
        self._latest = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def read(self):
        return self._read_file() if self.is_file else self._read_stream()

    def _read_file(self):
        if self._cap is None:
            self._cap = open_capture(self.src)
            fps = self._cap.get(cv2.CAP_PROP_FPS)
            self._fps = fps if fps and fps > 0 and not np.isnan(fps) else 30.0
        ok, frame = self._cap.read()
        if not ok:  # end of file: loop for demo purposes
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok:
                return None
        # play at real speed so wall-clock timers match the footage
        now = time.monotonic()
        if self._next_t > now:
            time.sleep(self._next_t - now)
        self._next_t = max(now, self._next_t) + 1.0 / self._fps
        return frame

    def _read_stream(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()
        with self._lock:
            frame, self._latest = self._latest, None
        if frame is None:
            time.sleep(0.02)
        return frame

    def _reader(self):
        """Reads continuously so OpenCV's buffer never goes stale; reconnects with backoff."""
        backoff = 1.0
        while not self._stop.is_set():
            cap = open_capture(self.src)
            if not cap.isOpened():
                cap.release()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break  # dropped connection -> outer loop reconnects
                with self._lock:
                    self._latest = frame
            cap.release()

    def close(self):
        self._stop.set()
        if self._cap is not None:
            self._cap.release()


def ensure_default_table_files():
    """Creates sample 6-table layout if tables.json / tabletops.json don't exist yet."""
    if not os.path.exists("tables.json"):
        sample_tables = {
            "Table 1": [[100, 180], [360, 180], [360, 390], [100, 390]],
            "Table 2": [[460, 180], [720, 180], [720, 390], [460, 390]],
            "Table 3": [[820, 180], [1080, 180], [1080, 390], [820, 390]],
            "Table 4": [[100, 440], [360, 440], [360, 660], [100, 660]],
            "Table 5": [[460, 440], [720, 440], [720, 660], [460, 660]],
            "Table 6": [[820, 440], [1080, 440], [1080, 660], [820, 660]],
        }
        with open("tables.json", "w") as f:
            json.dump(sample_tables, f, indent=2)

    if not os.path.exists("tabletops.json"):
        sample_tops = {
            "Table 1": [[150, 220], [310, 220], [310, 350], [150, 350]],
            "Table 2": [[510, 220], [670, 220], [670, 350], [510, 350]],
            "Table 3": [[870, 220], [1030, 220], [1030, 350], [870, 350]],
            "Table 4": [[150, 480], [310, 480], [310, 610], [150, 610]],
            "Table 5": [[510, 480], [670, 480], [670, 610], [510, 610]],
            "Table 6": [[870, 480], [1030, 480], [1030, 610], [870, 610]],
        }
        with open("tabletops.json", "w") as f:
            json.dump(sample_tops, f, indent=2)


def _empty_info(tables, seats):
    return {
        "frame": 0, "occupied_tables": 0, "needs_clearing_tables": 0,
        "total_tables": len(tables), "total_guests": 0,
        "total_seats": len(tables) * seats, "utilization": 0.0,
        "avg_seated_sec": 0, "tables": {}
    }


def run_pipeline(settings=None, on_visit_end=None, on_event=None,
                 should_stop=None, commands=None):
    """
    Generator yielding (annotated_frame, info_dict).

    on_visit_end(visit_dict)  - completed visit, with start_ts / end_ts
    on_event(event_dict)      - SEATED / CHECK_ON_TABLE / GUESTS_LEFT / NEEDS_CLEARING / CLEARED
    should_stop()             - return True to end the generator
    commands                  - queue.Queue of ("cleared", table_name) from the dashboard
    All timers use time.monotonic(), so they are correct at any video/inference FPS.
    """
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}
    ensure_default_table_files()

    with open("tables.json", "r") as f:
        tables = json.load(f)
    with open("tabletops.json", "r") as f:
        tabletops = json.load(f)

    det_model, pose_model = get_models(cfg)
    source = VideoSource(cfg["video_path"])

    tcfg = TableConfig(
        occupy_seconds=cfg["occupy_seconds"], free_seconds=cfg["free_seconds"],
        up_lock_seconds=cfg["up_lock_seconds"], down_lock_seconds=cfg["down_lock_seconds"],
        check_after_seconds=cfg["check_after_seconds"], min_visit_seconds=cfg["min_visit_seconds"],
        require_clearing=cfg["require_clearing"],
    )
    states = {t: TableState(t, tcfg) for t in tables}
    seats = cfg["seats_per_table"]

    def dispatch(events):
        for e in events:
            d = e.to_dict()
            if e.type == "GUESTS_LEFT" and on_visit_end:
                on_visit_end({"table_name": e.table, "guests": d["guests"],
                              "duration_sec": d["duration_sec"],
                              "start_ts": d["start_ts"], "end_ts": d["end_ts"]})
            if on_event:
                on_event(d)

    infer_interval = 1.0 / max(0.5, cfg["infer_fps"])
    last_infer = -1e9
    clean_boxes = []
    taken = {t: [] for t in tables}
    info = _empty_info(tables, seats)
    frame_idx = 0
    last_frame_t = time.monotonic()

    try:
        while not (should_stop and should_stop()):
            # staff actions from the dashboard
            while commands is not None and not commands.empty():
                cmd, name = commands.get_nowait()
                if cmd == "cleared" and name in states:
                    dispatch(states[name].mark_cleared(time.monotonic()))

            frame = source.read()
            if frame is None:
                # No *new* frame yet is normal for live sources (we poll faster than the camera
                # delivers). Only treat it as an outage after a few seconds without any frame.
                if time.monotonic() - last_frame_t < 3.0:
                    time.sleep(0.005)
                    continue
                frame = np.zeros((720, 1280, 3), dtype=np.uint8)
                cv2.putText(frame, f"Waiting for video source: {cfg['video_path']}", (50, 360),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, AMBER, 2)
                time.sleep(0.5)
                yield frame, info
                continue

            last_frame_t = time.monotonic()
            frame = cv2.resize(frame, cfg["display_size"])
            frame_idx += 1
            now = time.monotonic()

            # ---- inference + state update, throttled to infer_fps ----
            if now - last_infer >= infer_interval:
                last_infer = now
                det = det_model(frame, classes=[0], conf=cfg["confidence"], verbose=False)[0]
                pose = pose_model(frame, conf=cfg["confidence"], verbose=False)[0]

                raw_boxes = []
                if det.boxes is not None:
                    for box in det.boxes:
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        raw_boxes.append((x1, y1, x2, y2, float(box.conf[0])))

                seated = remove_standing(raw_boxes, pose)
                clean_boxes = remove_duplicates(seated, max_overlap=0.70)
                centers = [((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) for b in clean_boxes]

                for t_name, t_poly in tables.items():
                    inside = people_at_table(centers, t_poly)
                    dispatch(states[t_name].update(len(inside), now))
                    taken[t_name] = get_taken_seats(inside, t_poly, seats)

            # ---- draw (every frame, using cached detections) ----
            for x1, y1, x2, y2, conf in clean_boxes:
                cv2.rectangle(frame, (x1, y1), (x2, y2), GREEN, 2)
                cv2.putText(frame, f"Person {conf:.2f}", (x1, y1 - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, GREEN, 2)

            table_colors, table_stats = {}, {}
            occupied_n = dirty_n = total_guests = 0
            durations = []
            for t_name, t_poly in tables.items():
                snap = states[t_name].snapshot(now)
                tx, ty = t_poly[0]
                if snap["state"] == State.OCCUPIED.value:
                    occupied_n += 1
                    total_guests += snap["guests"]
                    durations.append(snap["duration_sec"])
                    table_colors[t_name] = RED
                    m, s = divmod(snap["duration_sec"], 60)
                    label = f"{t_name}: {snap['guests']}p | {m:02d}:{s:02d}"
                    if snap["check_due"]:
                        label += " | CHECK"
                    cv2.putText(frame, label, (tx, ty - 10), cv2.FONT_HERSHEY_SIMPLEX,
                                0.6, (255, 255, 255), 2)
                elif snap["state"] == State.NEEDS_CLEARING.value:
                    dirty_n += 1
                    table_colors[t_name] = AMBER
                    cv2.putText(frame, f"{t_name}: CLEAR", (tx, ty - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, AMBER, 2)
                else:
                    table_colors[t_name] = GREEN
                snap["taken_seats"] = taken[t_name] if snap["occupied"] else []
                snap["total_seats"] = seats
                table_stats[t_name] = snap

            frame = draw_tabletops(frame, tabletops, table_colors, tint=0.40)

            total_seats = len(tables) * seats
            utilization = min(1.0, total_guests / max(1, total_seats)) * 100.0
            cv2.rectangle(frame, (15, 15), (370, 95), (0, 0, 0), -1)
            cv2.putText(frame, f"Tables: {occupied_n}/{len(tables)} | Guests: {total_guests}/{total_seats}",
                        (25, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
            cv2.rectangle(frame, (25, 60), (285, 80), (60, 60, 60), -1)
            cv2.rectangle(frame, (25, 60), (25 + int(2.6 * utilization), 80), GREEN, -1)
            cv2.putText(frame, f"{int(utilization)}%", (295, 76),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

            info = {
                "frame": frame_idx,
                "occupied_tables": occupied_n,
                "needs_clearing_tables": dirty_n,
                "total_tables": len(tables),
                "total_guests": total_guests,
                "total_seats": total_seats,
                "utilization": round(utilization, 1),
                "avg_seated_sec": int(sum(durations) / len(durations)) if durations else 0,
                "tables": table_stats,
            }
            yield frame, info
    finally:
        # restart/shutdown: log any visit still in progress instead of dropping it
        now = time.monotonic()
        for st in states.values():
            dispatch(st.force_end(now))
        source.close()