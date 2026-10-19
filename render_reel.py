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
    "font_size_max": 30,       # px — як у макеті (Roboto 30)
    "font_size_min": 24,       # px — нижче не зменшуємо
    "line_height": 1.27,       # крок рядка (стандартний для Roboto)
    "blank_line": 0.8,         # висота порожнього рядка між абзацами (частка рядка)
    "box_width": 0.6,          # абсолютний максимум ширини блоку (частка кадру)
    "line_chars": "більше не воюєш",  # еталон найширшого рядка: довші рядки переносяться автоматично
    "safe_top": 250,           # px — зверху інтерфейс Instagram
    "safe_bottom": 420,        # px — знизу підпис і кнопки
    "center_y": 0.5,           # центр тексту по висоті
    "text_color": (255, 255, 255, 255),
    "shadow_alpha": 0,         # 0 = без тіні
    # --- VHS-обробка відео ---
    "vhs_res": 360,            # ширина «плівки» перед розтягуванням (менше = м'якше)
    "blur": 1.2,               # додаткова м'якість
    "chroma_shift": 3,         # px — розбіжність кольорів по краях
    "sepia": 0.60,             # сила сепії 0..1
    "saturation": 0.85,
    "black_lift": 0.10,        # «вицвілі» тіні 0..0.2
    "white_cap": 0.90,         # приглушені світлі 0.8..1
    "darken": 0.12,            # затемнення 0..1
    "grain_opacity": 0.55,     # сила зерна 0..1
    "fade_in": 0,              # 0 = текст видно з першого кадру
    "fade_out": 0,             # 0 = текст видно до останнього кадру
}


def reading_duration(text: str) -> float:
    """Скільки секунд тримати текст: ~2.5 слова/с + 3 с запасу, у межах 8–30 с."""
    words = len(text.split())
    return round(min(30.0, max(8.0, words / 2.5 + 3.0)), 1)


SHORT_WORDS = {"і", "й", "в", "у", "з", "а", "о", "я", "та", "не", "до", "на", "за", "по", "від", "із", "зі"}


def wrap_line(line: str, font, max_px: float) -> list:
    """Розбиває задовгий рядок на рівні за шириною частини.
    Короткі слова (і, в, з, не...) не лишаються в кінці рядка — переносяться разом із наступним."""
    if font.getlength(line) <= max_px:
        return [line]
    words = line.split()
    # склеюємо короткі службові слова з наступним словом
    units, i = [], 0
    while i < len(words):
        w = words[i]
        while w.split()[-1].lower().strip("«»,.") in SHORT_WORDS and not w.endswith(",") and i + 1 < len(words):
            i += 1
            w = w + " " + words[i]
        units.append(w)
        i += 1
    total = font.getlength(" ".join(units))
    n = 2
    while n <= len(units):
        target = total / n
        out, cur = [], ""
        for u in units:
            cand = (cur + " " + u).strip()
            after_comma = cur.endswith((",", "—", ":")) and font.getlength(cur) >= target * 0.55
            if cur and (font.getlength(cand) > target * 1.15 or after_comma) and len(out) < n - 1:
                out.append(cur)
                cur = u
            else:
                cur = cand
        out.append(cur)
        if max(font.getlength(x) for x in out) <= max_px:
            return out
        n += 1
    return units


def render_text_png(text: str, font_path: str, out_png: str) -> None:
    raw_lines = text.strip("\n").split("\n")
    max_w = int(W * STYLE["box_width"])
    max_h = H - STYLE["safe_top"] - STYLE["safe_bottom"]

    # Підбираємо найбільший шрифт, з яким текст влазить у безпечну зону
    size = STYLE["font_size_max"]
    while True:
        font = ImageFont.truetype(font_path, size)
        line_px = min(font.getlength(STYLE["line_chars"]) * 1.05, max_w)
        lines = []
        for rl in raw_lines:
            lines += wrap_line(rl, font, line_px) if rl.strip() else [""]
        lh = round(size * STYLE["line_height"])
        gap = round(lh * STYLE["blank_line"])
        ys, y = [], 0
        for l in lines:
            ys.append(y)
            y += lh if l.strip() else gap
        block_h = y
        widths = [font.getlength(l) for l in lines]
        if (max(widths or [0]) <= max_w and block_h <= max_h) or size <= STYLE["font_size_min"]:
            break
        size -= 1

    top = int(H * STYLE["center_y"] - block_h / 2)
    top = max(STYLE["safe_top"], min(top, H - STYLE["safe_bottom"] - block_h))

    def draw_lines(layer, dy, fill):
        d = ImageDraw.Draw(layer)
        for line, y0 in zip(lines, ys):
            if line.strip():
                d.text(((W - font.getlength(line)) / 2, top + y0 + dy), line, font=font, fill=fill)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if STYLE["shadow_alpha"]:
        from PIL import ImageFilter
        draw_lines(img, 2, (0, 0, 0, STYLE["shadow_alpha"]))
        img = img.filter(ImageFilter.GaussianBlur(6))
    draw_lines(img, 0, STYLE["text_color"])
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
            f"scale={STYLE['vhs_res']}:-2:flags=area,scale={W}:{H}:flags=bicubic,gblur=sigma={STYLE['blur']},"
            f"chromashift=cbh={STYLE['chroma_shift']}:crh=-{STYLE['chroma_shift']},"
            f"colorchannelmixer={sepia_darken_matrix(STYLE['sepia'], STYLE['darken'])},"
            f"eq=saturation={STYLE['saturation']},"
            f"curves=all='0/{STYLE['black_lift']} 1/{STYLE['white_cap']}',format=gbrp[bg];"
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
