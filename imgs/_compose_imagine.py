#!/usr/bin/env python3
"""Compose Imagine bases + clean text overlays into README assets.

Visuals = Imagine. Labels = PIL (exact, README-accurate).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
SESS = Path(
    "/home/jon/.grok/sessions/"
    "%2Fhome%2Fjon%2Fdata%2FProjects%2Fsalient-core-public/"
    "019fae5c-458a-7af1-81bb-a6685805beb8/images"
)

INTER = Path(
    "/home/jon/.claude/skills/salient-tutor-design/assets/fonts/Inter-VF.ttf"
)
FIRA = Path(
    "/home/jon/.claude/skills/salient-tutor-design/assets/fonts/FiraCode-VF.ttf"
)
NOTO_BOLD = "/usr/share/fonts/noto/NotoSans-Bold.ttf"
NOTO_REG = "/usr/share/fonts/noto/NotoSans-Regular.ttf"
NOTO_MONO = "/usr/share/fonts/noto/NotoSansMono-Regular.ttf"

NAVY = (2, 16, 31)
CYAN = (68, 245, 255)
CYAN_BRIGHT = (180, 250, 255)
WHITE = (255, 255, 255)
SLATE = (148, 163, 184)
EMERALD = (52, 211, 153)
ROSE = (251, 113, 133)
AMBER = (251, 191, 36)
VIOLET = (167, 139, 250)
ORANGE = (251, 146, 60)
SKY = (56, 189, 248)
INDIGO = (129, 140, 248)


def face(path: str | Path, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(path), size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
    return f


def bold(size: int) -> ImageFont.FreeTypeFont:
    return face(INTER if INTER.exists() else NOTO_BOLD, size, 700)


def reg(size: int) -> ImageFont.FreeTypeFont:
    return face(INTER if INTER.exists() else NOTO_REG, size, 400)


def med(size: int) -> ImageFont.FreeTypeFont:
    return face(INTER if INTER.exists() else NOTO_REG, size, 550)


def mono(size: int) -> ImageFont.FreeTypeFont:
    return face(FIRA if FIRA.exists() else NOTO_MONO, size)


def tw(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont) -> int:
    b = draw.textbbox((0, 0), text, font=f)
    return b[2] - b[0]


def th(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont) -> int:
    b = draw.textbbox((0, 0), text, font=f)
    return b[3] - b[1]


def load_up(name: str, size: tuple[int, int]) -> Image.Image:
    im = Image.open(SESS / name).convert("RGB")
    return im.resize(size, Image.Resampling.LANCZOS)


def chip(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    f: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] = WHITE,
    bg: tuple[int, int, int] = NAVY,
    pad: int = 10,
    radius: int = 10,
    alpha_bg: int = 200,
    img: Image.Image | None = None,
) -> None:
    """Semi-transparent label chip centered on xy (or left-anchored if img given with mode)."""
    w, h = tw(draw, text, f), th(draw, text, f)
    x, y = xy
    box = (
        int(x - w / 2 - pad),
        int(y - h / 2 - pad // 2),
        int(x + w / 2 + pad),
        int(y + h / 2 + pad // 2),
    )
    if img is not None:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rounded_rectangle(box, radius=radius, fill=(*bg, alpha_bg))
        img.alpha_composite(overlay)
        draw = ImageDraw.Draw(img)
    else:
        draw.rounded_rectangle(box, radius=radius, fill=bg)
    draw.text((x - w / 2, y - h / 2), text, font=f, fill=fill)


def label_chip(
    img: Image.Image,
    xy: tuple[float, float],
    lines: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int]]],
    bg: tuple[int, int, int] = NAVY,
    alpha: int = 210,
    pad: int = 12,
    gap: int = 4,
    radius: int = 12,
) -> None:
    """Multi-line centered chip."""
    # measure
    widths = []
    heights = []
    for text, f, _ in lines:
        # temp draw
        d = ImageDraw.Draw(img)
        widths.append(tw(d, text, f))
        heights.append(th(d, text, f))
    total_h = sum(heights) + gap * (len(lines) - 1)
    max_w = max(widths) if widths else 0
    cx, cy = xy
    box = (
        int(cx - max_w / 2 - pad),
        int(cy - total_h / 2 - pad),
        int(cx + max_w / 2 + pad),
        int(cy + total_h / 2 + pad),
    )
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(box, radius=radius, fill=(*bg, alpha))
    img.alpha_composite(overlay)
    draw = ImageDraw.Draw(img)
    y = cy - total_h / 2
    for (text, f, color), w, h in zip(lines, widths, heights):
        draw.text((cx - w / 2, y), text, font=f, fill=color)
        y += h + gap


def title_bar(
    img: Image.Image,
    title: str,
    subtitle: str,
    y: float = 0.04,
) -> None:
    W, H = img.size
    f_t = bold(max(28, W // 45))
    f_s = reg(max(16, W // 80))
    draw = ImageDraw.Draw(img)
    # dark gradient top
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    band = int(H * 0.14)
    for i in range(band):
        a = int(210 * (1 - i / band) ** 0.7)
        od.line([(0, i), (W, i)], fill=(*NAVY, a))
    img.alpha_composite(overlay)
    draw = ImageDraw.Draw(img)
    cx = W / 2
    ty = H * y
    draw.text((cx - tw(draw, title, f_t) / 2, ty), title, font=f_t, fill=CYAN_BRIGHT)
    draw.text(
        (cx - tw(draw, subtitle, f_s) / 2, ty + th(draw, title, f_t) + 8),
        subtitle,
        font=f_s,
        fill=SLATE,
    )


# ---------------------------------------------------------------------------
# Per-asset composers
# ---------------------------------------------------------------------------
def social_preview() -> Path:
    img = load_up("7.jpg", (1920, 1080)).convert("RGBA")
    W, H = img.size
    # left darken
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(int(W * 0.50)):
        a = int(165 * (1 - i / (W * 0.50)) ** 1.3)
        od.line([(i, 0), (i, H)], fill=(*NAVY, a))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    f_title = bold(96)
    f_tag = reg(32)
    f_chip = mono(26)
    f_url = mono(22)
    left = int(W * 0.055)

    title = "salient-core"
    tag = "an agent-control kernel for multi-agent systems"
    lic = "  Apache-2.0  "
    url = "github.com/baggybin/salient-core"

    # glow
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text((left, int(H * 0.30)), title, font=f_title, fill=(*CYAN_BRIGHT, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(20))
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    draw.text((left, int(H * 0.30)), title, font=f_title, fill=CYAN_BRIGHT)
    draw.text((left, int(H * 0.48)), tag, font=f_tag, fill=CYAN)
    chip_y = int(H * 0.58)
    cw = tw(draw, lic, f_chip)
    ch = th(draw, lic, f_chip)
    draw.rounded_rectangle(
        (left, chip_y - 6, left + cw + 12, chip_y + ch + 10),
        radius=14,
        fill=CYAN,
    )
    draw.text((left + 6, chip_y), lic, font=f_chip, fill=NAVY)
    draw.text((left, int(H * 0.70)), url, font=f_url, fill=(90, 130, 160))

    out = ROOT / "social-preview.jpg"
    img.convert("RGB").save(out, "JPEG", quality=94, optimize=True, subsampling=1)
    return out


def hero_bus() -> Path:
    # Control-plane metaphor image
    img = load_up("2.jpg", (1920, 1080))
    out = ROOT / "hero-bus.jpg"
    img.save(out, "JPEG", quality=94, optimize=True, subsampling=1)
    return out


def without_kernel() -> Path:
    img = load_up("10.jpg", (1600, 1600)).convert("RGBA")
    W, H = img.size
    # top/bottom bands
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(int(H * 0.12)):
        a = int(230 * (1 - i / (H * 0.12)) ** 0.5)
        od.line([(0, i), (W, i)], fill=(*NAVY, a))
    for i in range(int(H * 0.12)):
        a = int(230 * (1 - i / (H * 0.12)) ** 0.5)
        od.line([(0, H - 1 - i), (W, H - 1 - i)], fill=(*NAVY, a))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    f_h = bold(38)
    f_c = reg(22)
    mid = W // 2
    for cx, head, color in (
        (mid // 2, "without a kernel", WHITE),
        (mid + mid // 2, "with salient-core", CYAN_BRIGHT),
    ):
        draw.text((cx - tw(draw, head, f_h) / 2, H * 0.03), head, font=f_h, fill=color)
    for cx, cap in (
        (mid // 2, "cycles · stalls · leaked intent"),
        (mid + mid // 2, "typed bus · cycle detection · gates"),
    ):
        draw.text(
            (cx - tw(draw, cap, f_c) / 2, H * 0.93),
            cap,
            font=f_c,
            fill=(120, 155, 185),
        )
    out = ROOT / "without-kernel-comparison.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def control_surfaces() -> Path:
    img = load_up("9.jpg", (1920, 1080)).convert("RGBA")
    W, H = img.size
    title_bar(img, "The control ladder", "Five rungs under the model — not in the prompt")

    # Labels under the five pillars (approximate isometric positions)
    rungs = [
        ("Capability", "tool surface", EMERALD),
        ("Action", "scope + safeguards", ROSE),
        ("Delegation", "typed bus", CYAN),
        ("Budget", "token ledger", AMBER),
        ("Stop", "quiesce()", VIOLET),
    ]
    # pillars sit roughly across lower-middle of frame
    xs = [0.22, 0.36, 0.50, 0.64, 0.78]
    y = H * 0.88
    f_t = bold(22)
    f_s = reg(15)
    for x_frac, (title, sub, color) in zip(xs, rungs):
        label_chip(
            img,
            (W * x_frac, y),
            [(title, f_t, color), (sub, f_s, SLATE)],
            alpha=220,
            pad=10,
            gap=2,
        )

    out = ROOT / "control-surfaces.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def kernel_position() -> Path:
    img = load_up("5.jpg", (1920, 1080)).convert("RGBA")
    W, H = img.size
    title_bar(
        img,
        "Where the kernel sits",
        "Control lives below the model — not in the prompt",
    )

    # LLM above cloud
    label_chip(
        img,
        (W * 0.50, H * 0.18),
        [( "LLM / agent loop", bold(22), CYAN_BRIGHT)],
        alpha=200,
    )
    # kernel chassis
    label_chip(
        img,
        (W * 0.50, H * 0.42),
        [
            ("salient-core", bold(26), CYAN),
            ("policy · bus · audit · inbox", med(16), SLATE),
        ],
        alpha=200,
    )
    # three outs below
    bottoms = [
        (0.22, "Tools", "scoped + gated", EMERALD),
        (0.50, "Other agents", "bus-mediated", CYAN),
        (0.78, "Operator", "human-in-the-loop", VIOLET),
    ]
    for xf, title, sub, color in bottoms:
        label_chip(
            img,
            (W * xf, H * 0.90),
            [(title, bold(18), color), (sub, reg(14), SLATE)],
            alpha=220,
            pad=10,
        )

    out = ROOT / "kernel-position.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def policy_gate_flow() -> Path:
    img = load_up("6.jpg", (1920, 1080)).convert("RGBA")
    W, H = img.size
    title_bar(
        img,
        "Policy gates — default deny",
        "Every tool call classified below the model, every transport",
    )

    label_chip(
        img,
        (W * 0.50, H * 0.42),
        [
            ("Scope + safeguard gates", bold(24), CYAN_BRIGHT),
            ("capability ≠ authorization · fail closed", reg(15), SLATE),
        ],
        alpha=210,
    )
    label_chip(
        img,
        (W * 0.28, H * 0.62),
        [("DENY", bold(28), ROSE), ("never runs", reg(15), SLATE)],
        alpha=220,
    )
    label_chip(
        img,
        (W * 0.72, H * 0.62),
        [("ALLOW", bold(28), EMERALD), ("executes · audited", reg(15), SLATE)],
        alpha=220,
    )
    label_chip(
        img,
        (W * 0.28, H * 0.88),
        [("1. Shadow", bold(18), AMBER), ("record would-deny", reg(14), SLATE)],
        alpha=220,
    )
    label_chip(
        img,
        (W * 0.72, H * 0.88),
        [("2. Enforce", bold(18), EMERALD), ("enforce_builtin_policy", mono(13), SLATE)],
        alpha=220,
    )
    # transport chips top
    transports = [
        (0.18, "SDK"),
        (0.34, "Bus"),
        (0.50, "MCP"),
        (0.66, "Text"),
        (0.82, "Provider"),
    ]
    for xf, name in transports:
        label_chip(
            img,
            (W * xf, H * 0.20),
            [(name, med(15), WHITE)],
            alpha=200,
            pad=8,
            radius=8,
        )

    out = ROOT / "policy-gate-flow.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def delegation_flow() -> Path:
    img = load_up("4.jpg", (1920, 1080)).convert("RGBA")
    W, H = img.size
    title_bar(
        img,
        "Delegation & the operator inbox",
        "Typed MCP bus · humans hold the kill-switches",
    )

    label_chip(
        img,
        (W * 0.50, H * 0.14),
        [("Operator", bold(22), VIOLET), ("inbox · kill-switch · Q/A", reg(14), SLATE)],
        alpha=210,
    )
    label_chip(
        img,
        (W * 0.50, H * 0.55),
        [
            ("Typed bus (MCP)", bold(24), CYAN_BRIGHT),
            ("31 tools · cycle detection", reg(15), SLATE),
        ],
        alpha=210,
    )
    for xf, yf, name in (
        (0.12, 0.38, "Agent A"),
        (0.88, 0.38, "Agent B"),
        (0.12, 0.78, "Agent C"),
        (0.88, 0.78, "Agent D"),
    ):
        label_chip(
            img,
            (W * xf, H * yf),
            [(name, bold(16), WHITE), ("scoped tools", reg(13), SLATE)],
            alpha=210,
            pad=8,
        )

    out = ROOT / "delegation-flow.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def kernel_components() -> Path:
    img = load_up("8.jpg", (1920, 1080)).convert("RGBA")
    W, H = img.size
    title_bar(
        img,
        "What's in the kernel",
        "Mechanism only — domain skins plug in at the seams",
    )

    # Labels centered on each glowing tile (isometric board, back→front L→R).
    modules = [
        # back row: pink · amber · violet · cyan
        (0.31, 0.34, "Policy gates", ROSE),
        (0.44, 0.30, "Audit trail", AMBER),
        (0.56, 0.36, "Operator inbox", VIOLET),
        (0.72, 0.40, "Bus-as-MCP", CYAN),
        # front row: green · orange · sky · indigo
        (0.34, 0.52, "Noisy-OR KG", EMERALD),
        (0.48, 0.50, "Token budgets", ORANGE),
        (0.60, 0.54, "Runner", SKY),
        (0.72, 0.62, "SM-2 scheduler", INDIGO),
    ]
    f_t = bold(15)
    for xf, yf, name, color in modules:
        label_chip(
            img,
            (W * xf, H * yf),
            [(name, f_t, WHITE)],
            bg=NAVY,
            alpha=235,
            pad=8,
            radius=8,
        )

    label_chip(
        img,
        (W * 0.50, H * 0.92),
        [
            (
                "seams: DaemonServices · ToolBuilder · AgentBackend · AgentProvider",
                mono(14),
                SLATE,
            )
        ],
        alpha=220,
        pad=12,
    )

    out = ROOT / "kernel-components.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def main() -> None:
    outs = [
        social_preview(),
        hero_bus(),
        without_kernel(),
        control_surfaces(),
        kernel_position(),
        policy_gate_flow(),
        delegation_flow(),
        kernel_components(),
    ]
    for p in outs:
        im = Image.open(p)
        print(f"wrote {p.name:32s}  {im.size[0]}x{im.size[1]:4d}  {p.stat().st_size // 1024:5d} KB")


if __name__ == "__main__":
    main()
