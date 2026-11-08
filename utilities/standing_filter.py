from utilities.duplicates import calculate_overlap

THIGH_TO_TORSO_RATIO = 0.40
KNEE_CONF_THRESHOLD = 0.50

def is_standing(keypoints_xy, keypoints_conf):
    """
    COCO Keypoints:
    5: Left Shoulder, 6: Right Shoulder
    11: Left Hip, 12: Right Hip
    13: Left Knee, 14: Right Knee
    """
    sides = [
        (5, 11, 13),  # Left side
        (6, 12, 14)   # Right side
    ]

    best_side = None
    best_conf = -1

    for sh, hip, knee in sides:
        if len(keypoints_conf) > knee:
            # Ensure knee confidence is high enough to be reliable
            if keypoints_conf[knee] >= KNEE_CONF_THRESHOLD and keypoints_conf[sh] > 0.3 and keypoints_conf[hip] > 0.3:
                avg_conf = (keypoints_conf[sh] + keypoints_conf[hip] + keypoints_conf[knee]) / 3.0
                if avg_conf > best_conf:
                    best_conf = avg_conf
                    best_side = (sh, hip, knee)

    # If knees are occluded under the table, assume sitting (not standing)
    if best_side is None:
        return False

    sh, hip, knee = best_side
    sh_y = keypoints_xy[sh][1]
    hip_y = keypoints_xy[hip][1]
    knee_y = keypoints_xy[knee][1]

    torso_height = abs(hip_y - sh_y)
    thigh_height = abs(knee_y - hip_y)

    if torso_height < 5:  # Avoid division by zero
        return False

    ratio = thigh_height / torso_height
    # If ratio >= 0.4, legs are extended vertically -> Standing (Waiter/Passerby)
    return ratio >= THIGH_TO_TORSO_RATIO


def remove_standing(person_boxes, pose_result):
    """
    Matches person detection boxes with pose estimation boxes and filters out standing people.
    """
    if pose_result.keypoints is None or pose_result.boxes is None:
        return person_boxes

    pose_boxes = pose_result.boxes.xyxy.cpu().numpy()
    kpts_xy = pose_result.keypoints.xy.cpu().numpy()
    kpts_conf = (
        pose_result.keypoints.conf.cpu().numpy()
        if pose_result.keypoints.conf is not None
        else None
    )

    if kpts_conf is None:
        return person_boxes

    seated_people = []
    for p_box in person_boxes:
        standing = False
        for i, pose_box in enumerate(pose_boxes):
            # Check if this pose belongs to the detected person box
            if calculate_overlap(p_box[:4], pose_box) > 0.5:
                if is_standing(kpts_xy[i], kpts_conf[i]):
                    standing = True
                break
        if not standing:
            seated_people.append(p_box)

    return seated_people