"""Project-local camera overlays; independent of human annotation records."""
import math

def validate_regions(rows):
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("区域 JSON 应为非空数组，最多 10000 条")
    result = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 条不是对象")
        did = row.get("did")
        if not isinstance(did, str) or not did.strip() or len(did) > 200:
            raise ValueError(f"第 {i} 条缺少有效 did")
        width, height = row.get("width"), row.get("height")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 or v > 100000 for v in (width, height)):
            raise ValueError(f"第 {i} 条 width / height 无效")
        polygons = row.get("polygon")
        if not isinstance(polygons, list) or not polygons or len(polygons) > 100:
            raise ValueError(f"第 {i} 条 polygon 无效")
        for points in polygons:
            if not isinstance(points, list) or len(points) < 6 or len(points) % 2 or len(points) > 2000:
                raise ValueError(f"第 {i} 条多边形应为至少三个点的扁平坐标数组")
            for j, value in enumerate(points):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= (width if j % 2 == 0 else height):
                    raise ValueError(f"第 {i} 条坐标超出参考画面")
        # Preserve upstream reference metadata without interpreting it as labels.
        result.append(dict(row, did=did.strip()))
    return result
