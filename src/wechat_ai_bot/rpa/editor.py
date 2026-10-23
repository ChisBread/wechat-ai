"""Visual checks shared by text and attachment sending."""

from typing import Optional


def enabled_send_button(window_manager) -> Optional[list[int]]:
    """Locate the green enabled button inside the estimated toolbar region.

    The estimate can include the voice icon and empty space. A grey button is
    not evidence that a paste reached the editor, and must never be clicked.
    """
    bounds = window_manager.get_icon_position("send_button")
    if not bounds:
        return None
    left, top, right, bottom = map(int, bounds)
    if right <= left or bottom <= top:
        return None
    shot = window_manager.image_processor.take_screenshot(
        region=[left, top, right - left, bottom - top],
    )
    if shot is None:
        return None
    rgb = shot.convert("RGB")
    points = []
    for y in range(rgb.height):
        for x in range(rgb.width):
            red, green, blue = rgb.getpixel((x, y))
            if red < 80 and green > 140 and blue < 160:
                points.append((x, y))
    if len(points) < 100:
        return None
    x1, x2 = min(x for x, _ in points), max(x for x, _ in points)
    y1, y2 = min(y for _, y in points), max(y for _, y in points)
    if x2 - x1 < 20 or y2 - y1 < 12:
        return None
    return [left + x1, top + y1, left + x2, top + y2]
