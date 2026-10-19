#!/usr/bin/env python3
"""
Повний цикл одного рілсу:
  1) шукає вертикальне відео на Pexels за ключовими словами (пропускаючи вже використані);
  2) завантажує його;
  3) рендерить рілс через render_reel.py;
  4) пише meta.json (id відео, автор) — Apps Script забирає це разом з MP4.

Вхідні дані беруться зі змінних середовища (так їх передає GitHub Actions):
  REEL_TEXT, REEL_KEYWORDS, REEL_EXCLUDE (id через кому), PEXELS_API_KEY

Локальний тест без Pexels:
  REEL_TEXT="$(cat demo.txt)" python3 pipeline.py --video bg.mp4 --out out/reel.mp4 --meta out/meta.json
"""
import argparse
import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from render_reel import render  # noqa: E402
PEXELS_SEARCH = "https://api.pexels.com/videos/search"


def search_pexels(query: str, key: str) -> list:
    r = requests.get(
        PEXELS_SEARCH,
        headers={"Authorization": key},
        params={"query": query, "orientation": "portrait", "per_page": 40},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("videos", [])


def best_file(video: dict) -> dict | None:
    """Вертикальний mp4: найменший з висотою ≥1920, інакше найбільший доступний."""
    files = [
        f for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4" and f.get("height") and f.get("width")
        and f["height"] >= f["width"]
    ]
    if not files:
        return None
    full_hd = sorted([f for f in files if f["height"] >= 1920], key=lambda f: f["height"])
    if full_hd:
        return full_hd[0]
    return max(files, key=lambda f: f["height"])


def pick_video(keywords: str, exclude: set, key: str) -> tuple[dict, dict]:
    words = keywords.split()
    # спершу повний запит, потім коротші — щоб завжди щось знайшлося
    queries = [keywords] + [" ".join(words[:n]) for n in (3, 2, 1) if len(words) > n]
    for q in queries:
        for v in search_pexels(q, key):
            if str(v["id"]) in exclude or v.get("duration", 0) < 5:
                continue
            f = best_file(v)
            if f and f["height"] >= 1280:
                print(f"Pexels: '{q}' → id {v['id']} ({f['width']}x{f['height']}, {v['duration']} c)")
                return v, f
    raise SystemExit(f"Не знайшла відео на Pexels за запитом: {keywords}")


def font_path() -> str:
    """Roboto Medium: файл у репозиторії або системний (пакет fonts-roboto)."""
    import glob
    candidates = [os.path.join(HERE, "Roboto-Medium.ttf"), os.path.join(HERE, "Roboto-Medium.woff")]
    candidates += glob.glob("/usr/share/fonts/**/Roboto-Medium.ttf", recursive=True)
    for c in candidates:
        if os.path.exists(c):
            return c
    raise SystemExit("Не знайдено шрифт Roboto-Medium")


def grain_path() -> str:
    """Плівкове зерно: якщо файлу немає — генеруємо ffmpeg-ом (3 с, 1080x1920)."""
    import subprocess
    path = os.path.join(HERE, "pgr_film_grain_overlay.mp4")
    if not os.path.exists(path):
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x808080:s=540x960:r=24:d=3",
            "-vf", "format=gray,noise=c0s=40:c0f=t+u,scale=1080:1920:flags=neighbor,gblur=sigma=0.7,format=yuv420p",
            "-c:v", "libx264", "-crf", "30", "-preset", "slow", path,
        ], check=True)
    return path


def drive_id(value: str) -> str:
    """Приймає ID файлу або будь-яке посилання Google Drive на нього."""
    import re
    m = re.search(r"/d/([\w-]{20,})", value) or re.search(r"id=([\w-]{20,})", value)
    return m.group(1) if m else value


def download(url: str, path: str) -> None:
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--meta", required=True)
    p.add_argument("--video", help="локальне відео замість пошуку на Pexels (для тесту)")
    a = p.parse_args()

    # для ручного запуску з GitHub: «\n» у полі тексту = новий рядок
    text = os.environ.get("REEL_TEXT", "").replace("\r\n", "\n").replace("\\n", "\n").strip()
    if not text:
        sys.exit("Порожній текст (REEL_TEXT)")
    keywords = os.environ.get("REEL_KEYWORDS", "").strip() or "nature film"
    exclude = {x.strip() for x in os.environ.get("REEL_EXCLUDE", "").split(",") if x.strip()}

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    meta = {"keywords": keywords}

    drive_video = os.environ.get("REEL_VIDEO", "").strip()

    if a.video:
        bg = a.video
        meta.update({"pexels_id": "", "author": "local test"})
    elif drive_video:
        # відео з бібліотеки на Google Drive (файл має бути доступний «за посиланням»)
        fid = drive_id(drive_video)
        bg = os.path.join(os.path.dirname(os.path.abspath(a.out)), "bg.mp4")
        download(f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t", bg)
        with open(bg, "rb") as fh:
            head = fh.read(512).lower()
        if b"<html" in head or b"<!doctype" in head:
            sys.exit("Google Drive не віддав відео: відкрийте доступ до файлу/папки «Усі, хто має посилання»")
        meta.update({"video_id": fid, "source": "drive"})
    else:
        key = os.environ["PEXELS_API_KEY"]
        video, f = pick_video(keywords, exclude, key)
        bg = os.path.join(os.path.dirname(os.path.abspath(a.out)), "bg.mp4")
        download(f["link"], bg)
        meta.update({
            "pexels_id": str(video["id"]),
            "pexels_url": video.get("url", ""),
            "author": video.get("user", {}).get("name", ""),
            "author_url": video.get("user", {}).get("url", ""),
        })

    mode = os.environ.get("REEL_MODE", "").strip() or "auto"
    dur = render(bg, text, a.out, grain_path(), font_path(), None, mode)
    meta["duration"] = dur

    if not a.video and os.path.exists(bg) and bg.endswith("bg.mp4"):
        os.remove(bg)  # у артефакт кладемо лише готовий рілс
    with open(a.meta, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    print("Готово:", a.out, meta)


if __name__ == "__main__":
    main()
