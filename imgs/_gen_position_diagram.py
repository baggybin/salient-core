"""Generate imgs/kernel-position.png — where salient-core sits in the stack.

Code-rendered so labels stay exact (no model-garbled text). Palette matches
imgs/hero-bus.jpg / _overlay.py (navy + cyan bus glow).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "kernel-position.png"

PROP_BOLD = "/usr/share/fonts/noto/NotoSans-Bold.ttf"
PROP_REG = "/usr/share/fonts/noto/NotoSans-Regular.ttf"
PROP_MED = "/usr/share/fonts/noto/NotoSans-Medium.ttf"
MONO = "/usr/share/fonts/noto/NotoSansMono-Regular.ttf"

# Palette from imgs/_overlay.py
NAVY_BG = (2, 16, 31)
NAVY_PANEL = (8, 28, 48)
NAVY_CARD = (12, 36, 58)
CYAN = (68, 245, 255)
CYAN_BRIGHT = (180, 250, 255)
CYAN_DIM = (40, 120, 140)
CYAN_DEEP = (24, 60, 88)
SLATE_200 = (226, 232, 240)
SLATE_400 = (148, 163, 184)
WHITE = (255, 255, 255)
EMERALD = (52, 211, 153)
AMBER = (251, 191, 36)
VIOLET = (167, 139, 250)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def center_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    f: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    bbox = draw.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((xy[0] - tw / 2, xy[1] - th / 2), text, font=f, fill=fill)


def rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int] | None = None,
    outline: tuple[int, int, int] | None = None,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def arrow_down(
    draw: ImageDraw.ImageDraw,
    x: int,
    y0: int,
    y1: int,
    color: tuple[int, int, int],
    width: int = 3,
) -> None:
    draw.line([(x, y0), (x, y1 - 8)], fill=color, width=width)
    # arrow head
    draw.polygon([(x, y1), (x - 7, y1 - 12), (x + 7, y1 - 12)], fill=color)


def main() -> None:
    W, H = 1100, 780
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)

    # subtle grid
    for x in range(0, W, 40):
        draw.line([(x, 0), (x, H)], fill=(6, 24, 40), width=1)
    for y in range(0, H, 40):
        draw.line([(0, y), (W, y)], fill=(6, 24, 40), width=1)

    f_title = font(PROP_BOLD, 28)
    f_sub = font(PROP_REG, 15)
    f_box = font(PROP_BOLD, 18)
    f_box_sm = font(PROP_MED, 14)
    f_label = font(PROP_REG, 13)
    f_mono = font(MONO, 12)
    f_chip = font(PROP_MED, 12)

    cx = W // 2

    # --- Title ---
    center_text(draw, (cx, 36), "Where the kernel sits", f_title, CYAN_BRIGHT)
    center_text(
        draw,
        (cx, 68),
        "Control surfaces live below the model — not in the prompt",
        f_sub,
        SLATE_400,
    )

    # --- LLM box ---
    llm_box = (cx - 180, 100, cx + 180, 175)
    rounded_rect(draw, llm_box, 14, fill=NAVY_CARD, outline=CYAN_DIM, width=2)
    center_text(draw, (cx, 128), "LLM / agent loop", f_box, CYAN_BRIGHT)
    center_text(draw, (cx, 155), "Claude SDK  |  OpenAI Codex", f_label, SLATE_400)

    # arrow + label
    arrow_down(draw, cx, 180, 218, CYAN, width=3)
    center_text(draw, (cx + 95, 200), "tool calls", f_mono, CYAN_DIM)

    # --- Kernel block (the middle) ---
    k_left, k_top, k_right, k_bot = 90, 225, W - 90, 520
    rounded_rect(
        draw,
        (k_left, k_top, k_right, k_bot),
        18,
        fill=NAVY_PANEL,
        outline=CYAN,
        width=3,
    )
    # soft inner glow edge
    rounded_rect(
        draw,
        (k_left + 4, k_top + 4, k_right - 4, k_bot - 4),
        16,
        outline=CYAN_DEEP,
        width=1,
    )

    center_text(draw, (cx, k_top + 28), "salient-core", f_box, CYAN)
    center_text(
        draw,
        (cx, k_top + 52),
        "agent-control kernel  —  below the model",
        f_chip,
        CYAN_DIM,
    )

    # Three internal cards
    cards = [
        (
            "Policy gates",
            "scope + safeguards\ndefault-deny",
            EMERALD,
        ),
        (
            "Typed bus (MCP)",
            "delegation · context\nKG · discovery",
            CYAN,
        ),
        (
            "Audit trail",
            "every decision\nredacted + durable",
            AMBER,
        ),
    ]
    card_w, card_h = 250, 120
    gap = 36
    total_w = 3 * card_w + 2 * gap
    start_x = cx - total_w // 2
    card_y = k_top + 80

    for i, (title, body, accent) in enumerate(cards):
        x0 = start_x + i * (card_w + gap)
        x1 = x0 + card_w
        y0, y1 = card_y, card_y + card_h
        rounded_rect(draw, (x0, y0, x1, y1), 12, fill=NAVY_CARD, outline=accent, width=2)
        # accent bar at top
        draw.rounded_rectangle((x0 + 12, y0 + 10, x0 + 48, y0 + 14), radius=2, fill=accent)
        center_text(draw, ((x0 + x1) / 2, y0 + 38), title, f_box_sm, WHITE)
        # body — two lines
        for j, line in enumerate(body.split("\n")):
            center_text(
                draw,
                ((x0 + x1) / 2, y0 + 68 + j * 20),
                line,
                f_label,
                SLATE_400,
            )

    # operator inbox chip inside kernel, bottom
    inbox_y = k_bot - 48
    rounded_rect(
        draw,
        (cx - 160, inbox_y - 18, cx + 160, inbox_y + 18),
        10,
        fill=(20, 30, 55),
        outline=VIOLET,
        width=2,
    )
    center_text(draw, (cx, inbox_y), "operator inbox  |  typed Q/A", f_chip, VIOLET)

    # --- Arrows out of kernel ---
    targets = [
        (cx - 280, "Tools", "scoped + gated", EMERALD),
        (cx, "Other agents", "bus-mediated", CYAN),
        (cx + 280, "Operator", "human-in-the-loop", VIOLET),
    ]

    for tx, title, sub, accent in targets:
        arrow_down(draw, tx, k_bot + 2, k_bot + 48, accent, width=3)
        box = (tx - 105, k_bot + 55, tx + 105, k_bot + 125)
        rounded_rect(draw, box, 12, fill=NAVY_CARD, outline=accent, width=2)
        center_text(draw, (tx, k_bot + 78), title, f_box_sm, WHITE)
        center_text(draw, (tx, k_bot + 105), sub, f_label, SLATE_400)

    # footer caption
    center_text(
        draw,
        (cx, H - 28),
        "A denied call never runs.  Delegation waits for the operator when required.",
        f_sub,
        SLATE_400,
    )

    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
