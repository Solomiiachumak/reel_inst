#!/usr/bin/env python3
"""
PGR reel renderer — власна заміна Creatomate.

Збирає вертикальний рілс 1080x1920:
  фонове відео (cover) + легка сепія + затемнення
  + плівкове зерно (blend overlay)
  + текстовий блок по центру (Roboto Medium), що проявляється й зникає.

Приклад:
  python3 render_reel.py --video bg.mp4 --text text.txt --out reel.mp4

Потрібно: ffmpeg, Python 3, Pillow (pip install pillow).
"""
import argparse
import os
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FPS = 30

# --- Стиль (аналог шаблону PGR Reel A) ---
STYLE = {
    "font_size_max": 37,       # px  (~3.4 vmin)
    "font_size_min": 22,       # px  (~2 vmin) — нижче не зменшуємо
    "line_height": 1.45,       # міжрядковий інтервал
    "box_width": 0.55,         # макс. ширина текстового блоку (частка кадру)
    "safe_top": 250,           # px — зверху інтерфейс Instagram
    "safe_bottom": 420,        # px — знизу підпис і кнопки
    "center_y": 0.47,          # центр тексту по висоті
    "text_color": (255, 255, 255, 255),
    "shadow_alpha": 90,        # 0 = без тіні
    "sepia": 0.20,             # сила сепії 0..1
    "darken": 0.25,            # затемнення 0..1
    "grain_opacity": 0.60,     # сила зерна 0..1
    "fade_in": 0,              # 0 = текст видно з першого кадру
    "fade_out": 0,             # 0 = текст видно до останнього кадру
}


def reading_duration(text: str) -> float:
    """Скільки секунд тримати текст: ~2.5 слова/с + 3 с запасу, у межах 8–30 с."""
    words = len(text.split())
    return round(min(30.0, max(8.0, words / 2.5 + 3.0)), 1)


def render_text_png(text: str, font_path: str, out_png: str) -> None:
    lines = text.strip("\n").split("\n")
    max_w = int(W * STYLE["box_width"])
    max_h = H - STYLE["safe_top"] - STYLE["safe_bottom"]

    # Підбираємо найбільший шрифт, з яким текст влазить у безпечну зону
    size = STYLE["font_size_max"]
    while True:
        font = ImageFont.truetype(font_path, size)
        lh = int(size * STYLE["line_height"])
        widths = [font.getlength(l) for l in lines]
        block_h = lh * len(lines)
        if (max(widths or [0]) <= max_w and block_h <= max_h) or size <= STYLE["font_size_min"]:
            break
        size -= 1

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    top = int(H * STYLE["center_y"] - block_h / 2)
    top = max(STYLE["safe_top"], min(top, H - STYLE["safe_bottom"] - block_h))

    for i, line in enumerate(lines):
        if not line.strip():
            continue
        w = font.getlength(line)
        x = (W - w) / 2
        y = top + i * lh
        if STYLE["shadow_alpha"]:
            draw.text((x, y + 2), line, font=font, fill=(0, 0, 0, STYLE["shadow_alpha"]))
        draw.text((x, y), line, font=font, fill=STYLE["text_color"])

    if STYLE["shadow_alpha"]:
        # легке розмиття лише тіні: малюємо тінь окремим шаром
        from PIL import ImageFilter
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        for i, line in enumerate(lines):
            if line.strip():
                w = font.getlength(line)
                sd.text(((W - w) / 2, top + i * lh + 2), line, font=font,
                        fill=(0, 0, 0, STYLE["shadow_alpha"]))
        shadow = shadow.filter(ImageFilter.GaussianBlur(6))
        clean = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        cd = ImageDraw.Draw(clean)
        for i, line in enumerate(lines):
            if line.strip():
                w = font.getlength(line)
                cd.text(((W - w) / 2, top + i * lh), line, font=font, fill=STYLE["text_color"])
        img = Image.alpha_composite(shadow, clean)

    img.save(out_png)


def sepia_darken_matrix(s: float, d: float) -> str:
    """colorchannelmixer: змішування з сепією (s) і затемнення (d)."""
    sep = [[.393, .769, .189], [.349, .686, .168], [.272, .534, .131]]
    k = 1.0 - d
    m = []
    for r in range(3):
        for c in range(3):
            ident = 1.0 if r == c else 0.0
            m.append(round(((1 - s) * ident + s * sep[r][c]) * k, 4))
    names = ["rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb"]
    return ":".join(f"{n}={v}" for n, v in zip(names, m))


def render(video: str, text: str, out: str, grain: str, font: str, duration: float | None) -> float:
    dur = duration or reading_duration(text)
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "text.png")
        render_text_png(text, font, png)

        fades = []
        if STYLE["fade_in"]:
            fades.append(f"fade=t=in:st=0:d={STYLE['fade_in']}:alpha=1")
        if STYLE["fade_out"]:
            fades.append(f"fade=t=out:st={max(0.0, dur - STYLE['fade_out'])}:d={STYLE['fade_out']}:alpha=1")
        txt_chain = ",".join(["format=rgba"] + fades)
        filt = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
            f"colorchannelmixer={sepia_darken_matrix(STYLE['sepia'], STYLE['darken'])},format=gbrp[bg];"
            f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},format=gbrp[gr];"
            f"[bg][gr]blend=all_mode=overlay:all_opacity={STYLE['grain_opacity']},format=yuv420p[base];"
            f"[2:v]{txt_chain}[txt];"
            f"[base][txt]overlay=0:0:format=auto,format=yuv420p[v]"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-stream_loop", "-1", "-i", video,
            "-stream_loop", "-1", "-i", grain,
            "-loop", "1", "-framerate", str(FPS), "-i", png,
            "-filter_complex", filt, "-map", "[v]",
            "-t", str(dur), "-an",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out,
        ]
        subprocess.run(cmd, check=True)
    return dur


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="PGR reel renderer")
    p.add_argument("--video", required=True, help="фонове відео (будь-яке, буде обрізане до 9:16)")
    p.add_argument("--text", required=True, help="txt-файл з текстом; переноси рядків зберігаються")
    p.add_argument("--out", required=True, help="куди зберегти MP4")
    p.add_argument("--grain", default=os.path.join(here, "pgr_film_grain_overlay.mp4"))
    p.add_argument("--font", default=os.path.join(here, "Roboto-Medium.woff"))
    p.add_argument("--duration", type=float, default=None, help="тривалість, с (за замовч. — під довжину тексту)")
    a = p.parse_args()
    with open(a.text, encoding="utf-8") as f:
        txt = f.read()
    d = render(a.video, txt, a.out, a.grain, a.font, a.duration)
    print(f"OK: {a.out} ({d} c)")
