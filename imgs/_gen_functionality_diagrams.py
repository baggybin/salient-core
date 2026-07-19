"""Generate functionality diagrams for the salient-core README.

Code-rendered (exact text). Palette matches hero-bus / _overlay.py.
Outputs:
  - control-surfaces.png   — five control pillars (goal section)
  - policy-gate-flow.png   — default-deny authorization path
  - delegation-flow.png    — bus-mediated multi-agent + operator inbox
  - kernel-components.png  — what's in the kernel
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent

PROP_BOLD = "/usr/share/fonts/noto/NotoSans-Bold.ttf"
PROP_REG = "/usr/share/fonts/noto/NotoSans-Regular.ttf"
PROP_MED = "/usr/share/fonts/noto/NotoSans-Medium.ttf"
MONO = "/usr/share/fonts/noto/NotoSansMono-Regular.ttf"

NAVY_BG = (2, 16, 31)
NAVY_PANEL = (8, 28, 48)
NAVY_CARD = (12, 36, 58)
NAVY_SOFT = (18, 42, 68)
CYAN = (68, 245, 255)
CYAN_BRIGHT = (180, 250, 255)
CYAN_DIM = (40, 120, 140)
CYAN_DEEP = (24, 60, 88)
SLATE_200 = (226, 232, 240)
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


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
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


def rr(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    fill=None,
    outline=None,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def grid_bg(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    for x in range(0, w, 40):
        draw.line([(x, 0), (x, h)], fill=(6, 24, 40), width=1)
    for y in range(0, h, 40):
        draw.line([(0, y), (w, y)], fill=(6, 24, 40), width=1)


def title_block(
    draw: ImageDraw.ImageDraw,
    cx: int,
    title: str,
    subtitle: str,
    y: int = 36,
) -> int:
    f_title = font(PROP_BOLD, 26)
    f_sub = font(PROP_REG, 14)
    center_text(draw, (cx, y), title, f_title, CYAN_BRIGHT)
    center_text(draw, (cx, y + 30), subtitle, f_sub, SLATE_400)
    return y + 55


def arrow_h(
    draw: ImageDraw.ImageDraw,
    x0: int,
    x1: int,
    y: int,
    color: tuple[int, int, int],
    width: int = 3,
) -> None:
    if x1 > x0:
        draw.line([(x0, y), (x1 - 8, y)], fill=color, width=width)
        draw.polygon([(x1, y), (x1 - 12, y - 7), (x1 - 12, y + 7)], fill=color)
    else:
        draw.line([(x0, y), (x1 + 8, y)], fill=color, width=width)
        draw.polygon([(x1, y), (x1 + 12, y - 7), (x1 + 12, y + 7)], fill=color)


def arrow_v(
    draw: ImageDraw.ImageDraw,
    x: int,
    y0: int,
    y1: int,
    color: tuple[int, int, int],
    width: int = 3,
) -> None:
    if y1 > y0:
        draw.line([(x, y0), (x, y1 - 8)], fill=color, width=width)
        draw.polygon([(x, y1), (x - 7, y1 - 12), (x + 7, y1 - 12)], fill=color)
    else:
        draw.line([(x, y0), (x, y1 + 8)], fill=color, width=width)
        draw.polygon([(x, y1), (x - 7, y1 + 12), (x + 7, y1 + 12)], fill=color)


# ---------------------------------------------------------------------------
# 1. Control surfaces (five pillars)
# ---------------------------------------------------------------------------
def gen_control_surfaces() -> Path:
    W, H = 1100, 620
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)
    grid_bg(draw, W, H)
    cx = W // 2
    title_block(
        draw,
        cx,
        "Five control surfaces",
        "Levers live under the model — where agents cannot reason past them",
    )

    pillars = [
        ("Capability", "One tool surface\nper agent\nOS privilege optional", EMERALD),
        ("Action", "Scope + safeguards\non every call\ndefault-deny", ROSE),
        ("Delegation", "Bus-mediated\noperator inbox\ncycle detection", CYAN),
        ("Accountability", "Redacted audit\ntrail of every\ngate decision", AMBER),
        ("Staged trust", "Shadow mode first\nthen flip enforce\nwhen ready", VIOLET),
    ]

    card_w, card_h = 180, 220
    gap = 24
    total = 5 * card_w + 4 * gap
    x0 = (W - total) // 2
    y0 = 120
    f_title = font(PROP_BOLD, 16)
    f_body = font(PROP_REG, 13)
    f_num = font(PROP_BOLD, 20)

    for i, (title, body, accent) in enumerate(pillars):
        x = x0 + i * (card_w + gap)
        rr(draw, (x, y0, x + card_w, y0 + card_h), 14, fill=NAVY_CARD, outline=accent, width=2)
        # number chip
        rr(draw, (x + 14, y0 + 14, x + 48, y0 + 48), 8, fill=NAVY_SOFT, outline=accent, width=2)
        center_text(draw, (x + 31, y0 + 31), str(i + 1), f_num, accent)
        center_text(draw, (x + card_w / 2, y0 + 72), title, f_title, WHITE)
        draw.line(
            [(x + 28, y0 + 92), (x + card_w - 28, y0 + 92)],
            fill=accent,
            width=2,
        )
        for j, line in enumerate(body.split("\n")):
            center_text(
                draw,
                (x + card_w / 2, y0 + 120 + j * 22),
                line,
                f_body,
                SLATE_400,
            )

    # footer bar
    rr(draw, (80, 380, W - 80, 560), 16, fill=NAVY_PANEL, outline=CYAN_DEEP, width=2)
    f_h = font(PROP_BOLD, 16)
    f_p = font(PROP_REG, 14)
    center_text(draw, (cx, 415), "Why below the model?", f_h, CYAN)
    lines = [
        "Prompts can be ignored. Confused or manipulated agents can talk themselves into unsafe actions.",
        "Gates, the bus, and the audit trail sit under the loop — a denied call never runs, and every",
        "decision leaves a receipt the operator can replay.",
    ]
    for j, line in enumerate(lines):
        center_text(draw, (cx, 455 + j * 24), line, f_p, SLATE_400)

    out = ROOT / "control-surfaces.png"
    img.save(out, "PNG", optimize=True)
    return out


# ---------------------------------------------------------------------------
# 2. Policy gate flow (default-deny)
# ---------------------------------------------------------------------------
def gen_policy_gate_flow() -> Path:
    W, H = 1100, 700
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

    f_box = font(PROP_BOLD, 15)
    f_sm = font(PROP_REG, 12)
    f_chip = font(PROP_MED, 12)
    f_mono = font(MONO, 11)

    # Step 1: tool call sources
    y = 110
    sources = [
        ("SDK built-ins", SKY),
        ("Bus tools", CYAN),
        ("External MCP", VIOLET),
        ("Text commands", ORANGE),
    ]
    sw, sh = 180, 56
    gap = 20
    total = 4 * sw + 3 * gap
    sx = (W - total) // 2
    for i, (label, accent) in enumerate(sources):
        x = sx + i * (sw + gap)
        rr(draw, (x, y, x + sw, y + sh), 10, fill=NAVY_CARD, outline=accent, width=2)
        center_text(draw, (x + sw / 2, y + sh / 2), label, f_box, WHITE)

    # funnel arrows into gate
    mid_y = y + sh + 10
    gate_y = mid_y + 50
    for i in range(4):
        x = sx + i * (sw + gap) + sw // 2
        draw.line([(x, y + sh + 2), (cx, gate_y - 4)], fill=CYAN_DIM, width=2)

    # Gate box
    gw, gh = 420, 100
    gx0, gy0 = cx - gw // 2, gate_y
    rr(draw, (gx0, gy0, gx0 + gw, gy0 + gh), 14, fill=NAVY_PANEL, outline=CYAN, width=3)
    center_text(draw, (cx, gy0 + 32), "Scope + safeguard gates", f_box, CYAN_BRIGHT)
    center_text(draw, (cx, gy0 + 58), "transport-neutral  |  capability != authorization", f_sm, SLATE_400)
    center_text(draw, (cx, gy0 + 80), "unclassified tools fail closed", f_mono, ROSE)

    # Split arrows
    split_y = gy0 + gh + 20
    arrow_v(draw, cx - 180, gy0 + gh + 2, split_y + 40, EMERALD, 3)
    arrow_v(draw, cx + 180, gy0 + gh + 2, split_y + 40, ROSE, 3)

    # Allow / Deny cards
    allow_box = (cx - 320, split_y + 45, cx - 40, split_y + 145)
    deny_box = (cx + 40, split_y + 45, cx + 320, split_y + 145)
    rr(draw, allow_box, 12, fill=NAVY_CARD, outline=EMERALD, width=2)
    rr(draw, deny_box, 12, fill=NAVY_CARD, outline=ROSE, width=2)
    center_text(draw, ((allow_box[0] + allow_box[2]) / 2, split_y + 75), "ALLOW", font(PROP_BOLD, 18), EMERALD)
    center_text(
        draw,
        ((allow_box[0] + allow_box[2]) / 2, split_y + 110),
        "tool executes  |  audited",
        f_sm,
        SLATE_400,
    )
    center_text(draw, ((deny_box[0] + deny_box[2]) / 2, split_y + 75), "DENY", font(PROP_BOLD, 18), ROSE)
    center_text(
        draw,
        ((deny_box[0] + deny_box[2]) / 2, split_y + 110),
        "never runs  |  denial recorded",
        f_sm,
        SLATE_400,
    )

    # Shadow then enforce strip
    strip_y = split_y + 175
    rr(draw, (100, strip_y, W - 100, strip_y + 130), 14, fill=NAVY_PANEL, outline=VIOLET, width=2)
    center_text(draw, (cx, strip_y + 28), "Staged trust: shadow then enforce", font(PROP_BOLD, 15), VIOLET)

    # two stages side by side
    s1 = (140, strip_y + 50, 500, strip_y + 110)
    s2 = (600, strip_y + 50, 960, strip_y + 110)
    rr(draw, s1, 10, fill=NAVY_CARD, outline=AMBER, width=2)
    rr(draw, s2, 10, fill=NAVY_CARD, outline=EMERALD, width=2)
    center_text(draw, ((s1[0] + s1[2]) / 2, strip_y + 68), "1. Shadow mode", f_chip, AMBER)
    center_text(
        draw,
        ((s1[0] + s1[2]) / 2, strip_y + 90),
        "record would-deny, still permit",
        f_sm,
        SLATE_400,
    )
    center_text(draw, ((s2[0] + s2[2]) / 2, strip_y + 68), "2. Enforce mode", f_chip, EMERALD)
    center_text(
        draw,
        ((s2[0] + s2[2]) / 2, strip_y + 90),
        "enforce_builtin_policy: true",
        f_mono,
        SLATE_400,
    )
    arrow_h(draw, 510, 590, strip_y + 80, CYAN_DIM, 2)

    out = ROOT / "policy-gate-flow.png"
    img.save(out, "PNG", optimize=True)
    return out


# ---------------------------------------------------------------------------
# 3. Delegation + operator inbox
# ---------------------------------------------------------------------------
def gen_delegation_flow() -> Path:
    W, H = 1100, 740
    img = Image.new("RGB", (W, H), NAVY_BG)
    draw = ImageDraw.Draw(img)
    grid_bg(draw, W, H)
    cx = W // 2
    title_block(
        draw,
        cx,
        "Delegation & the operator inbox",
        "Agents coordinate over a typed bus — humans hold the kill-switches",
    )

    f_box = font(PROP_BOLD, 14)
    f_sm = font(PROP_REG, 12)
    f_tiny = font(PROP_REG, 11)
    f_mono = font(MONO, 11)

    # Operator panel at top
    op_box = (cx - 200, 100, cx + 200, 165)
    rr(draw, op_box, 12, fill=NAVY_CARD, outline=VIOLET, width=2)
    center_text(draw, (cx, 122), "Operator", font(PROP_BOLD, 16), VIOLET)
    center_text(draw, (cx, 148), "inbox  |  kill-switch  |  answers", f_sm, SLATE_400)

    # Central bus
    bus_box = (cx - 160, 280, cx + 160, 400)
    rr(draw, bus_box, 16, fill=NAVY_PANEL, outline=CYAN, width=3)
    center_text(draw, (cx, 320), "Typed bus (MCP)", font(PROP_BOLD, 17), CYAN_BRIGHT)
    center_text(draw, (cx, 348), "~40 tools per agent", f_sm, SLATE_400)
    center_text(draw, (cx, 370), "ask_agent  |  ask_consensus", f_mono, CYAN_DIM)
    center_text(draw, (cx, 388), "cycle detection, cooldowns", f_tiny, SLATE_500)

    # arrows from bus up to operator
    arrow_v(draw, cx, 275, 175, VIOLET, 3)
    center_text(draw, (cx + 95, 220), "typed Q/A", f_mono, VIOLET)

    # Agents around the bus
    agents = [
        (cx - 340, 200, "Agent A", "scoped tools", EMERALD),
        (cx + 340, 200, "Agent B", "scoped tools", SKY),
        (cx - 340, 480, "Agent C", "scoped tools", AMBER),
        (cx + 340, 480, "Agent D", "scoped tools", VIOLET),
    ]
    aw, ah = 150, 70
    for ax, ay, name, sub, accent in agents:
        if ax < cx and ay < 340:
            tx, ty = cx - 160, 300
        elif ax > cx and ay < 340:
            tx, ty = cx + 160, 300
        elif ax < cx:
            tx, ty = cx - 160, 380
        else:
            tx, ty = cx + 160, 380
        draw.line([(ax, ay), (tx, ty)], fill=CYAN_DIM, width=2)
        rr(
            draw,
            (ax - aw // 2, ay - ah // 2, ax + aw // 2, ay + ah // 2),
            12,
            fill=NAVY_CARD,
            outline=accent,
            width=2,
        )
        center_text(draw, (ax, ay - 12), name, f_box, WHITE)
        center_text(draw, (ax, ay + 14), sub, f_tiny, SLATE_400)

    # Side callouts — clear of agents
    callouts = [
        (70, 560, 400, 670, "Mediated", "Delegation waits when a human\nmust decide — not fire-and-forget.", VIOLET),
        (W - 400, 560, W - 70, 670, "Observable", "Cycle detection, loop cooldowns;\ndisable agent and routing skips it.", CYAN),
    ]
    for x0, y0, x1, y1, title, body, accent in callouts:
        rr(draw, (x0, y0, x1, y1), 12, fill=NAVY_PANEL, outline=accent, width=2)
        center_text(draw, ((x0 + x1) / 2, y0 + 28), title, font(PROP_BOLD, 14), accent)
        for j, line in enumerate(body.split("\n")):
            center_text(draw, ((x0 + x1) / 2, y0 + 55 + j * 18), line, f_sm, SLATE_400)

    # center bottom note — between callouts, not overlapping
    center_text(
        draw,
        (cx, 700),
        "Agents never spawn peers at will — reach is wired at startup",
        font(PROP_BOLD, 13),
        SLATE_200,
    )

    out = ROOT / "delegation-flow.png"
    img.save(out, "PNG", optimize=True)
    return out


# ---------------------------------------------------------------------------
# 4. Kernel components map
# ---------------------------------------------------------------------------
def gen_kernel_components() -> Path:
    W, H = 1100, 720
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
        ("Bus-as-MCP", "~40 inter-agent tools\nextra_tools slot", CYAN, "bus/"),
        ("Noisy-OR KG", "Corroboration +\nembeddings, TTL", EMERALD, "memory/kg"),
        ("SM-2 scheduler", "Spaced repetition\ngradebook", ORANGE, "tutor/"),
        ("Runner", "Claude + Codex\nAgentBackend seam", SKY, "daemon/"),
    ]

    # Layout: 4 on top row, 3 on bottom (centered)
    f_title = font(PROP_BOLD, 15)
    f_body = font(PROP_REG, 12)
    f_path = font(MONO, 11)

    card_w, card_h = 230, 150
    gap = 22

    def draw_card(x: int, y: int, title: str, body: str, accent, path: str) -> None:
        rr(draw, (x, y, x + card_w, y + card_h), 14, fill=NAVY_CARD, outline=accent, width=2)
        draw.rounded_rectangle((x + 14, y + 12, x + 50, y + 16), radius=2, fill=accent)
        center_text(draw, (x + card_w / 2, y + 40), title, f_title, WHITE)
        for j, line in enumerate(body.split("\n")):
            center_text(draw, (x + card_w / 2, y + 72 + j * 18), line, f_body, SLATE_400)
        center_text(draw, (x + card_w / 2, y + card_h - 22), path, f_path, CYAN_DIM)

    # row 1: 4 cards
    row1 = components[:4]
    total1 = 4 * card_w + 3 * gap
    x1 = (W - total1) // 2
    y1 = 110
    for i, c in enumerate(row1):
        draw_card(x1 + i * (card_w + gap), y1, *c)

    # row 2: 3 cards centered
    row2 = components[4:]
    total2 = 3 * card_w + 2 * gap
    x2 = (W - total2) // 2
    y2 = y1 + card_h + 28
    for i, c in enumerate(row2):
        draw_card(x2 + i * (card_w + gap), y2, *c)

    # seams footer
    fy = y2 + card_h + 30
    rr(draw, (80, fy, W - 80, fy + 100), 14, fill=NAVY_PANEL, outline=CYAN_DEEP, width=2)
    center_text(draw, (cx, fy + 28), "Two kinds of seams", font(PROP_BOLD, 15), CYAN)
    center_text(
        draw,
        (cx, fy + 55),
        "Protocol contracts:  DaemonServices, ToolBuilder, AliasProtocol, AgentBackend",
        f_body,
        SLATE_400,
    )
    center_text(
        draw,
        (cx, fy + 78),
        "Runtime registration:  set_* / register_* called at startup, read at call time",
        f_body,
        SLATE_400,
    )

    out = ROOT / "kernel-components.png"
    img.save(out, "PNG", optimize=True)
    return out


def main() -> None:
    outs = [
        gen_control_surfaces(),
        gen_policy_gate_flow(),
        gen_delegation_flow(),
        gen_kernel_components(),
    ]
    for p in outs:
        print(f"wrote {p.name} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
