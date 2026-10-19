#!/usr/bin/env python3
"""Draw the SafeAgent Studio task flow to activity_map.svg and activity_map.png.

The picture follows the activity diagram in README.md. Run from the repo root:

    python activity_map.py
"""

from __future__ import annotations

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SVG_PATH = ROOT / "activity_map.svg"
PNG_PATH = ROOT / "activity_map.png"
FONT = "Microsoft YaHei, SimHei, sans-serif"
FONT_FILE = Path(r"C:\Windows\Fonts\msyh.ttc")

# kind -> fill, stroke
COLORS = {
    "term": ("#E7F6EE", "#1F8A4C"),
    "bad": ("#FDECEC", "#C24141"),
    "step": ("#EEF4FC", "#2F6FED"),
    "ask": ("#FFF7E8", "#D48806"),
    "sand": ("#E7F7F6", "#0F766E"),
}


def _nodes():
    """id, kind, text, center x, center y, width, height."""
    return [
        ("start", "term", "提交 Prompt", 280, 56, 180, 46),
        ("ws", "step", "连接 WebSocket", 280, 132, 190, 46),
        ("run", "step", "POST /run 并启动线程", 280, 208, 230, 46),
        ("session", "step", "装入会话记忆", 280, 284, 180, 46),
        ("recall", "step", "召回长期记忆\n距离大于 0.7 的丢掉", 280, 372, 230, 62),
        ("model", "step", "请求大模型", 280, 470, 170, 46),
        ("tool", "ask", "返回工具调用?", 280, 590, 200, 108),
        ("fake", "ask", "要求执行\n但从未调工具?", 280, 790, 210, 112),
        ("nudge", "step", "注入纠正提示", 90, 680, 150, 46),
        ("abort", "bad", "中止：禁止伪造执行结果", 280, 960, 250, 46),
        ("answer", "term", "推送最终回复", 560, 790, 180, 46),
        ("save", "step", "写回会话", 560, 890, 150, 46),
        ("box", "ask", "这次创建过容器?", 560, 1030, 210, 108),
        ("held", "sand", "停容器\n卷保留 24 小时", 390, 1200, 180, 62),
        ("gone", "sand", "导出 outputs\n并销毁容器", 730, 1200, 190, 62),
        ("mem", "step", "成功则写入 Chroma", 560, 1340, 210, 46),
        ("end", "term", "结束", 560, 1430, 120, 46),
        ("host", "sand", "宿主机搜索\n不起沙箱", 620, 590, 180, 64),
        ("risk", "ask", "高风险操作?", 920, 470, 190, 100),
        ("hitl", "ask", "300 秒内点同意?", 1180, 680, 220, 108),
        ("denied", "bad", "工具结果为拒绝", 1180, 860, 180, 46),
        ("lane", "ask", "已有沙箱?", 920, 680, 180, 108),
        ("create", "sand", "git archive 播种\n并恢复上一轮产物", 740, 860, 220, 64),
        ("exec", "sand", "在 /workspace 执行", 1000, 860, 200, 46),
        ("fuse", "ask", "同一工具\n连续失败 3 次?", 1000, 1040, 210, 112),
        ("stop", "bad", "中止：检查路径或参数", 1280, 1040, 240, 46),
    ]


def _edges():
    """src, dst, label, exit side, entry side, route.

    route is "straight" or a list of extra points the line must pass.
    """
    return [
        ("start", "ws", "", "bottom", "top", "straight"),
        ("ws", "run", "", "bottom", "top", "straight"),
        ("run", "session", "", "bottom", "top", "straight"),
        ("session", "recall", "", "bottom", "top", "straight"),
        ("recall", "model", "", "bottom", "top", "straight"),
        ("model", "tool", "", "bottom", "top", "straight"),
        ("tool", "fake", "否", "bottom", "top", "straight"),
        ("tool", "host", "web_search", "right", "left", "straight"),
        ("tool", "risk", "bash / 读 / 写 / 改", "right", "left", [(500, 520), (820, 470)]),
        ("fake", "nudge", "重试未满 2 次", "left", "bottom", [(175, 790)]),
        ("nudge", "model", "", "top", "left", [(90, 470)]),
        ("fake", "abort", "仍然不调用", "bottom", "top", "straight"),
        ("fake", "answer", "解释类或已经调用过", "right", "left", "straight"),
        ("answer", "save", "", "bottom", "top", "straight"),
        ("save", "box", "", "bottom", "top", "straight"),
        ("box", "held", "保留沙箱", "left", "top", [(470, 1200)]),
        ("box", "gone", "不保留", "right", "top", [(650, 1200)]),
        ("box", "mem", "没有", "bottom", "top", "straight"),
        ("held", "mem", "", "bottom", "left", [(390, 1340)]),
        ("gone", "mem", "", "bottom", "right", [(730, 1340)]),
        ("mem", "end", "", "bottom", "top", "straight"),
        ("risk", "lane", "只读或允许的写入", "bottom", "top", "straight"),
        ("risk", "hitl", "需要审批", "right", "top", [(1180, 470)]),
        ("hitl", "denied", "拒绝或超时", "bottom", "top", "straight"),
        ("hitl", "lane", "同意", "left", "right", "straight"),
        ("lane", "create", "没有", "left", "top", [(800, 740)]),
        ("lane", "exec", "已有", "bottom", "top", [(1000, 760)]),
        ("create", "exec", "", "right", "left", "straight"),
        ("host", "fuse", "", "bottom", "bottom", [(620, 710), (430, 710), (430, 1136), (1000, 1136)]),
        ("exec", "fuse", "", "bottom", "top", "straight"),
        ("denied", "fuse", "", "left", "right", [(1120, 860), (1120, 1040)]),
        ("fuse", "stop", "是", "right", "left", "straight"),
        ("fuse", "model", "否", "top", "right", [(1500, 984), (1500, 408), (370, 408)]),
    ]


def _side_point(node, side):
    _id, _kind, _text, cx, cy, w, h = node
    if side == "top":
        return cx, cy - h / 2
    if side == "bottom":
        return cx, cy + h / 2
    if side == "left":
        return cx - w / 2, cy
    return cx + w / 2, cy


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _shape(node) -> str:
    nid, kind, text, cx, cy, w, h = node
    fill, stroke = COLORS[kind]
    lines = text.split("\n")
    if kind == "ask":
        body = (
            f'<polygon points="{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
        )
    elif kind in ("term", "bad") and nid in ("start", "end", "abort", "stop"):
        body = (
            f'<rect x="{cx - w / 2}" y="{cy - h / 2}" width="{w}" height="{h}" rx="{h / 2}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
        )
    else:
        body = (
            f'<rect x="{cx - w / 2}" y="{cy - h / 2}" width="{w}" height="{h}" rx="8" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
        )
    start = cy - (len(lines) - 1) * 9
    tspans = []
    for index, line in enumerate(lines):
        tspans.append(
            f'<tspan x="{cx}" y="{start + index * 18}">{_escape(line)}</tspan>'
        )
    label = (
        f'<text text-anchor="middle" font-family="{FONT}" font-size="15" fill="#1F2937">'
        + "".join(tspans)
        + "</text>"
    )
    return body + label


def _arrow(points, label: str) -> str:
    cleaned = [points[0]]
    for point in points[1:]:
        if abs(point[0] - cleaned[-1][0]) < 0.5 and abs(point[1] - cleaned[-1][1]) < 0.5:
            continue
        cleaned.append(point)
    if len(cleaned) < 2:
        return ""
    coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in cleaned)
    x1, y1 = cleaned[-2]
    x2, y2 = cleaned[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    size = 8
    left = (
        x2 - size * math.cos(angle) + size * 0.55 * math.sin(angle),
        y2 - size * math.sin(angle) - size * 0.55 * math.cos(angle),
    )
    right = (
        x2 - size * math.cos(angle) - size * 0.55 * math.sin(angle),
        y2 - size * math.sin(angle) + size * 0.55 * math.cos(angle),
    )
    # Stop the shaft just before the arrow head so the line does not poke through.
    shaft_end = (
        x2 - size * 0.75 * math.cos(angle),
        y2 - size * 0.75 * math.sin(angle),
    )
    shaft = cleaned[:-1] + [shaft_end]
    shaft_coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in shaft)
    parts = [
        f'<polyline points="{shaft_coords}" fill="none" stroke="#334155" stroke-width="1.4"/>',
        f'<polygon points="{x2:.1f},{y2:.1f} {left[0]:.1f},{left[1]:.1f} {right[0]:.1f},{right[1]:.1f}" fill="#334155"/>',
    ]
    if label:
        lx = (cleaned[0][0] + cleaned[1][0]) / 2
        ly = (cleaned[0][1] + cleaned[1][1]) / 2 - 8
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" font-family="{FONT}" '
            f'font-size="12" fill="#475569">{_escape(label)}</text>'
        )
    return "".join(parts)


def build_svg() -> str:
    nodes = {item[0]: item for item in _nodes()}
    width, height = 1580, 1520
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="32" y="28" font-family="{FONT}" font-size="18" font-weight="700" fill="#111827">SafeAgent Studio 活动图</text>',
    ]
    for src, dst, label, out_side, in_side, route in _edges():
        start = _side_point(nodes[src], out_side)
        end = _side_point(nodes[dst], in_side)
        if route == "straight":
            points = [start, end]
        else:
            points = [start, *route, end]
        chunks.append(_arrow(points, label))
    for node in _nodes():
        chunks.append(_shape(node))
    chunks.append("</svg>")
    return "\n".join(chunks)


def _write_png(svg_text: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    font = ImageFont.truetype(str(FONT_FILE), 15) if FONT_FILE.is_file() else ImageFont.load_default()
    small = ImageFont.truetype(str(FONT_FILE), 12) if FONT_FILE.is_file() else font
    title = ImageFont.truetype(str(FONT_FILE), 20) if FONT_FILE.is_file() else font
    image = Image.new("RGB", (1580, 1520), "white")
    draw = ImageDraw.Draw(image)
    draw.text((32, 8), "SafeAgent Studio 活动图", fill="#111827", font=title)
    nodes = {item[0]: item for item in _nodes()}

    def line(points, label):
        cleaned = [points[0]]
        for point in points[1:]:
            if abs(point[0] - cleaned[-1][0]) < 0.5 and abs(point[1] - cleaned[-1][1]) < 0.5:
                continue
            cleaned.append(point)
        if len(cleaned) < 2:
            return
        draw.line(cleaned, fill="#334155", width=2)
        x1, y1 = cleaned[-2]
        x2, y2 = cleaned[-1]
        angle = math.atan2(y2 - y1, x2 - x1)
        size = 9
        left = (
            x2 - size * math.cos(angle) + size * 0.55 * math.sin(angle),
            y2 - size * math.sin(angle) - size * 0.55 * math.cos(angle),
        )
        right = (
            x2 - size * math.cos(angle) - size * 0.55 * math.sin(angle),
            y2 - size * math.sin(angle) + size * 0.55 * math.cos(angle),
        )
        draw.polygon([(x2, y2), left, right], fill="#334155")
        if label:
            lx = (cleaned[0][0] + cleaned[1][0]) / 2
            ly = (cleaned[0][1] + cleaned[1][1]) / 2 - 16
            box = draw.textbbox((0, 0), label, font=small)
            tw, th = box[2] - box[0], box[3] - box[1]
            draw.rectangle((lx - tw / 2 - 2, ly - 2, lx + tw / 2 + 2, ly + th + 2), fill="white")
            draw.text((lx - tw / 2, ly), label, fill="#475569", font=small)

    for src, dst, label, out_side, in_side, route in _edges():
        start = _side_point(nodes[src], out_side)
        end = _side_point(nodes[dst], in_side)
        points = [start, end] if route == "straight" else [start, *route, end]
        line(points, label)

    for nid, kind, text, cx, cy, w, h in _nodes():
        fill, stroke = COLORS[kind]
        box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
        if kind == "ask":
            draw.polygon(
                [(cx, cy - h / 2), (cx + w / 2, cy), (cx, cy + h / 2), (cx - w / 2, cy)],
                fill=fill,
                outline=stroke,
            )
        elif nid in ("start", "end", "abort", "stop"):
            draw.rounded_rectangle(box, radius=h / 2, fill=fill, outline=stroke, width=2)
        else:
            draw.rounded_rectangle(box, radius=8, fill=fill, outline=stroke, width=2)
        lines = text.split("\n")
        total = len(lines) * 18
        top = cy - total / 2
        for index, row in enumerate(lines):
            bbox = draw.textbbox((0, 0), row, font=font)
            tw = bbox[2] - bbox[0]
            draw.text((cx - tw / 2, top + index * 18), row, fill="#1F2937", font=font)
    image.save(PNG_PATH, "PNG")
    return True


def main() -> None:
    svg_text = build_svg()
    SVG_PATH.write_text(svg_text, encoding="utf-8")
    wrote_png = _write_png(svg_text)
    print(SVG_PATH)
    if wrote_png:
        print(PNG_PATH)


if __name__ == "__main__":
    main()
