#!/usr/bin/env python3
"""YT Factory: сценарий (YAML) -> готовое видео с озвучкой, субтитрами по слову и быстрым монтажом.

Использование:
    python3 factory.py scripts/de/001-katzen.yaml
    python3 factory.py scripts/de/001-katzen.yaml --format long
"""
import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
OUTPUT = ROOT / "output"
FONTS = ROOT / "assets" / "fonts"
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
UA = "yt-factory/0.1"

# В облачных окружениях с прокси нужен свой набор сертификатов; на Mac это не срабатывает.
if os.environ.get("SSL_CERT_FILE"):
    import certifi
    certifi.where = lambda: os.environ["SSL_CERT_FILE"]

import edge_tts  # noqa: E402  (импорт после настройки сертификатов)


def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"Ошибка команды:\n{' '.join(map(str, cmd))}\n{res.stderr[-2000:]}")
    return res.stdout


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", str(path)]).strip())


# ---------- Озвучка ----------

async def _tts(text, voice, rate, mp3_path):
    words = []
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({"text": chunk["text"], "start": start,
                              "end": start + chunk["duration"] / 1e7})
    return words


def synthesize(segments, voice, rate, work):
    """Озвучивает каждый сегмент отдельно; возвращает тайминги сегментов и слов."""
    timeline, words, t = [], [], 0.0
    for i, seg in enumerate(segments):
        mp3 = work / f"voice_{i:02d}.mp3"
        seg_words = asyncio.run(_tts(seg["text"], voice, rate, mp3))
        d = duration(mp3)
        for w in seg_words:
            words.append({**w, "start": w["start"] + t, "end": w["end"] + t})
        timeline.append({"start": t, "dur": d, "audio": mp3})
        t += d
    return timeline, words


# ---------- Картинки ----------

def _retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def _wikimedia_thumb(url, width=1280):
    """Wikimedia блокирует частые скачивания оригиналов, а уменьшенные копии отдаёт охотно."""
    m = re.match(r"(https://upload\.wikimedia\.org/wikipedia/commons)/(\w)/(\w\w)/([^/]+)$", url)
    if not m or not m.group(4).lower().endswith((".jpg", ".jpeg", ".png")):
        return url
    base, a, ab, name = m.groups()
    return f"{base}/thumb/{a}/{ab}/{name}/{width}px-{name}"


def _download(url, dest):
    def go():
        req = urllib.request.Request(_wikimedia_thumb(url), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    _retry(go)
    time.sleep(0.5)


def _get_json(url, headers=None):
    def go():
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    return _retry(go)


def _search_pexels(query, orientation, n):
    key = os.environ["PEXELS_API_KEY"]
    q = urllib.parse.quote(query)
    data = _get_json(f"https://api.pexels.com/videos/search?query={q}&orientation={orientation}&per_page={n}",
                     {"Authorization": key})
    out = []
    for v in data.get("videos", []):
        files = sorted((f for f in v["video_files"] if f.get("height") and f["height"] >= 1080),
                       key=lambda f: f["height"])
        if files:
            out.append({"url": files[0]["link"], "ext": ".mp4",
                        "credit": f"Video: {v['user']['name']} (Pexels)"})
    return out


def _search_openverse(query, orientation, n):
    q = urllib.parse.quote(query)
    aspect = "tall" if orientation == "portrait" else "wide"
    base = (f"https://api.openverse.org/v1/images/?q={q}&license_type=commercial,modification"
            f"&size=large&mature=false&page_size={n}")
    # Flickr отдаёт файлы надёжнее всех; вертикальных фото мало, поэтому дальше берём любые.
    data = {}
    for extra in (f"&source=flickr&aspect_ratio={aspect}", "&source=flickr", ""):
        data = _get_json(base + extra)
        if len(data.get("results", [])) >= n:
            break
    return [{"url": r["url"], "ext": Path(urllib.parse.urlparse(r["url"]).path).suffix.lower() or ".jpg",
             "credit": f"\"{r.get('title') or 'Image'}\" by {r.get('creator') or 'unknown'}, "
                       f"{r['license'].upper()} {r.get('license_version') or ''} ({r.get('foreign_landing_url')})"}
            for r in data.get("results", [])]


def fetch_media(query, orientation, n):
    """Локальный файл/папка из assets/media или поиск: Pexels (если есть ключ) -> Openverse."""
    local = ROOT / "assets" / "media" / query
    if local.is_file():
        return [(local, None)]
    if local.is_dir():
        return [(p, None) for p in sorted(local.iterdir()) if p.suffix.lower() in MEDIA_EXT]

    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40]
    folder = CACHE / f"{slug}-{orientation}-{hashlib.md5(query.encode()).hexdigest()[:6]}"
    meta = folder / "meta.json"
    if meta.exists():
        items = json.loads(meta.read_text())
    else:
        items = []
        try:
            if os.environ.get("PEXELS_API_KEY"):
                items = _search_pexels(query, orientation, n)
            if not items:
                items = _search_openverse(query, orientation, n)
        except Exception as e:  # сеть/лимиты — не роняем всё видео
            print(f"  ! поиск '{query}' не удался: {e}")
        folder.mkdir(parents=True, exist_ok=True)
        ok = []
        for i, it in enumerate(items[:n]):
            dest = folder / f"{i}{it['ext'] if it['ext'] in MEDIA_EXT else '.jpg'}"
            try:
                _download(it["url"], dest)
                ok.append({**it, "file": dest.name})
            except Exception as e:
                print(f"  ! не скачалось {it['url']}: {e}")
        items = ok
        if items:
            meta.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    return [(folder / it["file"], it["credit"]) for it in items]


# ---------- Монтаж ----------

def render_cut(src, dur, out, W, H, fps, variant, flash):
    """Один кадр-нарезка: заполнение экрана, «удар» зумом в начале, медленный дрейф, вспышка."""
    frames = max(1, round(dur * fps))
    fx = f",fade=t=in:st=0:d=0.12:color=white" if flash else ""
    if src.suffix.lower() in VIDEO_EXT:
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"scale=w='{W}*(1+0.18*max(0,1-t/0.18))':h=-2:eval=frame,crop={W}:{H},"
              f"fps={fps},setsar=1{fx}")
        cmd = ["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(src), "-t", f"{dur:.3f}",
               "-vf", vf, "-an"]
    else:
        # Кадр собирается в 2x размере (меньше дрожания при зуме), потом zoompan.
        drift = "1.0+0.0016*(on-5)" if variant % 2 == 0 else "1.12-0.0016*(on-5)"
        z = f"if(lt(on,5),1.28-0.04*on,{drift})"
        zoom = (f"zoompan=z='{z}':d={frames}:s={W}x{H}:fps={fps}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',setsar=1{fx}")
        w, h = map(int, run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=width,height", "-of", "csv=p=0", str(src)]).strip().split(",")[:2])
        if (w > h) != (W > H):
            # Горизонтальное фото в вертикальном видео (или наоборот): фото целиком поверх размытого фона.
            vf = (f"split[a][b];[a]scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
                  f"boxblur=40:2,eq=brightness=-0.08[bg];[b]scale={W*2}:{H*2}:force_original_aspect_ratio=decrease[fg];"
                  f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{zoom}")
        else:
            vf = f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},{zoom}"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", vf, "-frames:v", str(frames)]
    run(cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)])


def render_placeholder(dur, out, W, H, fps, variant):
    colors = ["0x1d3557", "0x6a040f", "0x14213d", "0x3a0ca3"]
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"color=c={colors[variant % len(colors)]}:s={W}x{H}:r={fps}:d={dur:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])


# ---------- Субтитры ----------

def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(words, highlights, total, W, H, fmt_cfg, style, channel, path):
    fs = fmt_cfg["font_size"]
    y = int(H * fmt_cfg["caption_y"])
    hl = {h.lower() for h in highlights}
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Word,{style['font']},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(6, fs // 13)},4,5,40,40,0,1",
        f"Style: Mark,{style['font']},{fs // 2},&H8CFFFFFF,&H8CFFFFFF,&HB4000000,&H00000000,-1,0,0,0,100,100,4,0,1,"
        f"2,0,2,40,40,{int(H * 0.06)},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i, w in enumerate(words):
        end = words[i + 1]["start"] if i + 1 < len(words) else min(total, w["end"] + 0.4)
        text = w["text"].replace("{", "").replace("}", "")
        color = ""
        if any(ch.isdigit() for ch in text) or text.lower().strip(".,!?") in hl:
            color = f"\\c{style['highlight_color']}"
        pop = "\\fscx125\\fscy125\\t(0,90,\\fscx100\\fscy100)"
        lines.append(f"Dialogue: 1,{ass_time(w['start'])},{ass_time(end)},Word,,0,0,0,,"
                     f"{{\\an5\\pos({W // 2},{y}){pop}{color}}}{text}")
    lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Mark,,0,0,0,,{channel}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- Сборка ----------

def build(script_path, fmt):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    script = yaml.safe_load(Path(script_path).read_text(encoding="utf-8"))
    lang = script.get("lang", "de")
    fmt = fmt or script.get("format", "short")
    fc, style = cfg["formats"][fmt], cfg["style"]
    W, H, fps = fc["width"], fc["height"], style["fps"]
    orientation = "portrait" if H > W else "landscape"
    vcfg = {**cfg["voices"][lang], **script.get("voice", {})}

    name = f"{Path(script_path).stem}-{lang}-{fmt}"
    work = OUTPUT / name / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    segments = script["segments"]
    print(f"[1/4] Озвучка ({vcfg['voice']}) — {len(segments)} сегментов")
    timeline, words = synthesize(segments, vcfg["voice"], vcfg["rate"], work)
    total = timeline[-1]["start"] + timeline[-1]["dur"]

    print("[2/4] Подбор картинок")
    credits = set()
    cuts, n = [], 0
    for i, (seg, tl) in enumerate(zip(segments, timeline)):
        pool = []
        for q in seg.get("visuals", []):
            pool += fetch_media(q, orientation, 4)
        k = max(1, round(tl["dur"] / style["cut_seconds"]))
        bounds = [tl["dur"] * j / k for j in range(k + 1)]
        for j in range(k):
            src, credit = pool[j % len(pool)] if pool else (None, None)
            if credit:
                credits.add(credit)
            cuts.append((src, bounds[j + 1] - bounds[j], n, j == 0))
            n += 1

    print(f"[3/4] Монтаж — {len(cuts)} склеек")
    listing = []
    for idx, (src, d, variant, flash) in enumerate(cuts):
        out = work / f"cut_{idx:03d}.mp4"
        if src:
            render_cut(src, d, out, W, H, fps, variant, flash)
        else:
            render_placeholder(d, out, W, H, fps, variant)
        listing.append(f"file '{out.name}'")
    (work / "cuts.txt").write_text("\n".join(listing) + "\n")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(work / "cuts.txt"),
         "-c", "copy", str(work / "video.mp4")])

    (work / "voice.txt").write_text("".join(f"file '{t['audio'].name}'\n" for t in timeline))
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(work / "voice.txt"),
         "-ar", "48000", "-ac", "2", str(work / "voice.wav")])

    print("[4/4] Субтитры, музыка, финальный рендер")
    build_ass(words, script.get("highlight", []), total, W, H, fc, style, cfg["channel_name"], work / "subs.ass")

    inputs = ["-i", str(work / "video.mp4"), "-i", str(work / "voice.wav")]
    afilter = "[1:a]volume=1.0[v]"
    mix = ["[v]"]
    music = sorted((ROOT / "assets" / "music").glob("*.mp3"))
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(random.choice(music))]
        afilter += f";[{len(inputs) // 2 - 1}:a]volume={style['music_volume']}[m]"
        mix.append("[m]")
    sfx = sorted((ROOT / "assets" / "sfx").glob("*.mp3"))
    if sfx:
        idx = len(inputs) // 2
        inputs += ["-i", str(sfx[0])]
        starts = [t["start"] for t in timeline[1:]]
        if starts:
            afilter += f";[{idx}:a]volume={style['sfx_volume']},asplit={len(starts)}" + \
                "".join(f"[s{j}]" for j in range(len(starts)))
            for j, s in enumerate(starts):
                ms = max(0, int((s - 0.15) * 1000))
                afilter += f";[s{j}]adelay={ms}|{ms}[d{j}]"
                mix.append(f"[d{j}]")
    afilter += f";{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[aout]"

    final = OUTPUT / name / f"{name}.mp4"
    subs = str(work / "subs.ass").replace("\\", "/").replace(":", "\\:")
    run(["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration=1,ass='{subs}':fontsdir='{FONTS}'[vout];{afilter}",
         "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)])

    desc = [script.get("description", "").strip(), ""]
    if script.get("sources"):
        desc += ["Quellen:"] + [f"- {s}" for s in script["sources"]] + [""]
    if credits:
        desc += ["Bildnachweise:"] + [f"- {c}" for c in sorted(credits)] + [""]
    desc += [" ".join(f"#{t}" for t in script.get("hashtags", []))]
    (OUTPUT / name / "upload.txt").write_text(
        f"TITLE:\n{script.get('title', '')}\n\nDESCRIPTION:\n" + "\n".join(desc) +
        f"\n\nTAGS:\n{', '.join(script.get('tags', []))}\n", encoding="utf-8")

    shutil.rmtree(work)
    print(f"\nГотово: {final}  ({total:.1f} сек)\nТекст для загрузки: {OUTPUT / name / 'upload.txt'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Сценарий YAML -> готовое видео")
    ap.add_argument("script", nargs="+", help="путь к сценарию(ям) .yaml")
    ap.add_argument("--format", choices=["short", "long"], help="переопределить формат из сценария")
    a = ap.parse_args()
    for s in a.script:
        build(s, a.format)
