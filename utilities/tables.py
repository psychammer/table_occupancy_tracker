import cv2
import numpy as np
import math

def people_at_table(person_centers, table_polygon):
    """
    Returns the list of person centers that fall inside the table region polygon.
    """
    poly = np.array(table_polygon, dtype=np.int32)
    inside_centers = []
    for cx, cy in person_centers:
        if cv2.pointPolygonTest(poly, (float(cx), float(cy)), False) >= 0:
            inside_centers.append((cx, cy))
    return inside_centers


def draw_tabletops(frame, tabletops_dict, table_colors, tint=0.40):
    """
    Draws a semi-transparent red (occupied) or green (free) overlay on tabletops.
    """
    overlay = frame.copy()
    for table_name, points in tabletops_dict.items():
        if len(points) < 3:
            continue
        pts = np.array(points, dtype=np.int32)
        color = table_colors.get(table_name, (0, 200, 0))
        cv2.fillPoly(overlay, [pts], color)
        cv2.polylines(frame, [pts], isClosed=True, color=color, thickness=2)

    cv2.addWeighted(overlay, tint, frame, 1 - tint, 0, frame)
    return frame


def get_taken_seats(person_centers_at_table, table_polygon, seats_per_table=4):
    """
    Determines which seat numbers (1 to seats_per_table) are occupied
    based on the angle of the person relative to the center of the table.
    """
    if not person_centers_at_table or len(table_polygon) == 0:
        return []

    pts = np.array(table_polygon)
    tx = np.mean(pts[:, 0])
    ty = np.mean(pts[:, 1])

    taken = set()
    for px, py in person_centers_at_table:
        angle = math.atan2(py - ty, px - tx)  # -pi to pi
        norm_angle = (angle + math.pi) / (2 * math.pi)  # 0.0 to 1.0
        seat_idx = int(norm_angle * seats_per_table) % seats_per_table + 1
        taken.add(seat_idx)

    return sorted(list(taken))
