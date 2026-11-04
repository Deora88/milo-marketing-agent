"""Media for Instagram posts, made to Meta's specs (Instagram Graph API, IG User Media reference).

- Feed photos / carousel items: JPEG, sRGB, max 8 MB, aspect 4:5 to 1.91:1, width 320-1440 px.
  Milo saves them as 1080 px JPEGs; anything taller than 4:5 is cropped to 4:5, wider than 1.91:1 to 1.91:1.
- Stories and Reels: 9:16 (1080x1920). The picture is centred over a blurred, darker copy of itself, so a square
  AI image fills the screen without cropping the subject.
- Reels: an MP4 built from the post's images with the ffmpeg that ships with imageio-ffmpeg (no install needed):
  H.264, yuv420p (4:2:0), 30 fps, closed GOP, moov atom at the front (+faststart), silent AAC 48 kHz track,
  3 seconds per image (Reels must be 3 s to 15 min).
"""
import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

POST_DIR = Path("data/posts")
FONT_DIR = Path(__file__).parent / "fonts"  # Plus Jakarta Sans (SIL Open Font License, see fonts/OFL.txt)
BRAND = (242, 84, 45)  # #F2542D, the Milo brand orange
OVERLAY_LIMITS = {"headline": 60, "details": 90, "cta": 30}
MAX_UPLOAD = 8 * 1024 * 1024
FEED_W = 1080
STORY = (1080, 1920)
SECONDS_PER_IMAGE = 3
MIN_RATIO, MAX_RATIO = 4 / 5, 1.91  # width / height allowed in the feed


class MediaError(ValueError):
    """The image or video can't be used; the message is shown to the user."""


def _open(data: bytes) -> Image.Image:
    if len(data) > MAX_UPLOAD:
        raise MediaError("Image is larger than 8 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any unreadable file
        raise MediaError("That file is not an image Milo can read (use JPG or PNG).") from exc
    img = ImageOps.exif_transpose(img)  # phone photos: apply the rotation flag
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, "white")  # PNG transparency -> white, JPEG has no alpha
        background.paste(img, mask=img.convert("RGBA").split()[-1])
        img = background
    return img.convert("RGB")


def _jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def normalised_jpeg(data: bytes, max_side: int = 1440) -> bytes:
    """Any image as an upright sRGB JPEG, at most 1440 px on the long side (kept uncropped for Stories/Reels)."""
    img = _open(data)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    return _jpeg(img)


def feed_jpeg(data: bytes) -> bytes:
    """A feed-ready JPEG: 1080 px wide, aspect clamped to Instagram's 4:5 .. 1.91:1."""
    img = _open(data)
    w, h = img.size
    ratio = w / h
    if ratio < MIN_RATIO:  # too tall: crop top/bottom
        new_h = int(w / MIN_RATIO)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif ratio > MAX_RATIO:  # too wide: crop the sides
        new_w = int(h * MAX_RATIO)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    w, h = img.size
    img = img.resize((FEED_W, round(h * FEED_W / w)), Image.LANCZOS)
    return _jpeg(img)


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    name = "PlusJakartaSans_800ExtraBold.ttf" if weight == "bold" else "PlusJakartaSans_600SemiBold.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


def clean_text(text: str, font: ImageFont.FreeTypeFont | None = None) -> str:
    """One line of overlay text: no line breaks, and only characters the font can draw (emoji become nothing
    instead of empty boxes)."""
    font = font or _font("bold", 40)

    def glyph(ch: str) -> bytes:
        im = Image.new("L", (100, 100))
        ImageDraw.Draw(im).text((10, 10), ch, font=font, fill=255)
        return im.tobytes()

    missing = glyph("\ue000")  # what the font draws for a character it doesn't have
    out = []
    for ch in " ".join((text or "").split()):
        if ch == " " or glyph(ch) != missing:
            out.append(ch)
    return " ".join("".join(out).split())


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        test = f"{line} {word}".strip()
        if draw.textlength(test, font=font) <= width or not line:
            line = test
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _fit(draw, text: str, weight: str, start: int, width: int, max_lines: int):
    """The biggest font size (down to 60%) at which the text fits in max_lines lines."""
    size = start
    while True:
        font = _font(weight, size)
        lines = _wrap(draw, text, font, width)
        if len(lines) <= max_lines or size <= start * 0.6:
            return font, lines[:max_lines]
        size -= 4


def _draw_lines(draw, lines, font, y: int, fill, line_gap: float = 1.18) -> int:
    asc, desc = font.getmetrics()
    for ln in lines:
        w = draw.textlength(ln, font=font)
        draw.text(((STORY[0] - w) / 2, y), ln, font=font, fill=fill)
        y += int((asc + desc) * line_gap)
    return y


def vertical_frame(data: bytes, overlay: dict | None = None) -> bytes:
    """A 1080x1920 (9:16) JPEG for Stories and Reels: the picture over a blurred copy of itself.

    overlay (optional) = {"headline", "details", "cta"}: drawn in the free areas above and below the picture
    (headline + details at the top, the call to action as an orange button at the bottom), so the picture
    itself is never covered. Instagram's API can't add text or stickers to a Story, so the text is part of
    the image."""
    img = _open(data)
    overlay = {k: clean_text(str((overlay or {}).get(k) or ""))[:n] for k, n in OVERLAY_LIMITS.items()}
    scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    width = STORY[0] - 2 * 90
    blocks = []  # (font, lines, fill, line_gap, space_before), measured first so the picture never overlaps text
    if overlay["headline"]:
        font, lines = _fit(scratch, overlay["headline"], "bold", 84, width, 2)
        blocks.append((font, lines, "white", 1.08, 0))
    if overlay["details"]:
        font, lines = _fit(scratch, overlay["details"], "semi", 44, width, 2)
        blocks.append((font, lines, (235, 235, 235), 1.18, 18 if blocks else 0))
    text_h = sum(sb + int(sum(f.getmetrics()) * g) * len(ls) for f, ls, _, g, sb in blocks)
    top = 150 + text_h + 70 if blocks else 120
    bottom = 330 if overlay["cta"] else 120
    has_text = bool(blocks or overlay["cta"])
    bg = ImageOps.fit(img, STORY, Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new("RGB", STORY, "black"), 0.45 if has_text else 0.35)
    fg = img.copy()
    area_h = STORY[1] - top - bottom
    fg.thumbnail((STORY[0], area_h), Image.LANCZOS)
    bg.paste(fg, ((STORY[0] - fg.width) // 2, top + (area_h - fg.height) // 2))
    draw = ImageDraw.Draw(bg)
    y = 150
    for font, lines, fill, gap, space_before in blocks:
        y = _draw_lines(draw, lines, font, y + space_before, fill, gap)
    if overlay["cta"]:
        font, lines = _fit(draw, overlay["cta"], "bold", 50, width - 120, 1)
        text = lines[0]
        tw = draw.textlength(text, font=font)
        asc, desc = font.getmetrics()
        bw, bh = int(tw + 120), asc + desc + 56
        x0, y0 = (STORY[0] - bw) // 2, STORY[1] - bottom + (bottom - bh) // 2 - 20
        draw.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=bh // 2, fill=BRAND)
        draw.text(((STORY[0] - tw) / 2, y0 + 28), text, font=font, fill="white")
    return _jpeg(bg)


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def build_reel(frames: list[bytes], out_path: Path, seconds_each: int = SECONDS_PER_IMAGE) -> Path:
    """MP4 slideshow (9:16) from 9:16 JPEG frames, to Instagram's Reels spec."""
    if not frames:
        raise MediaError("A Reel needs at least one image.")
    duration = max(3, seconds_each * len(frames))  # Reels: at least 3 seconds
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        listing = []
        for i, frame in enumerate(frames):
            p = Path(tmp) / f"f{i}.jpg"
            p.write_bytes(frame)
            listing += [f"file '{p.as_posix()}'", f"duration {duration / len(frames):.3f}"]
        listing.append(f"file '{(Path(tmp) / f'f{len(frames) - 1}.jpg').as_posix()}'")  # concat: repeat last
        lst = Path(tmp) / "frames.txt"
        lst.write_text("\n".join(listing))
        cmd = [ffmpeg_exe(), "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(lst),
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-vf", "scale=1080:1920,format=yuv420p,fps=30",
               "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast", "-g", "60", "-bf", "0",
               "-flags", "+cgop", "-b:v", "5M", "-maxrate", "8M", "-bufsize", "10M",
               "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-t", f"{duration}", "-shortest", "-movflags", "+faststart", str(out_path)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        except subprocess.CalledProcessError as exc:
            raise MediaError(f"Couldn't build the Reel video: {exc.stderr.decode(errors='ignore')[-300:]}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(f"Couldn't build the Reel video ({type(exc).__name__}).") from exc
    return out_path
