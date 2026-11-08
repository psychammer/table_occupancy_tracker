def calculate_overlap(box1, box2):
    """
    Calculates the intersection area relative to the smaller box area.
    box format: (x1, y1, x2, y2, conf)
    """
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])

    inter_width = max(0, x2 - x1)
    inter_height = max(0, y2 - y1)
    inter_area = inter_width * inter_height

    area1 = max(1, (box1[2] - box1[0]) * (box1[3] - box1[1]))
    area2 = max(1, (box2[2] - box2[0]) * (box2[3] - box2[1]))
    min_area = min(area1, area2)

    return inter_area / min_area


def remove_duplicates(boxes, max_overlap=0.70):
    """
    Sorts boxes by confidence (highest first) and removes boxes that overlap
    more than max_overlap (70%).
    """
    # Sort by confidence descending
    sorted_boxes = sorted(boxes, key=lambda x: x[4], reverse=True)
    kept = []

    for box in sorted_boxes:
        is_duplicate = False
        for kept_box in kept:
            if calculate_overlap(box, kept_box) >= max_overlap:
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append(box)

    return kept