#!/usr/bin/env python3
"""
PGR reel renderer — власна заміна Creatomate.

Збирає вертикальний рілс 1080x1920:
  фонове відео (cover) + плівкова обробка (вицвіле, молочні тіні, ореол, віньєтка)
  + кольорове плівкове зерно (генерується ffmpeg, файл зерна не потрібен)
  + текст по центру (Roboto Medium): блоком або абзацами по черзі.

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
    # --- плівкова обробка (погоджено 20.10.2026 за референсом 1-4: вицвіло, молочні тіні, ореол, кольорове зерно) ---
    "film_res": 200,           # ширина «плівки» перед розтягуванням (менше = м'якше, як VHS)
    "blur": 1.0,               # додаткова м'якість
    "vhs_luma_smear": 3,       # горизонтальне розмиття яскравості (VHS)
    "vhs_chroma_smear": 12,    # колір «тече» по горизонталі
    "vhs_chroma_shift": 5,     # px — зсув кольору
    "streaks": 0.5,            # горизонтальні смуги від світлих ділянок 0..1
    "warm": 0.06,              # тепло-коричневий зсув (прибирає «цифровий» синій)
    "vhs_saturation": 0.6,     # фінальне приглушення кольорів
    "shadow_red": 0.01,        # тонування тіней (червоний)
    "high_blue": 0.01,         # тонування світла (синій)
    "saturation": 1.15,        # насиченість (після кривої кадр і так вицвітає)
    "curve": "0/0.15 0.25/0.29 0.5/0.45 0.75/0.57 1/0.68",  # підняті чорні, притиснуті білі
    "vignette": "PI/6",        # віньєтка (кут; більше = сильніше)
    "bloom_threshold": 0.4,    # з якої яскравості світиться ореол
    "bloom_radius": 30,
    "bloom": 0.4,              # сила ореолу 0..1
    "grain_size": 2.35,        # розмір зерна (1 = піксель)
    "grain_strength": 85,      # амплітуда шуму
    "grain_soft": 0.55,        # м'якість зернин
    "grain_opacity": 0.85,     # сила зерна 0..1 (зерно кольорове, мерехтить)
    "max_bitrate": "3500k",  # щоб 20-секундний рілс був < 10 МБ
    # --- режим «по черзі»: абзаци (через порожній рядок) з'являються по одному ---
    "seq_words_per_sec": 3.0,  # швидкість читання
    "seq_min": 2.5,            # с — мінімум на один пункт
    "seq_pad": 0.8,            # с — запас на кожен пункт
    "seq_last_extra": 1.5,     # с — фінальний пункт стоїть довше
    "fade_in": 0,              # 0 = текст видно з першого кадру
    "fade_out": 0,             # 0 = текст видно до останнього кадру
    # --- шаблон «список»: кожен пункт — окреме відео (за референсом «ти 10/10») ---
    "list_font_size": 42,      # px у кадрі 1080x1920
    "list_line_height": 1.2,
    "list_line_px": 720,       # максимальна ширина рядка, px (~35 знаків)
    "list_item_sec": 3.0,      # с на кожен пункт
    "list_last_sec": 5.0,      # с на фінальний пункт
    "list_end_fade": 1.2,      # с — в кінці кадр плавно йде в однотонний фон
    "list_end_color": "0x9a9080",
    "list_positions": [0.25, 0.37, 0.5, 0.62, 0.72],  # можливі центри тексту по висоті
}


def reading_duration(text: str) -> float:
    """Скільки секунд тримати текст: ~2.5 слова/с + 3 с запасу, у межах 8–30 с."""
    words = len(text.split())
    return round(min(30.0, max(8.0, words / 2.5 + 3.0)), 1)


SHORT_WORDS = {"і", "й", "в", "у", "з", "а", "о", "я", "та", "не", "до", "на", "за", "по", "від", "із", "зі"}


# слова, на яких краще не закінчувати рядок (вони тягнуть за собою продовження)
WEAK_END = {"як", "що", "коли", "бо", "де", "щоб", "ніж", "якщо", "мій", "моя", "моє", "мої", "моїм", "твій", "твоя", "твоїм"}


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
    # мінімальна кількість рядків, а серед варіантів — найрівніші за шириною (переважно розрив після коми)
    m = len(units)
    width = lambda a, b: font.getlength(" ".join(units[a:b]))
    for n in range(2, m + 1):
        best = None
        def parts(start, k):
            if k == 1:
                yield [(start, m)]
                return
            for cut in range(start + 1, m - k + 2):
                for rest in parts(cut, k - 1):
                    yield [(start, cut)] + rest
        for split in parts(0, n):
            ws = [width(a, b) for a, b in split]
            if max(ws) > max_px:
                continue
            commas = sum(1 for a, b in split[:-1] if units[b - 1].endswith((",", "—", ":")))
            weak = sum(1 for a, b in split[:-1] if units[b - 1].lower() in WEAK_END)
            score = (max(ws) - min(ws)) - commas * 80 + weak * 80
            if best is None or score < best[0]:
                best = (score, split)
        if best:
            return [" ".join(units[a:b]) for a, b in best[1]]
    return units


def render_text_png(text: str, font_path: str, out_png: str, size: int | None = None,
                    line_px_fixed: float | None = None, line_height: float | None = None,
                    center_y: float | None = None) -> bool:
    """Малює текстовий блок у PNG. Повертає True, якщо текст вмістився без зменшення шрифту."""
    raw_lines = text.strip("\n").split("\n")
    max_w = int(W * STYLE["box_width"])
    max_h = H - STYLE["safe_top"] - STYLE["safe_bottom"]

    # Підбираємо найбільший шрифт, з яким текст влазить у безпечну зону
    fixed = size is not None
    size = size or STYLE["font_size_max"]
    lh_k = line_height or STYLE["line_height"]
    cy = STYLE["center_y"] if center_y is None else center_y
    while True:
        font = ImageFont.truetype(font_path, size)
        line_px = line_px_fixed or min(font.getlength(STYLE["line_chars"]) * 1.12, max_w)
        lines = []
        for rl in raw_lines:
            lines += wrap_line(rl, font, line_px) if rl.strip() else [""]
        lh = round(size * lh_k)
        gap = round(lh * STYLE["blank_line"])
        ys, y = [], 0
        for l in lines:
            ys.append(y)
            y += lh if l.strip() else gap
        block_h = y
        widths = [font.getlength(l) for l in lines]
        if fixed or (max(widths or [0]) <= max_w and block_h <= max_h) or size <= STYLE["font_size_min"]:
            break
        size -= 1

    top = int(H * cy - block_h / 2)
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
    return size == STYLE["font_size_max"]


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


def paragraphs(text: str) -> list:
    out, cur = [], []
    for line in text.strip("\n").split("\n"):
        if line.strip():
            cur.append(line)
        elif cur:
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out

def grain_dims() -> tuple:
    return int(W / STYLE["grain_size"]) // 2 * 2, int(H / STYLE["grain_size"]) // 2 * 2


def prep_chain() -> str:
    """Будь-яке відео → 1080x1920 (cover), FPS."""
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}"


def grade_chain(src: str, grain_idx: int) -> str:
    """Плівка/VHS + зерно. На вході мітка src, на виході [b0]."""
    wm = STYLE["warm"]
    return (
        f"[{src}]scale={STYLE['film_res']}:-2:flags=area,scale={W}:{H}:flags=bicubic,gblur=sigma={STYLE['blur']},"
        f"colorbalance=rs={STYLE['shadow_red']}:bh={STYLE['high_blue']},"
        f"eq=saturation={STYLE['saturation']},vignette=angle={STYLE['vignette']},"
        f"curves=all='{STYLE['curve']}',"
        f"split[a][b];[b]scale={W // 4}:{H // 4}:flags=area,curves=all='0/0 {STYLE['bloom_threshold']}/0 1/1',"
        f"gblur=sigma={STYLE['bloom_radius'] / 4},scale={W}:{H}:flags=bicubic[gl];"
        f"[a][gl]blend=all_mode=screen:all_opacity={STYLE['bloom']},"
        # --- VHS: розмиття по горизонталі, колір «тече», смуги від світла, тепло-коричневий тон ---
        f"format=yuv444p,gblur=sigma={STYLE['vhs_luma_smear']}:sigmaV=0.6:planes=1,"
        f"gblur=sigma={STYLE['vhs_chroma_smear']}:sigmaV=1.5:planes=6,"
        f"chromashift=cbh={STYLE['vhs_chroma_shift']}:crh=-{STYLE['vhs_chroma_shift']},format=gbrp,"
        f"split[c][d];[d]scale={W // 4}:{H // 4},curves=all='0/0 0.45/0 1/1',"
        f"gblur=sigma=14:sigmaV=0.8,scale={W}:{H}[sm];"
        f"[c][sm]blend=all_mode=screen:all_opacity={STYLE['streaks']},"
        f"colorbalance=rs={wm}:gs={wm / 2}:bs=-{wm}:rm={wm}:gm={wm / 2}:bm=-{wm}:rh={wm}:gh={wm / 3}:bh=-{wm},"
        f"eq=saturation={STYLE['vhs_saturation']},format=gbrp[bg];"
        f"[{grain_idx}:v]format=gbrp,noise=alls={STYLE['grain_strength']}:allf=t+u,"
        f"scale={W}:{H}:flags=bicubic,gblur=sigma={STYLE['grain_soft']},format=gbrp[gr];"
        f"[bg][gr]blend=all_mode=overlay:all_opacity={STYLE['grain_opacity']},format=yuv420p[b0]"
    )


def calm_position(video: str, tmp: str, block_h: int) -> float:
    """Обирає висоту для тексту там, де кадр найспокійніший (найменше деталей і найменше світла)."""
    import numpy as np
    from PIL import ImageFilter
    png = os.path.join(tmp, "probe.png")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", video,
                    "-vf", prep_chain() + ",scale=270:480", "-frames:v", "1", png], check=False)
    if not os.path.exists(png):
        return 0.5
    g = Image.open(png).convert("L")
    edges = np.asarray(g.filter(ImageFilter.FIND_EDGES)).astype(float)
    lum = np.asarray(g).astype(float)
    best, best_y = None, 0.5
    for cy in STYLE["list_positions"]:
        y0 = int((cy * H - block_h / 2) / 4); y1 = int((cy * H + block_h / 2) / 4)
        band_e = edges[max(0, y0):y1, 50:220].mean()
        band_l = lum[max(0, y0):y1, 50:220].mean()
        score = band_e + max(0, band_l - 150) * 0.5 + abs(cy - 0.5) * 8  # трохи віддаємо перевагу центру
        if best is None or score < best:
            best, best_y = score, cy
    os.remove(png)
    return best_y


def render_list(videos: list, text: str, out: str, font: str) -> float:
    """Шаблон «список»: пункт N (абзац) іде на відео N, кожен по 3 с, текст стає в спокійне місце кадру."""
    paras = paragraphs(text)
    n = len(paras)
    vids = [videos[i % len(videos)] for i in range(n)]
    durs = [STYLE["list_item_sec"]] * (n - 1) + [STYLE["list_last_sec"]]
    total = round(sum(durs), 2)
    with tempfile.TemporaryDirectory() as tmp:
        inputs, parts, t = [], [], 0.0
        for i, v in enumerate(vids):
            inputs += ["-stream_loop", "-1", "-i", v]
            parts.append(f"[{i}:v]{prep_chain()},trim=duration={durs[i]},setpts=PTS-STARTPTS[s{i}]")
        gw, gh = grain_dims()
        inputs += ["-f", "lavfi", "-i", f"color=c=0x808080:s={gw}x{gh}:r={FPS}"]
        filt = ";".join(parts) + ";" + "".join(f"[s{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[raw];"
        filt += grade_chain("raw", n)
        fd = STYLE["list_end_fade"]
        filt += f";[b0]fade=t=out:st={total - fd}:d={fd}:color={STYLE['list_end_color']}[f0]"
        prev = "f0"
        size, lh = STYLE["list_font_size"], STYLE["list_line_height"]
        for i, para in enumerate(paras):
            # висота блоку, щоб правильно оцінити місце
            font_obj = ImageFont.truetype(font, size)
            nlines = sum(len(wrap_line(l, font_obj, STYLE["list_line_px"])) for l in para.split("\n"))
            cy = calm_position(vids[i], tmp, int(nlines * size * lh))
            png = os.path.join(tmp, f"l{i}.png")
            render_text_png(para, font, png, size=size, line_px_fixed=STYLE["list_line_px"], line_height=lh, center_y=cy)
            a = round(t, 3); b = round(t + durs[i], 3); t += durs[i]
            inputs += ["-loop", "1", "-framerate", str(FPS), "-i", png]
            k = n + 1 + i
            filt += f";[{k}:v]format=rgba[t{i}];[{prev}][t{i}]overlay=0:0:format=auto:enable='between(t,{a},{b})'[o{i}]"
            prev = f"o{i}"
        filt += f";[{prev}]format=yuv420p[v]"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
               "-filter_complex", filt, "-map", "[v]", "-t", str(total), "-an",
               "-c:v", "libx264", "-preset", "medium", "-crf", "21",
               "-maxrate", STYLE["max_bitrate"], "-bufsize", "8M",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        subprocess.run(cmd, check=True)
    return total


def render(video: str, text: str, out: str, grain: str, font: str,
           duration: float | None, mode: str = "auto") -> float:
    """mode: block — весь текст одним блоком; sequence — абзаци по черзі;
    auto — блок, якщо вміщається без зменшення шрифту, інакше по черзі."""
    with tempfile.TemporaryDirectory() as tmp:
        layers = []  # (png, start, end)
        block_png = os.path.join(tmp, "text.png")
        fits = render_text_png(text, font, block_png)
        paras = paragraphs(text)
        sequence = mode == "sequence" or (mode == "auto" and not fits and len(paras) > 1)

        if sequence:
            t = 0.0
            for i, para in enumerate(paras):
                png = os.path.join(tmp, f"p{i}.png")
                render_text_png(para, font, png)
                d = max(STYLE["seq_min"], len(para.split()) / STYLE["seq_words_per_sec"] + STYLE["seq_pad"])
                if i == len(paras) - 1:
                    d += STYLE["seq_last_extra"]
                layers.append((png, round(t, 2), round(t + d, 2)))
                t += d
            dur = round(t, 1)
        else:
            dur = duration or reading_duration(text)
            layers.append((block_png, 0, dur))

        fades = []
        if STYLE["fade_in"]:
            fades.append(f"fade=t=in:st=0:d={STYLE['fade_in']}:alpha=1")
        if STYLE["fade_out"]:
            fades.append(f"fade=t=out:st={max(0.0, dur - STYLE['fade_out'])}:d={STYLE['fade_out']}:alpha=1")
        txt_chain = ",".join(["format=rgba"] + fades)

        gw, gh = grain_dims()
        filt = f"[0:v]{prep_chain()}[raw];" + grade_chain("raw", 1)
        inputs = ["-stream_loop", "-1", "-i", video,
                  "-f", "lavfi", "-i", f"color=c=0x808080:s={gw}x{gh}:r={FPS}"]
        prev = "b0"
        for k, (png, a, b) in enumerate(layers):
            inputs += ["-loop", "1", "-framerate", str(FPS), "-i", png]
            chain = txt_chain if not sequence else "format=rgba"
            filt += f";[{k + 2}:v]{chain}[t{k}]"
            enable = "" if not sequence else f":enable='between(t,{a},{b})'"
            filt += f";[{prev}][t{k}]overlay=0:0:format=auto{enable}[b{k + 1}]"
            prev = f"b{k + 1}"
        filt += f";[{prev}]format=yuv420p[v]"

        cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
               "-filter_complex", filt, "-map", "[v]",
               "-t", str(dur), "-an",
               "-c:v", "libx264", "-preset", "medium", "-crf", "21",
               "-maxrate", STYLE["max_bitrate"], "-bufsize", "8M",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               out]
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
    p.add_argument("--mode", default="auto", choices=["auto", "block", "sequence"])
    a = p.parse_args()
    with open(a.text, encoding="utf-8") as f:
        txt = f.read()
    d = render(a.video, txt, a.out, a.grain, a.font, a.duration, a.mode)
    print(f"OK: {a.out} ({d} c)")
