import cv2
import json
import argparse
import numpy as np

# Controls:
# Left Click : Add corner point
# 'n'        : Next table
# 'u'        : Undo last point
# 'c'        : Clear all
# 's'        : Save to JSON
# 'f'        : Freeze / unfreeze the picture (webcam only; live by default)
# 'q'        : Quit

current_points = []
tables = {}
table_counter = 1

def on_mouse(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        current_points.append([x, y])

def main():
    global current_points, tables, table_counter
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", default="videos/PlaceE_reduced.mp4", help="Video path, stream URL, or webcam index (e.g. 0)")
    parser.add_argument("--mode", choices=["tables", "tabletops"], default="tables")
    args = parser.parse_args()

    out_file = "tables.json" if args.mode == "tables" else "tabletops.json"

    src = int(args.video) if args.video.strip().isdigit() else args.video
    cap = cv2.VideoCapture(src)
    if isinstance(src, int):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        for _ in range(30):      # webcams return dark frames while auto-exposure settles
            cap.read()
    ret, frame = cap.read()
    live = isinstance(src, int) and ret   # webcam: keep streaming so you can see the scene
    frozen = False
    if not live:
        cap.release()

    if not ret:
        print("Video not found, creating blank canvas (1280x720)...")
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    else:
        frame = cv2.resize(frame, (1280, 720))

    cv2.namedWindow("Select Points")
    cv2.setMouseCallback("Select Points", on_mouse)

    while True:
        if live and not frozen:
            ok, new = cap.read()
            if ok:
                frame = cv2.resize(new, (1280, 720))
        canvas = frame.copy()
        # Draw saved tables
        for name, pts in tables.items():
            pts_arr = np.array(pts, dtype=np.int32)
            cv2.polylines(canvas, [pts_arr], True, (0, 255, 0), 2)
            cv2.putText(canvas, name, tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        # Draw current points
        for pt in current_points:
            cv2.circle(canvas, tuple(pt), 5, (0, 165, 255), -1)
        if len(current_points) > 1:
            cv2.polylines(canvas, [np.array(current_points, dtype=np.int32)], False, (0, 165, 255), 2)

        cv2.putText(
            canvas,
            f"Mode: {args.mode} | Click: Add | N: Next Table | U: Undo | C: Clear | S: Save | Q: Quit",
            (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2
        )
        if live:
            status = "FROZEN" if frozen else "LIVE"
            cv2.putText(canvas, f"Webcam: {status} (F = freeze/unfreeze)",
                        (20, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
        cv2.imshow("Select Points", canvas)

        key = cv2.waitKey(20) & 0xFF
        if key == ord('n') and len(current_points) >= 3:
            tables[f"Table {table_counter}"] = current_points.copy()
            table_counter += 1
            current_points = []
        elif key == ord('u') and current_points:
            current_points.pop()
        elif key == ord('c'):
            tables.clear()
            current_points.clear()
            table_counter = 1
        elif key == ord('s'):
            with open(out_file, "w") as f:
                json.dump(tables, f, indent=2)
            print(f"Saved {len(tables)} tables to {out_file}")
        elif key == ord('f') and live:
            frozen = not frozen
        elif key == ord('q'):
            break

    if live:
        cap.release()

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()