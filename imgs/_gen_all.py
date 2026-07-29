#!/usr/bin/env python3
"""Generate high-def README assets for salient-core.

Atmospheric bases come from Imagine; text and diagrams are code-rendered so
labels stay exact and match the README (control ladder, 31 tools, etc.).
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

# Prefer Inter VF from the design system; fall back to Noto.
INTER = Path(
    "/home/jon/.claude/skills/salient-tutor-design/assets/fonts/Inter-VF.ttf"
)
FIRA = Path(
    "/home/jon/.claude/skills/salient-tutor-design/assets/fonts/FiraCode-VF.ttf"
)
NOTO_BOLD = "/usr/share/fonts/noto/NotoSans-Bold.ttf"
NOTO_REG = "/usr/share/fonts/noto/NotoSans-Regular.ttf"
NOTO_MED = "/usr/share/fonts/noto/NotoSans-Medium.ttf"
NOTO_MONO = "/usr/share/fonts/noto/NotoSansMono-Regular.ttf"

# Palette — navy + cyan bus glow (matches brand of the hero art)
NAVY_BG = (2, 16, 31)
NAVY_PANEL = (8, 28, 48)
NAVY_CARD = (12, 36, 58)
NAVY_SOFT = (18, 42, 68)
NAVY_LINE = (10, 32, 52)
CYAN = (68, 245, 255)
CYAN_BRIGHT = (180, 250, 255)
CYAN_DIM = (40, 120, 140)
CYAN_DEEP = (24, 60, 88)
SLATE_200 = (226, 232, 240)
SLATE_300 = (203, 213, 225)
SLATE_400 = (148, 163, 184)
SLATE_500 = (100, 116, 139)
WHITE = (255, 255, 255)
EMERALD = (52, 211, 153)
EMERALD_DIM = (16, 120, 90)
AMBER = (251, 191, 36)
ROSE = (251, 113, 133)
VIOLET = (167, 139, 250)
ORANGE = (251, 146, 60)
SKY = (56, 189, 248)
INDIGO = (129, 140, 248)

SCALE = 2  # 2× retina diagrams
W0 = 1100  # logical width; actual = W0 * SCALE


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a face. kind: bold|reg|med|mono."""
    size = max(1, int(size * SCALE))
    if kind == "mono":
        path = str(FIRA) if FIRA.exists() else NOTO_MONO
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            return ImageFont.truetype(NOTO_MONO, size)
    if INTER.exists():
        # Inter VF: weight via variation if available
        f = ImageFont.truetype(str(INTER), size)
        weight = {"bold": 700, "med": 550, "reg": 400}.get(kind, 400)
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        return f
    path = {"bold": NOTO_BOLD, "med": NOTO_MED, "reg": NOTO_REG}[kind]
    return ImageFont.truetype(path, size)


def tw(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont) -> int:
    b = draw.textbbox((0, 0), text, font=f)
    return b[2] - b[0]


def th(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont) -> int:
    b = draw.textbbox((0, 0), text, font=f)
    return b[3] - b[1]


def center_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    f: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    draw.text(
        (xy[0] - tw(draw, text, f) / 2, xy[1] - th(draw, text, f) / 2),
        text,
        font=f,
        fill=fill,
    )


def left_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    f: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    draw.text(xy, text, font=f, fill=fill)


def s(n: float) -> int:
    """Scale a logical coordinate."""
    return int(n * SCALE)


def rr(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    fill=None,
    outline=None,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(
        box, radius=radius, fill=fill, outline=outline, width=max(1, width)
    )


def card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    accent: tuple[int, int, int],
    radius: int = 14,
    width: int = 2,
    fill: tuple[int, int, int] = NAVY_CARD,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=accent, width=width)


def grid_bg(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    step = s(40)
    for x in range(0, w, step):
        draw.line([(x, 0), (x, h)], fill=NAVY_LINE, width=1)
    for y in range(0, h, step):
        draw.line([(0, y), (w, y)], fill=NAVY_LINE, width=1)
    # soft vignette corners via sparse dots
    for x in range(s(20), w, s(80)):
        for y in range(s(20), h, s(80)):
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=(14, 40, 64))


def arrow_v(
    draw: ImageDraw.ImageDraw,
    x: int,
    y0: int,
    y1: int,
    color: tuple[int, int, int],
    width: int = 3,
) -> None:
    width = max(2, s(width // SCALE) if width >= 2 else width)
    width = max(2, int(3 * SCALE / 2)) if SCALE > 1 else 3
    head = s(10)
    if y1 > y0:
        draw.line([(x, y0), (x, y1 - head)], fill=color, width=width)
        draw.polygon(
            [(x, y1), (x - head // 2 - 2, y1 - head), (x + head // 2 + 2, y1 - head)],
            fill=color,
        )
    else:
        draw.line([(x, y0), (x, y1 + head)], fill=color, width=width)
        draw.polygon(
            [(x, y1), (x - head // 2 - 2, y1 + head), (x + head // 2 + 2, y1 + head)],
            fill=color,
        )


def arrow_h(
    draw: ImageDraw.ImageDraw,
    x0: int,
    x1: int,
    y: int,
    color: tuple[int, int, int],
    width: int = 3,
) -> None:
    width = max(2, int(3 * SCALE / 2))
    head = s(10)
    if x1 > x0:
        draw.line([(x0, y), (x1 - head, y)], fill=color, width=width)
        draw.polygon(
            [(x1, y), (x1 - head, y - head // 2 - 2), (x1 - head, y + head // 2 + 2)],
            fill=color,
        )
    else:
        draw.line([(x0, y), (x1 + head, y)], fill=color, width=width)
        draw.polygon(
            [(x1, y), (x1 + head, y - head // 2 - 2), (x1 + head, y + head // 2 + 2)],
            fill=color,
        )


def title_block(
    draw: ImageDraw.ImageDraw,
    cx: int,
    title: str,
    subtitle: str,
    y: int | None = None,
) -> int:
    y = s(36) if y is None else y
    f_title = font("bold", 26)
    f_sub = font("reg", 14)
    center_text(draw, (cx, y), title, f_title, CYAN_BRIGHT)
    center_text(draw, (cx, y + s(30)), subtitle, f_sub, SLATE_400)
    return y + s(58)


def accent_bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    color: tuple[int, int, int],
    w: int = 36,
) -> None:
    draw.rounded_rectangle((x, y, x + w, y + s(4)), radius=s(2), fill=color)


# ---------------------------------------------------------------------------
# Atmospheric: social-preview, hero-bus, without-kernel
# ---------------------------------------------------------------------------
def make_social_preview() -> Path:
    """Bus crystal scene + left typography."""
    src = SESS / "3.jpg"
    # Target OG-ish 16:9 at high def
    W, H = 1920, 1080
    base = Image.open(src).convert("RGB")
    base = base.resize((W, H), Image.Resampling.LANCZOS)

    # Darken left third slightly so type pops
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(int(W * 0.48)):
        alpha = int(150 * (1 - i / (W * 0.48)) ** 1.4)
        od.line([(i, 0), (i, H)], fill=(2, 16, 31, alpha))
    base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(base)
    left_x = int(W * 0.055)
    f_title = font("bold", 72 // SCALE)  # already *SCALE inside → ~72px at S=2? wait
    # font() multiplies by SCALE; for photo overlays we want absolute sizes
    f_title = ImageFont.truetype(
        str(INTER) if INTER.exists() else NOTO_BOLD, 92
    )
    f_tag = ImageFont.truetype(
        str(INTER) if INTER.exists() else NOTO_REG, 34
    )
    f_chip = ImageFont.truetype(
        str(FIRA) if FIRA.exists() else NOTO_MONO, 26
    )
    f_url = ImageFont.truetype(
        str(FIRA) if FIRA.exists() else NOTO_MONO, 24
    )
    try:
        f_title.set_variation_by_axes([700])
        f_tag.set_variation_by_axes([400])
    except Exception:
        pass

    title = "salient-core"
    tagline = "an agent-control kernel for multi-agent systems"
    license_ = "Apache-2.0"
    url = "github.com/baggybin/salient-core"

    # Soft glow behind title
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text((left_x, int(H * 0.30)), title, font=f_title, fill=(*CYAN_BRIGHT, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(18))
    base = Image.alpha_composite(base.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(base)

    draw.text((left_x, int(H * 0.30)), title, font=f_title, fill=CYAN_BRIGHT)
    draw.text((left_x, int(H * 0.48)), tagline, font=f_tag, fill=CYAN)

    chip_text = f"  {license_}  "
    # measure
    bb = draw.textbbox((0, 0), chip_text, font=f_chip)
    cw, ch = bb[2] - bb[0], bb[3] - bb[1]
    chip_y = int(H * 0.58)
    pad_x, pad_y = 10, 8
    draw.rounded_rectangle(
        (
            left_x - 2,
            chip_y - pad_y,
            left_x + cw + pad_x,
            chip_y + ch + pad_y,
        ),
        radius=14,
        fill=CYAN,
    )
    draw.text((left_x + 4, chip_y), chip_text, font=f_chip, fill=NAVY_BG)
    draw.text((left_x, int(H * 0.70)), url, font=f_url, fill=(90, 130, 160))

    out = ROOT / "social-preview.jpg"
    base.save(out, "JPEG", quality=94, optimize=True, subsampling=1)
    return out


def make_hero_bus() -> Path:
    """Control-plane metaphor — model above, kernel plane, tools/agents/op below."""
    src = SESS / "2.jpg"
    W, H = 1920, 1080
    base = Image.open(src).convert("RGB")
    base = base.resize((W, H), Image.Resampling.LANCZOS)
    out = ROOT / "hero-bus.jpg"
    base.save(out, "JPEG", quality=94, optimize=True, subsampling=1)
    return out


def make_without_kernel() -> Path:
    """Chaotic mesh vs typed bus — clean captions only."""
    src = SESS / "1.jpg"
    # Upscale to 1600 square for README width
    side = 1600
    base = Image.open(src).convert("RGB")
    base = base.resize((side, side), Image.Resampling.LANCZOS)
    W, H = base.size
    draw = ImageDraw.Draw(base)

    f_h = ImageFont.truetype(
        str(INTER) if INTER.exists() else NOTO_BOLD, 36
    )
    f_cap = ImageFont.truetype(
        str(INTER) if INTER.exists() else NOTO_REG, 22
    )
    try:
        f_h.set_variation_by_axes([700])
        f_cap.set_variation_by_axes([450])
    except Exception:
        pass

    # Top / bottom navy bands for readable captions
    top0, top1 = int(H * 0.0), int(H * 0.10)
    bot0, bot1 = int(H * 0.90), H
    # gradient-ish solid bands
    draw.rectangle((0, top0, W, top1), fill=NAVY_BG)
    draw.rectangle((0, bot0, W, bot1), fill=NAVY_BG)
    # soft fade into image
    fade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fade)
    for i in range(40):
        a = int(200 * (1 - i / 40))
        y = top1 + i
        fd.line([(0, y), (W, y)], fill=(*NAVY_BG, a))
        y2 = bot0 - i
        fd.line([(0, y2), (W, y2)], fill=(*NAVY_BG, a))
    base = Image.alpha_composite(base.convert("RGBA"), fade).convert("RGB")
    draw = ImageDraw.Draw(base)

    divider_x = W // 2
    # thin cyan divider through bands
    draw.rectangle((divider_x - 1, 0, divider_x + 1, top1 + 30), fill=CYAN_DEEP)
    draw.rectangle((divider_x - 1, bot0 - 30, divider_x + 1, H), fill=CYAN_DEEP)

    cx_l = divider_x // 2
    cx_r = divider_x + (W - divider_x) // 2
    head_y = int(H * 0.035)

    def ctext(cx, y, text, f, fill):
        bb = draw.textbbox((0, 0), text, font=f)
        draw.text((cx - (bb[2] - bb[0]) // 2, y), text, font=f, fill=fill)

    ctext(cx_l, head_y, "without a kernel", f_h, SLATE_200)
    ctext(cx_r, head_y, "with salient-core", f_h, CYAN_BRIGHT)

    cap_y = int(H * 0.93)
    cap_color = (120, 155, 185)
    ctext(cx_l, cap_y, "cycles · stalls · leaked intent", f_cap, cap_color)
    ctext(cx_r, cap_y, "typed bus · cycle detection · gates", f_cap, cap_color)

    out = ROOT / "without-kernel-comparison.png"
    base.save(out, "PNG", optimize=True)
    return out


# ---------------------------------------------------------------------------
# Diagrams
# ---------------------------------------------------------------------------
def gen_control_surfaces() -> Path:
    """Five rungs of the control ladder (README-accurate)."""
    W, H = s(W0), s(680)
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)
    grid_bg(draw, W, H)
    cx = W // 2
    title_block(
        draw,
        cx,
        "The control ladder",
        "Five rungs under the model — none of them a prompt instruction",
    )

    # Matches README "The control ladder" table exactly
    pillars = [
        (
            "Capability",
            "What tools does this\nagent even have?",
            "one tool surface\nper agent",
            EMERALD,
        ),
        (
            "Action",
            "May it make this call\non this target?",
            "scope + safeguards\ndefault-deny",
            ROSE,
        ),
        (
            "Delegation",
            "Who is it allowed\nto talk to?",
            "typed bus · cycles\noperator approval",
            CYAN,
        ),
        (
            "Budget",
            "How much may it spend\nbefore it stops?",
            "epoch-keyed ledger\nwarn → park → interrupt",
            AMBER,
        ),
        (
            "Stop",
            "Is it actually dead,\nor just quiet?",
            "quiesce() evidence\nor unverified",
            VIOLET,
        ),
    ]

    card_w, card_h = s(190), s(280)
    gap = s(18)
    total = 5 * card_w + 4 * gap
    x0 = (W - total) // 2
    y0 = s(110)
    f_num = font("bold", 18)
    f_title = font("bold", 16)
    f_q = font("reg", 12)
    f_a = font("med", 12)

    for i, (title, question, answer, accent) in enumerate(pillars):
        x = x0 + i * (card_w + gap)
        card(draw, (x, y0, x + card_w, y0 + card_h), accent, radius=s(14), width=s(2))
        # number chip
        chip = (x + s(14), y0 + s(14), x + s(48), y0 + s(48))
        draw.rounded_rectangle(chip, radius=s(8), fill=NAVY_SOFT, outline=accent, width=s(2))
        center_text(draw, (x + s(31), y0 + s(31)), str(i + 1), f_num, accent)
        center_text(draw, (x + card_w / 2, y0 + s(70)), title, f_title, WHITE)
        draw.line(
            [(x + s(28), y0 + s(90)), (x + card_w - s(28), y0 + s(90))],
            fill=accent,
            width=s(2),
        )
        # operator question
        for j, line in enumerate(question.split("\n")):
            center_text(
                draw, (x + card_w / 2, y0 + s(118) + j * s(18)), line, f_q, SLATE_400
            )
        # mechanism answer
        draw.line(
            [(x + s(36), y0 + s(168)), (x + card_w - s(36), y0 + s(168))],
            fill=CYAN_DEEP,
            width=1,
        )
        for j, line in enumerate(answer.split("\n")):
            center_text(
                draw, (x + card_w / 2, y0 + s(196) + j * s(18)), line, f_a, accent
            )

    # footer
    fy = y0 + card_h + s(28)
    draw.rounded_rectangle(
        (s(70), fy, W - s(70), fy + s(120)),
        radius=s(16),
        fill=NAVY_PANEL,
        outline=CYAN_DEEP,
        width=s(2),
    )
    f_h = font("bold", 16)
    f_p = font("reg", 14)
    center_text(draw, (cx, fy + s(32)), "Why below the model?", f_h, CYAN)
    lines = [
        "Prompts can be ignored. Confused or manipulated agents can talk themselves past safety text.",
        "Gates, budgets, and quiesce sit under the loop — a denied call never runs, and a stop is only a stop",
        "when you can point at the dead subprocess (or the kernel says unverified).",
    ]
    for j, line in enumerate(lines):
        center_text(draw, (cx, fy + s(62) + j * s(20)), line, f_p, SLATE_400)

    out = ROOT / "control-surfaces.png"
    img.save(out, "PNG", optimize=True)
    return out


def gen_policy_gate_flow() -> Path:
    W, H = s(W0), s(760)
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)
    grid_bg(draw, W, H)
    cx = W // 2
    title_block(
        draw,
        cx,
        "Policy gates — default deny",
        "Every tool invocation is classified below the model, on every transport",
    )

    f_box = font("bold", 14)
    f_sm = font("reg", 12)
    f_chip = font("med", 12)
    f_mono = font("mono", 11)

    # Five transports (README-accurate)
    sources = [
        ("SDK built-ins", SKY),
        ("Bus tools", CYAN),
        ("External MCP", VIOLET),
        ("Text commands", ORANGE),
        ("Provider runtimes", INDIGO),
    ]
    sw, sh = s(170), s(54)
    gap = s(16)
    total = 5 * sw + 4 * gap
    sx = (W - total) // 2
    y = s(108)
    for i, (label, accent) in enumerate(sources):
        x = sx + i * (sw + gap)
        card(draw, (x, y, x + sw, y + sh), accent, radius=s(10), width=s(2))
        center_text(draw, (x + sw / 2, y + sh / 2), label, f_box, WHITE)

    gate_y = y + sh + s(70)
    for i in range(5):
        x = sx + i * (sw + gap) + sw // 2
        draw.line([(x, y + sh + s(4)), (cx, gate_y - s(4))], fill=CYAN_DIM, width=s(2))

    gw, gh = s(480), s(110)
    gx0 = cx - gw // 2
    card(draw, (gx0, gate_y, gx0 + gw, gate_y + gh), CYAN, radius=s(14), width=s(3), fill=NAVY_PANEL)
    center_text(draw, (cx, gate_y + s(28)), "Scope + safeguard gates", font("bold", 17), CYAN_BRIGHT)
    center_text(
        draw,
        (cx, gate_y + s(56)),
        "transport-neutral  ·  capability ≠ authorization",
        f_sm,
        SLATE_400,
    )
    center_text(
        draw,
        (cx, gate_y + s(82)),
        "unclassified tools fail closed",
        f_mono,
        ROSE,
    )

    # Allow / Deny
    split_y = gate_y + gh + s(18)
    arrow_v(draw, cx - s(180), gate_y + gh + s(2), split_y + s(40), EMERALD)
    arrow_v(draw, cx + s(180), gate_y + gh + s(2), split_y + s(40), ROSE)

    allow = (cx - s(320), split_y + s(48), cx - s(40), split_y + s(150))
    deny = (cx + s(40), split_y + s(48), cx + s(320), split_y + s(150))
    card(draw, allow, EMERALD, radius=s(12), width=s(2))
    card(draw, deny, ROSE, radius=s(12), width=s(2))
    center_text(draw, ((allow[0] + allow[2]) / 2, split_y + s(78)), "ALLOW", font("bold", 18), EMERALD)
    center_text(
        draw,
        ((allow[0] + allow[2]) / 2, split_y + s(112)),
        "tool executes  ·  I/O audited",
        f_sm,
        SLATE_400,
    )
    center_text(draw, ((deny[0] + deny[2]) / 2, split_y + s(78)), "DENY", font("bold", 18), ROSE)
    center_text(
        draw,
        ((deny[0] + deny[2]) / 2, split_y + s(112)),
        "never runs  ·  denial recorded",
        f_sm,
        SLATE_400,
    )

    # Staged trust strip
    strip_y = split_y + s(180)
    draw.rounded_rectangle(
        (s(90), strip_y, W - s(90), strip_y + s(130)),
        radius=s(14),
        fill=NAVY_PANEL,
        outline=VIOLET,
        width=s(2),
    )
    center_text(
        draw,
        (cx, strip_y + s(28)),
        "Staged trust: shadow, then enforce",
        font("bold", 15),
        VIOLET,
    )
    s1 = (s(130), strip_y + s(52), s(500), strip_y + s(110))
    s2 = (s(600), strip_y + s(52), s(970), strip_y + s(110))
    card(draw, s1, AMBER, radius=s(10), width=s(2))
    card(draw, s2, EMERALD, radius=s(10), width=s(2))
    center_text(draw, ((s1[0] + s1[2]) / 2, strip_y + s(70)), "1. Shadow mode", f_chip, AMBER)
    center_text(
        draw,
        ((s1[0] + s1[2]) / 2, strip_y + s(94)),
        "record would-deny, still permit",
        f_sm,
        SLATE_400,
    )
    center_text(draw, ((s2[0] + s2[2]) / 2, strip_y + s(70)), "2. Enforce mode", f_chip, EMERALD)
    center_text(
        draw,
        ((s2[0] + s2[2]) / 2, strip_y + s(94)),
        "enforce_builtin_policy: true",
        f_mono,
        SLATE_400,
    )
    arrow_h(draw, s(510), s(590), strip_y + s(82), CYAN_DIM)

    out = ROOT / "policy-gate-flow.png"
    img.save(out, "PNG", optimize=True)
    return out


def gen_delegation_flow() -> Path:
    W, H = s(W0), s(800)
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)
    grid_bg(draw, W, H)
    cx = W // 2
    title_block(
        draw,
        cx,
        "Delegation & the operator inbox",
        "Agents coordinate over a typed MCP bus — humans hold the kill-switches",
    )

    f_box = font("bold", 14)
    f_sm = font("reg", 12)
    f_tiny = font("reg", 11)
    f_mono = font("mono", 11)

    # Operator
    op = (cx - s(200), s(100), cx + s(200), s(170))
    card(draw, op, VIOLET, radius=s(12), width=s(2))
    center_text(draw, (cx, s(124)), "Operator", font("bold", 16), VIOLET)
    center_text(draw, (cx, s(150)), "inbox  ·  kill-switch  ·  typed answers", f_sm, SLATE_400)

    # Bus
    bus = (cx - s(175), s(300), cx + s(175), s(430))
    card(draw, bus, CYAN, radius=s(16), width=s(3), fill=NAVY_PANEL)
    center_text(draw, (cx, s(330)), "Typed bus (MCP)", font("bold", 17), CYAN_BRIGHT)
    center_text(draw, (cx, s(360)), "31 tools per agent", f_sm, SLATE_400)
    center_text(draw, (cx, s(386)), "ask_agent  ·  ask_consensus", f_mono, CYAN_DIM)
    center_text(draw, (cx, s(408)), "cycle detection · cooldowns", f_tiny, SLATE_500)

    arrow_v(draw, cx, s(295), s(180), VIOLET)
    center_text(draw, (cx + s(100), s(235)), "typed Q/A", f_mono, VIOLET)

    agents = [
        (cx - s(350), s(220), "Agent A", "scoped tools", EMERALD),
        (cx + s(350), s(220), "Agent B", "scoped tools", SKY),
        (cx - s(350), s(520), "Agent C", "scoped tools", AMBER),
        (cx + s(350), s(520), "Agent D", "scoped tools", VIOLET),
    ]
    aw, ah = s(150), s(72)
    for ax, ay, name, sub, accent in agents:
        # connection to bus edge
        if ax < cx and ay < s(360):
            tx, ty = cx - s(175), s(340)
        elif ax > cx and ay < s(360):
            tx, ty = cx + s(175), s(340)
        elif ax < cx:
            tx, ty = cx - s(175), s(400)
        else:
            tx, ty = cx + s(175), s(400)
        draw.line([(ax, ay), (tx, ty)], fill=CYAN_DIM, width=s(2))
        card(
            draw,
            (ax - aw // 2, ay - ah // 2, ax + aw // 2, ay + ah // 2),
            accent,
            radius=s(12),
            width=s(2),
        )
        center_text(draw, (ax, ay - s(12)), name, f_box, WHITE)
        center_text(draw, (ax, ay + s(14)), sub, f_tiny, SLATE_400)

    callouts = [
        (
            s(60),
            s(600),
            s(420),
            s(720),
            "Mediated",
            "Delegation waits when a human\nmust decide — not fire-and-forget.",
            VIOLET,
        ),
        (
            W - s(420),
            s(600),
            W - s(60),
            s(720),
            "Observable",
            "Cycle detection + loop cooldowns;\ndisable an agent and routing skips it.",
            CYAN,
        ),
    ]
    for x0, y0, x1, y1, title, body, accent in callouts:
        card(draw, (x0, y0, x1, y1), accent, radius=s(12), width=s(2), fill=NAVY_PANEL)
        center_text(draw, ((x0 + x1) / 2, y0 + s(28)), title, font("bold", 14), accent)
        for j, line in enumerate(body.split("\n")):
            center_text(draw, ((x0 + x1) / 2, y0 + s(58) + j * s(20)), line, f_sm, SLATE_400)

    center_text(
        draw,
        (cx, s(760)),
        "Agents never spawn peers at will — reach is wired at startup",
        font("bold", 13),
        SLATE_200,
    )

    out = ROOT / "delegation-flow.png"
    img.save(out, "PNG", optimize=True)
    return out


def gen_kernel_components() -> Path:
    W, H = s(W0), s(780)
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)
    grid_bg(draw, W, H)
    cx = W // 2
    title_block(
        draw,
        cx,
        "What's in the kernel",
        "Mechanism only — domain skins plug in at the seams",
    )

    components = [
        ("Policy gates", "Scope + safeguards\ndefault-deny, shadow path", ROSE, "policy/"),
        ("Audit trail", "Redacted I/O log\ndegraded-health flag", AMBER, "memory/actions"),
        ("Operator inbox", "Typed Q/A for\nhuman decisions", VIOLET, "coord/"),
        ("Bus-as-MCP", "31 inter-agent tools\n+ extra_tools slot", CYAN, "bus/"),
        ("Noisy-OR KG", "Corroboration +\nembeddings, TTL", EMERALD, "memory/kg"),
        ("Token budgets", "Epoch-keyed ledger\nwarn → park → interrupt", ORANGE, "daemon/"),
        ("Runner", "Claude + Codex +\npolybrain seam", SKY, "daemon/"),
        ("SM-2 scheduler", "Spaced repetition\ngradebook", INDIGO, "tutor/"),
    ]

    f_title = font("bold", 15)
    f_body = font("reg", 12)
    f_path = font("mono", 11)
    card_w, card_h = s(230), s(150)
    gap = s(20)

    def draw_card(x, y, title, body, accent, path):
        card(draw, (x, y, x + card_w, y + card_h), accent, radius=s(14), width=s(2))
        accent_bar(draw, x + s(14), y + s(14), accent, w=s(40))
        center_text(draw, (x + card_w / 2, y + s(44)), title, f_title, WHITE)
        for j, line in enumerate(body.split("\n")):
            center_text(draw, (x + card_w / 2, y + s(78) + j * s(18)), line, f_body, SLATE_400)
        center_text(draw, (x + card_w / 2, y + card_h - s(22)), path, f_path, CYAN_DIM)

    # 4 + 4 grid
    for row in range(2):
        row_items = components[row * 4 : (row + 1) * 4]
        total = 4 * card_w + 3 * gap
        x1 = (W - total) // 2
        y1 = s(110) + row * (card_h + s(24))
        for i, c in enumerate(row_items):
            draw_card(x1 + i * (card_w + gap), y1, *c)

    fy = s(110) + 2 * (card_h + s(24)) + s(10)
    draw.rounded_rectangle(
        (s(70), fy, W - s(70), fy + s(110)),
        radius=s(14),
        fill=NAVY_PANEL,
        outline=CYAN_DEEP,
        width=s(2),
    )
    center_text(draw, (cx, fy + s(28)), "Two kinds of seams", font("bold", 15), CYAN)
    center_text(
        draw,
        (cx, fy + s(58)),
        "Protocol contracts:  DaemonServices · ToolBuilder · AliasProtocol · AgentBackend · AgentProvider",
        f_body,
        SLATE_400,
    )
    center_text(
        draw,
        (cx, fy + s(82)),
        "Runtime registration:  set_* / register_* at startup, read at call time — never import time",
        f_body,
        SLATE_400,
    )

    out = ROOT / "kernel-components.png"
    img.save(out, "PNG", optimize=True)
    return out


def gen_kernel_position() -> Path:
    W, H = s(W0), s(820)
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)
    grid_bg(draw, W, H)
    cx = W // 2

    title_block(
        draw,
        cx,
        "Where the kernel sits",
        "Control surfaces live below the model — not in the prompt",
    )

    f_box = font("bold", 17)
    f_box_sm = font("bold", 14)
    f_label = font("reg", 13)
    f_mono = font("mono", 12)
    f_chip = font("med", 12)

    # LLM
    llm = (cx - s(190), s(105), cx + s(190), s(185))
    card(draw, llm, CYAN_DIM, radius=s(14), width=s(2))
    center_text(draw, (cx, s(132)), "LLM / agent loop", f_box, CYAN_BRIGHT)
    center_text(draw, (cx, s(162)), "Claude SDK  ·  OpenAI Codex  ·  polybrain", f_label, SLATE_400)

    arrow_v(draw, cx, s(190), s(230), CYAN)
    center_text(draw, (cx + s(100), s(212)), "tool calls", f_mono, CYAN_DIM)

    # Kernel shell
    k = (s(80), s(238), W - s(80), s(560))
    card(draw, k, CYAN, radius=s(18), width=s(3), fill=NAVY_PANEL)
    draw.rounded_rectangle(
        (k[0] + s(4), k[1] + s(4), k[2] - s(4), k[3] - s(4)),
        radius=s(16),
        outline=CYAN_DEEP,
        width=1,
    )
    center_text(draw, (cx, k[1] + s(28)), "salient-core", f_box, CYAN)
    center_text(
        draw,
        (cx, k[1] + s(54)),
        "agent-control kernel  —  below the model",
        f_chip,
        CYAN_DIM,
    )

    cards = [
        ("Policy gates", "scope + safeguards\ndefault-deny", EMERALD),
        ("Typed bus (MCP)", "delegation · context\nKG · discovery", CYAN),
        ("Audit trail", "every decision\nredacted + durable", AMBER),
    ]
    card_w, card_h = s(250), s(120)
    gap = s(32)
    total_w = 3 * card_w + 2 * gap
    start_x = cx - total_w // 2
    card_y = k[1] + s(82)
    for i, (title, body, accent) in enumerate(cards):
        x0 = start_x + i * (card_w + gap)
        card(draw, (x0, card_y, x0 + card_w, card_y + card_h), accent, radius=s(12), width=s(2))
        accent_bar(draw, x0 + s(12), card_y + s(12), accent)
        center_text(draw, (x0 + card_w / 2, card_y + s(42)), title, f_box_sm, WHITE)
        for j, line in enumerate(body.split("\n")):
            center_text(
                draw,
                (x0 + card_w / 2, card_y + s(72) + j * s(20)),
                line,
                f_label,
                SLATE_400,
            )

    inbox_y = k[3] - s(48)
    card(
        draw,
        (cx - s(180), inbox_y - s(20), cx + s(180), inbox_y + s(20)),
        VIOLET,
        radius=s(10),
        width=s(2),
        fill=(20, 30, 55),
    )
    center_text(draw, (cx, inbox_y), "operator inbox  ·  typed Q/A", f_chip, VIOLET)

    targets = [
        (cx - s(300), "Tools", "scoped + gated", EMERALD),
        (cx, "Other agents", "bus-mediated", CYAN),
        (cx + s(300), "Operator", "human-in-the-loop", VIOLET),
    ]
    for tx, title, sub, accent in targets:
        arrow_v(draw, tx, k[3] + s(4), k[3] + s(48), accent)
        box = (tx - s(110), k[3] + s(55), tx + s(110), k[3] + s(130))
        card(draw, box, accent, radius=s(12), width=s(2))
        center_text(draw, (tx, k[3] + s(80)), title, f_box_sm, WHITE)
        center_text(draw, (tx, k[3] + s(108)), sub, f_label, SLATE_400)

    center_text(
        draw,
        (cx, H - s(32)),
        "A denied call never runs.  Delegation waits for the operator when required.",
        font("reg", 14),
        SLATE_400,
    )

    out = ROOT / "kernel-position.png"
    img.save(out, "PNG", optimize=True)
    return out


def main() -> None:
    outs = [
        make_social_preview(),
        make_hero_bus(),
        make_without_kernel(),
        gen_control_surfaces(),
        gen_policy_gate_flow(),
        gen_delegation_flow(),
        gen_kernel_components(),
        gen_kernel_position(),
    ]
    for p in outs:
        im = Image.open(p)
        print(f"wrote {p.name:32s}  {im.size[0]}x{im.size[1]:4d}  {p.stat().st_size // 1024:5d} KB")


if __name__ == "__main__":
    main()
