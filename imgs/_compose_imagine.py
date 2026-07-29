#!/usr/bin/env python3
"""Compose Imagine bases + uniform clean text overlays for README assets.

Design rules (all diagrams share the same system):
  - Canvas: 1920×1080 (16:9) except comparison 1600×1600
  - Title bar: fixed top band, cyan-bright title + slate subtitle
  - Chips: solid navy glass, 1px cyan-dim border, identical padding/radius
  - Type scale: title 34 / sub 17 / label 18 / meta 14 / mono 13
  - Accent color only on the primary label line; meta always slate
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

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

# --- Palette ---
NAVY = (2, 16, 31)
NAVY_CHIP = (6, 22, 40)
CYAN = (68, 245, 255)
CYAN_BRIGHT = (200, 252, 255)
CYAN_DIM = (48, 140, 165)
SLATE = (160, 176, 196)
SLATE_DIM = (110, 130, 155)
WHITE = (245, 250, 255)
EMERALD = (52, 211, 153)
ROSE = (251, 113, 133)
AMBER = (251, 191, 36)
VIOLET = (167, 139, 250)
ORANGE = (251, 146, 60)
SKY = (56, 189, 248)
INDIGO = (129, 140, 248)

# --- Type scale (px at 1920-wide; ~2× what shows at README width=900) ---
T_TITLE = 42
T_SUB = 20
T_LABEL = 20
T_META = 15
T_MONO = 14
T_SOCIAL_TITLE = 92
T_SOCIAL_TAG = 32
T_SOCIAL_CHIP = 24
T_SOCIAL_URL = 22

CHIP_PAD_X = 16
CHIP_PAD_Y = 11
CHIP_GAP = 4
CHIP_RADIUS = 11
CHIP_BORDER = 2
CHIP_ALPHA = 242
TITLE_BAND_H = 0.14  # fraction of height


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


def semibold(size: int) -> ImageFont.FreeTypeFont:
    return face(INTER if INTER.exists() else NOTO_BOLD, size, 600)


def reg(size: int) -> ImageFont.FreeTypeFont:
    return face(INTER if INTER.exists() else NOTO_REG, size, 400)


def mono(size: int) -> ImageFont.FreeTypeFont:
    return face(FIRA if FIRA.exists() else NOTO_MONO, size)


def measure(text: str, f: ImageFont.FreeTypeFont) -> tuple[int, int]:
    # Use a throwaway image for bbox
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    b = d.textbbox((0, 0), text, font=f)
    return b[2] - b[0], b[3] - b[1]


def load_base(name: str, size: tuple[int, int], contrast: float = 1.08, darken: float = 0.88) -> Image.Image:
    """Upscale Imagine base, slightly darken + contrast for text legibility."""
    im = Image.open(SESS / name).convert("RGB")
    im = im.resize(size, Image.Resampling.LANCZOS)
    im = ImageEnhance.Contrast(im).enhance(contrast)
    im = ImageEnhance.Brightness(im).enhance(darken)
    return im.convert("RGBA")


def soft_vignette(img: Image.Image, strength: int = 90) -> Image.Image:
    """Gentle edge darken so chips and title read better."""
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # top band for title
    band = int(H * TITLE_BAND_H)
    for i in range(band):
        a = int(strength * 1.4 * (1 - i / band) ** 0.65)
        od.line([(0, i), (W, i)], fill=(*NAVY, min(230, a)))
    # bottom fade
    bot = int(H * 0.10)
    for i in range(bot):
        a = int(strength * 0.9 * (1 - i / bot) ** 0.7)
        od.line([(0, H - 1 - i), (W, H - 1 - i)], fill=(*NAVY, min(200, a)))
    return Image.alpha_composite(img, overlay)


def draw_title(img: Image.Image, title: str, subtitle: str) -> None:
    """Uniform top title + subtitle, centered."""
    W, H = img.size
    f_t = bold(T_TITLE)
    f_s = reg(T_SUB)
    tw_t, th_t = measure(title, f_t)
    tw_s, th_s = measure(subtitle, f_s)
    # subtle text shadow for clarity
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    cx = W / 2
    y0 = H * 0.028
    for dx, dy in ((1, 1), (0, 2), (2, 0)):
        sd.text((cx - tw_t / 2 + dx, y0 + dy), title, font=f_t, fill=(0, 0, 0, 90))
        sd.text((cx - tw_s / 2 + dx, y0 + th_t + 8 + dy), subtitle, font=f_s, fill=(0, 0, 0, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(1.5))
    img.alpha_composite(shadow)
    draw = ImageDraw.Draw(img)
    draw.text((cx - tw_t / 2, y0), title, font=f_t, fill=CYAN_BRIGHT)
    draw.text((cx - tw_s / 2, y0 + th_t + 8), subtitle, font=f_s, fill=SLATE)


def chip(
    img: Image.Image,
    xy: tuple[float, float],
    primary: str,
    secondary: str | None = None,
    accent: tuple[int, int, int] = CYAN,
    border: tuple[int, int, int] | None = None,
    mono_secondary: bool = False,
    min_width: int = 0,
) -> None:
    """Uniform two-line (or one-line) label chip, centered on xy.

    Primary text is always white for max clarity; accent only on bar + border.
    """
    f_p = semibold(T_LABEL)
    f_s = mono(T_MONO) if mono_secondary else reg(T_META)
    lines: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int]]] = [
        (primary, f_p, WHITE),
    ]
    if secondary:
        lines.append((secondary, f_s, SLATE))

    widths = [measure(t, f)[0] for t, f, _ in lines]
    heights = [measure(t, f)[1] for t, f, _ in lines]
    content_w = max(widths + [min_width])
    content_h = sum(heights) + (CHIP_GAP if secondary else 0)

    cx, cy = xy
    box = (
        int(cx - content_w / 2 - CHIP_PAD_X),
        int(cy - content_h / 2 - CHIP_PAD_Y),
        int(cx + content_w / 2 + CHIP_PAD_X),
        int(cy + content_h / 2 + CHIP_PAD_Y),
    )

    # Drop shadow under chip
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sbox = (box[0] + 2, box[1] + 3, box[2] + 2, box[3] + 3)
    sd.rounded_rectangle(sbox, radius=CHIP_RADIUS, fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(4))
    img.alpha_composite(shadow)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # solid chip body
    od.rounded_rectangle(box, radius=CHIP_RADIUS, fill=(*NAVY_CHIP, CHIP_ALPHA))
    bcol = border or accent
    od.rounded_rectangle(box, radius=CHIP_RADIUS, outline=(*bcol, 220), width=CHIP_BORDER)
    # left accent bar
    bar_x0 = box[0] + 5
    bar_y0 = box[1] + 7
    bar_y1 = box[3] - 7
    od.rounded_rectangle(
        (bar_x0, bar_y0, bar_x0 + 4, bar_y1),
        radius=2,
        fill=(*accent, 255),
    )
    img.alpha_composite(overlay)

    draw = ImageDraw.Draw(img)
    y = box[1] + CHIP_PAD_Y
    for (text, f, color), w, h in zip(lines, widths, heights):
        # center in chip (slight right bias past accent bar)
        tx = cx - w / 2 + 3
        draw.text((tx, y), text, font=f, fill=color)
        y += h + CHIP_GAP


def save_png(img: Image.Image, name: str) -> Path:
    out = ROOT / name
    rgb = img.convert("RGB")
    rgb.save(out, "PNG", optimize=True)
    return out


def save_jpg(img: Image.Image, name: str, quality: int = 94) -> Path:
    out = ROOT / name
    img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True, subsampling=1)
    return out


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
def social_preview() -> Path:
    img = load_base("7.jpg", (1920, 1080), contrast=1.05, darken=0.92)
    W, H = img.size

    # Left readability gradient
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    span = int(W * 0.52)
    for i in range(span):
        a = int(175 * (1 - i / span) ** 1.25)
        od.line([(i, 0), (i, H)], fill=(*NAVY, a))
    img = Image.alpha_composite(img, overlay)

    f_title = bold(T_SOCIAL_TITLE)
    f_tag = reg(T_SOCIAL_TAG)
    f_chip = mono(T_SOCIAL_CHIP)
    f_url = mono(T_SOCIAL_URL)

    left = int(W * 0.055)
    title = "salient-core"
    tag = "an agent-control kernel for multi-agent systems"
    lic = "Apache-2.0"
    url = "github.com/baggybin/salient-core"

    # Glow behind title
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text((left, int(H * 0.30)), title, font=f_title, fill=(*CYAN_BRIGHT, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(22))
    img = Image.alpha_composite(img, glow)

    draw = ImageDraw.Draw(img)
    # title shadow
    for dx, dy in ((2, 2), (0, 3)):
        draw.text((left + dx, int(H * 0.30) + dy), title, font=f_title, fill=(0, 0, 0, 100))
    draw.text((left, int(H * 0.30)), title, font=f_title, fill=CYAN_BRIGHT)
    draw.text((left, int(H * 0.475)), tag, font=f_tag, fill=CYAN)

    # license pill — same geometry language as diagram chips
    chip_text = f"  {lic}  "
    cw, ch = measure(chip_text, f_chip)
    chip_y = int(H * 0.575)
    pill = (left, chip_y - 4, left + cw + 8, chip_y + ch + 8)
    draw.rounded_rectangle(pill, radius=14, fill=CYAN)
    draw.text((left + 4, chip_y), chip_text, font=f_chip, fill=NAVY)
    draw.text((left, int(H * 0.695)), url, font=f_url, fill=SLATE_DIM)

    return save_jpg(img, "social-preview.jpg")


def hero_bus() -> Path:
    img = load_base("2.jpg", (1920, 1080), contrast=1.06, darken=0.95)
    return save_jpg(img, "hero-bus.jpg")


def without_kernel() -> Path:
    img = load_base("10.jpg", (1600, 1600), contrast=1.06, darken=0.90)
    W, H = img.size

    # Solid readable bands (top + bottom) with soft fade into art
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    top_h, bot_h = int(H * 0.11), int(H * 0.11)
    od.rectangle((0, 0, W, top_h), fill=(*NAVY, 245))
    od.rectangle((0, H - bot_h, W, H), fill=(*NAVY, 245))
    for i in range(36):
        a = int(200 * (1 - i / 36))
        od.line([(0, top_h + i), (W, top_h + i)], fill=(*NAVY, a))
        od.line([(0, H - bot_h - i), (W, H - bot_h - i)], fill=(*NAVY, a))
    # divider through bands
    mid = W // 2
    od.rectangle((mid - 1, 0, mid + 1, top_h + 30), fill=(*CYAN_DIM, 200))
    od.rectangle((mid - 1, H - bot_h - 30, mid + 1, H), fill=(*CYAN_DIM, 200))
    img = Image.alpha_composite(img, overlay)

    f_h = bold(36)
    f_c = reg(20)
    draw = ImageDraw.Draw(img)

    def ctext(cx: float, y: float, text: str, f, fill):
        w, h = measure(text, f)
        draw.text((cx - w / 2, y), text, font=f, fill=fill)

    ctext(mid / 2, H * 0.035, "without a kernel", f_h, WHITE)
    ctext(mid + mid / 2, H * 0.035, "with salient-core", f_h, CYAN_BRIGHT)
    ctext(mid / 2, H * 0.935, "cycles · stalls · leaked intent", f_c, SLATE_DIM)
    ctext(mid + mid / 2, H * 0.935, "typed bus · cycle detection · gates", f_c, SLATE_DIM)

    return save_png(img, "without-kernel-comparison.png")


def control_surfaces() -> Path:
    img = load_base("9.jpg", (1920, 1080), contrast=1.1, darken=0.86)
    img = soft_vignette(img, strength=100)
    draw_title(
        img,
        "The control ladder",
        "Five rungs under the model — none of them a prompt instruction",
    )
    W, H = img.size

    rungs = [
        ("Capability", "one tool surface", EMERALD),
        ("Action", "scope + safeguards", ROSE),
        ("Delegation", "typed bus + cycles", CYAN),
        ("Budget", "token ledger", AMBER),
        ("Stop", "quiesce() evidence", VIOLET),
    ]
    # Even spacing under the isometric pillars (narrower than full width)
    margin = 0.18
    usable = 1.0 - 2 * margin
    y = H * 0.905
    for i, (title, sub, color) in enumerate(rungs):
        x = W * (margin + usable * (i + 0.5) / len(rungs))
        chip(img, (x, y), title, sub, accent=color, border=color, min_width=130)

    return save_png(img, "control-surfaces.png")


def kernel_position() -> Path:
    img = load_base("5.jpg", (1920, 1080), contrast=1.08, darken=0.88)
    img = soft_vignette(img, strength=95)
    draw_title(
        img,
        "Where the kernel sits",
        "Control lives below the model — not in the prompt",
    )
    W, H = img.size

    chip(img, (W * 0.50, H * 0.175), "LLM / agent loop", "Claude · Codex · polybrain", accent=CYAN_BRIGHT)
    chip(
        img,
        (W * 0.50, H * 0.42),
        "salient-core",
        "policy · bus · audit · inbox",
        accent=CYAN,
        border=CYAN,
    )
    bottoms = [
        (0.20, "Tools", "scoped + gated", EMERALD),
        (0.50, "Other agents", "bus-mediated", CYAN),
        (0.80, "Operator", "human-in-the-loop", VIOLET),
    ]
    for xf, title, sub, color in bottoms:
        chip(img, (W * xf, H * 0.905), title, sub, accent=color, border=color, min_width=110)

    return save_png(img, "kernel-position.png")


def policy_gate_flow() -> Path:
    img = load_base("6.jpg", (1920, 1080), contrast=1.1, darken=0.85)
    img = soft_vignette(img, strength=100)
    draw_title(
        img,
        "Policy gates — default deny",
        "Every tool call classified below the model, on every transport",
    )
    W, H = img.size

    transports = [
        (0.16, "SDK built-ins", SKY),
        (0.33, "Bus tools", CYAN),
        (0.50, "External MCP", VIOLET),
        (0.67, "Text commands", ORANGE),
        (0.84, "Provider", INDIGO),
    ]
    for xf, name, color in transports:
        chip(img, (W * xf, H * 0.195), name, accent=color, border=color, min_width=90)

    chip(
        img,
        (W * 0.50, H * 0.42),
        "Scope + safeguard gates",
        "capability ≠ authorization · fail closed",
        accent=CYAN_BRIGHT,
        border=CYAN,
        min_width=220,
    )
    chip(img, (W * 0.26, H * 0.62), "DENY", "never runs · recorded", accent=ROSE, border=ROSE, min_width=100)
    chip(img, (W * 0.74, H * 0.62), "ALLOW", "executes · audited", accent=EMERALD, border=EMERALD, min_width=100)
    chip(
        img,
        (W * 0.28, H * 0.885),
        "1. Shadow mode",
        "record would-deny, still permit",
        accent=AMBER,
        border=AMBER,
        min_width=140,
    )
    chip(
        img,
        (W * 0.72, H * 0.885),
        "2. Enforce mode",
        "enforce_builtin_policy: true",
        accent=EMERALD,
        border=EMERALD,
        mono_secondary=True,
        min_width=140,
    )

    return save_png(img, "policy-gate-flow.png")


def delegation_flow() -> Path:
    img = load_base("4.jpg", (1920, 1080), contrast=1.08, darken=0.87)
    img = soft_vignette(img, strength=95)
    draw_title(
        img,
        "Delegation & the operator inbox",
        "Agents coordinate over a typed MCP bus — humans hold the kill-switches",
    )
    W, H = img.size

    chip(
        img,
        (W * 0.50, H * 0.155),
        "Operator",
        "inbox · kill-switch · typed Q/A",
        accent=VIOLET,
        border=VIOLET,
        min_width=140,
    )
    chip(
        img,
        (W * 0.50, H * 0.55),
        "Typed bus (MCP)",
        "31 tools · cycle detection",
        accent=CYAN_BRIGHT,
        border=CYAN,
        min_width=160,
    )
    agents = [
        (0.11, 0.36, "Agent A"),
        (0.89, 0.36, "Agent B"),
        (0.11, 0.80, "Agent C"),
        (0.89, 0.80, "Agent D"),
    ]
    for xf, yf, name in agents:
        chip(img, (W * xf, H * yf), name, "scoped tools", accent=WHITE, border=CYAN_DIM, min_width=90)

    return save_png(img, "delegation-flow.png")


def kernel_components() -> Path:
    img = load_base("8.jpg", (1920, 1080), contrast=1.1, darken=0.86)
    img = soft_vignette(img, strength=100)
    draw_title(
        img,
        "What's in the kernel",
        "Mechanism only — domain skins plug in at the seams",
    )
    W, H = img.size

    # Labels centered on each glowing tile (back row then front row)
    modules = [
        (0.295, 0.290, "Policy gates", ROSE),      # pink
        (0.430, 0.265, "Audit trail", AMBER),      # amber
        (0.550, 0.335, "Operator inbox", VIOLET),  # purple
        (0.710, 0.375, "Bus-as-MCP", CYAN),        # light cyan
        (0.325, 0.485, "Noisy-OR KG", EMERALD),    # green
        (0.470, 0.475, "Token budgets", ORANGE),   # orange
        (0.590, 0.520, "Runner", SKY),             # white/sky
        (0.710, 0.600, "SM-2 scheduler", INDIGO),  # indigo
    ]
    for xf, yf, name, color in modules:
        chip(img, (W * xf, H * yf), name, accent=color, border=color, min_width=110)

    chip(
        img,
        (W * 0.50, H * 0.93),
        "Seams",
        "DaemonServices · ToolBuilder · AgentBackend · AgentProvider",
        accent=CYAN,
        mono_secondary=True,
        min_width=280,
    )

    return save_png(img, "kernel-components.png")


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
