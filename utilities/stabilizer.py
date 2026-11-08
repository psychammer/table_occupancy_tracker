import cv2
import numpy as np


class CameraStabilizer:
    def __init__(self, nfeatures=2000):
        self.orb = cv2.ORB_create(nfeatures=nfeatures)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self.ref_kp = None
        self.ref_des = None
        self.last_valid_H = np.eye(3, dtype=np.float32)
        self.matched_points_count = 0

    def reset(self):
        """Resets the reference keypoints (useful when a video loops or restarts)."""
        self.ref_kp = None
        self.ref_des = None
        self.last_valid_H = np.eye(3, dtype=np.float32)
        self.matched_points_count = 0

    def set_reference(self, frame, exclude_boxes=None):
        if frame is None:
            return
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mask = np.ones_like(gray, dtype=np.uint8) * 255
        # Mask out people so walking guests/staff do not distort camera tracking
        if exclude_boxes:
            for box in exclude_boxes:
                x1, y1, x2, y2 = box[:4]
                mask[max(0, int(y1)):int(y2), max(0, int(x1)):int(x2)] = 0

        self.ref_kp, self.ref_des = self.orb.detectAndCompute(gray, mask)
        self.last_valid_H = np.eye(3, dtype=np.float32)

    def update_homography(self, frame, exclude_boxes=None):
        if frame is None:
            return self.last_valid_H

        if self.ref_des is None or self.ref_kp is None or len(self.ref_kp) < 10:
            self.set_reference(frame, exclude_boxes)
            return self.last_valid_H

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mask = np.ones_like(gray, dtype=np.uint8) * 255
        if exclude_boxes:
            for box in exclude_boxes:
                x1, y1, x2, y2 = box[:4]
                mask[max(0, int(y1)):int(y2), max(0, int(x1)):int(x2)] = 0

        kp, des = self.orb.detectAndCompute(gray, mask)
        if des is None or len(kp) < 10:
            self.matched_points_count = 0
            return self.last_valid_H

        matches = self.bf.knnMatch(self.ref_des, des, k=2)
        good = [m for pair in matches if len(pair) == 2 for m, n in [pair] if m.distance < 0.75 * n.distance]

        self.matched_points_count = len(good)
        if len(good) >= 12:
            src_pts = np.float32([self.ref_kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            H, _ = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if H is not None:
                # Sanity check: Ensure homography determinant is non-degenerate
                det = float(np.linalg.det(H[:2, :2]))
                if 0.3 < det < 3.0:
                    self.last_valid_H = 0.75 * H + 0.25 * self.last_valid_H
        return self.last_valid_H

    def transform_points(self, points, enable=True):
        """
        Warp a polygon list [[x1, y1], [x2, y2], ...] to match camera movement.
        """
        if not enable or self.ref_des is None or self.last_valid_H is None or len(points) == 0:
            return points

        pts = np.float32(points).reshape(-1, 1, 2)
        try:
            transformed = cv2.perspectiveTransform(pts, self.last_valid_H)
            return [[int(round(p[0][0])), int(round(p[0][1]))] for p in transformed]
        except Exception:
            return points