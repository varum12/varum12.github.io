#!/usr/bin/env python3
"""YT Factory: сценарий (YAML) -> готовое видео в мемной подаче.

Озвучка, субтитры по слову, быстрые склейки с «ударом» зумом, анимированные эмодзи,
стикеры-надписи, тряска, глитч, звуковые эффекты, музыка с приглушением под голос,
обложка, текст для загрузки и автоматическая нарезка Shorts из длинного видео.

Использование:
    ./video scripts/de/001-gehirn-absturz.yaml          # одно видео
    ./video scripts/de/*.yaml                           # все сценарии подряд
    ./video scripts/de/100-long.yaml --format long      # 16:9 + Shorts из него
    ./video --voice-samples de                          # образцы голосов на выбор
"""
import argparse
import asyncio
import base64
import concurrent.futures as cf
import functools
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
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
UA = "YTFactory/1.0 (https://github.com/varum12/varum12.github.io) python-urllib"
EMOJI_URL = "https://fonts.gstatic.com/s/e/notoemoji/latest/{code}/512.{ext}"
EMOJI_CREDIT = "Animierte Emojis: Noto Emoji von Google, CC BY 4.0"
WORKERS = max(2, (os.cpu_count() or 4) // 2)

# В облачных окружениях с прокси нужен свой набор сертификатов; на Mac это не срабатывает.
if os.environ.get("SSL_CERT_FILE"):
    import certifi
    certifi.where = lambda: os.environ["SSL_CERT_FILE"]

import edge_tts  # noqa: E402  (импорт после настройки сертификатов)


@functools.cache
def ff_bins():
    """ffmpeg с фильтром ass (libass) и ffprobe рядом с ним.

    На Mac обычный `brew install ffmpeg` собран без libass, поэтому сначала ищем ffmpeg-full."""
    candidates = [os.environ.get("FFMPEG_BIN"), "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
                  "/usr/local/opt/ffmpeg-full/bin/ffmpeg", shutil.which("ffmpeg")]
    for c in dict.fromkeys(filter(None, candidates)):
        if not Path(c).exists():
            continue
        filters = subprocess.run([c, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if re.search(r"^\s*\S+\s+ass\s", filters, re.M):
            probe = Path(c).with_name("ffprobe")
            return c, str(probe) if probe.exists() else (shutil.which("ffprobe") or "ffprobe")
    sys.exit("Не найден ffmpeg с поддержкой субтитров (libass).\n"
             "На Mac выполните: brew install ffmpeg-full")


def run(cmd):
    cmd = list(cmd)
    if cmd[0] in ("ffmpeg", "ffprobe"):
        cmd[0] = ff_bins()[0 if cmd[0] == "ffmpeg" else 1]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        line = " ".join(map(str, cmd))
        err = res.stderr if len(res.stderr) <= 3000 else res.stderr[:1800] + "\n...\n" + res.stderr[-1000:]
        sys.exit(f"Ошибка команды:\n{line[:400]}{' ...' if len(line) > 400 else ''}\n{err}")
    return res.stdout


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", str(path)]).strip())


def frame_size(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height", "-of", "csv=p=0", str(path)])
    w, h = out.strip().splitlines()[0].split(",")[:2]
    return int(w), int(h)


# ---------- Сеть ----------

def _retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            # 4xx (кроме «слишком часто») повтором не лечится — сразу дальше.
            client_error = isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429
            if i == tries - 1 or client_error:
                raise
            time.sleep(2 * (i + 1))


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


def _get_json(url, headers=None, data=None):
    def go():
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=45 if data is None else 180) as r:
            return json.load(r)
    return _retry(go, tries=2 if data is None else 1)


# ---------- Озвучка ----------

def _clean_word(w):
    return w.strip(".,!?;:\"'„“”»«…()[]–—")


async def _tts_edge(text, voice, rate, mp3_path):
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


def _tts_elevenlabs(text, voice_id, model, mp3_path):
    """Платный голос ElevenLabs (нужен ELEVENLABS_API_KEY). Тайминги слов собираются из таймингов букв."""
    key = os.environ.get("ELEVENLABS_API_KEY") or sys.exit("Нет ELEVENLABS_API_KEY")
    body = json.dumps({"text": text, "model_id": model}).encode()
    data = _get_json(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
                     f"?output_format=mp3_44100_128", {"xi-api-key": key, "Content-Type": "application/json"}, body)
    Path(mp3_path).write_bytes(base64.b64decode(data["audio_base64"]))
    al = data["alignment"]
    words, cur, ws, we = [], "", 0.0, 0.0
    for c, s, e in zip(al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]):
        if c.isspace():
            if cur:
                words.append({"text": cur, "start": ws, "end": we})
            cur = ""
        else:
            ws = s if not cur else ws
            cur, we = cur + c, e
    if cur:
        words.append({"text": cur, "start": ws, "end": we})
    return [{**w, "text": _clean_word(w["text"])} for w in words if _clean_word(w["text"])]


def tts(text, vcfg, mp3_path):
    if vcfg.get("engine") == "elevenlabs":
        return _tts_elevenlabs(text, vcfg["voice"], vcfg.get("model", "eleven_multilingual_v2"), mp3_path)
    return _retry(lambda: asyncio.run(_tts_edge(text, vcfg["voice"], vcfg.get("rate", "+0%"), mp3_path)))


def _mark_breaks(text, words):
    """Отмечает слова, после которых в тексте стоит знак препинания — там рвём группу субтитров."""
    pos = 0
    for w in words:
        i = text.find(w["text"], pos)
        if i < 0:
            continue
        pos = i + len(w["text"])
        rest = text[pos:pos + 4].lstrip("\"'“”»«)")
        w["brk"] = bool(rest) and rest[0] in ".!?:;,–—"
    return words


def synthesize(segments, vcfg, work):
    """Озвучивает каждый сегмент отдельно; возвращает тайминги сегментов и слов (в секундах от начала)."""
    def one(i):
        mp3 = work / f"voice_{i:03d}.mp3"
        text = str(segments[i]["text"])
        return mp3, _mark_breaks(text, tts(text, vcfg, mp3))

    with cf.ThreadPoolExecutor(4) as ex:
        results = list(ex.map(one, range(len(segments))))
    timeline, words, t = [], [], 0.0
    for i, (mp3, seg_words) in enumerate(results):
        d = duration(mp3)
        for w in seg_words:
            words.append({**w, "start": w["start"] + t, "end": w["end"] + t, "seg": i})
        timeline.append({"start": t, "dur": d, "audio": mp3})
        t += d
    return timeline, words


# ---------- Картинки и видео ----------

def _search_pexels(query, orientation, n):
    hdr = {"Authorization": os.environ["PEXELS_API_KEY"]}
    q = urllib.parse.quote(query)
    out = []
    data = _get_json(f"https://api.pexels.com/videos/search?query={q}&orientation={orientation}"
                     f"&size=medium&per_page={n * 2}", hdr)
    for v in sorted(data.get("videos", []), key=lambda v: v.get("duration") or 99):
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"
                 and f.get("width") and f.get("height") and min(f["width"], f["height"]) >= 1080]
        if files:
            best = min(files, key=lambda f: f["width"] * f["height"])
            out.append({"url": best["link"], "ext": ".mp4", "credit": f"Video: {v['user']['name']} (Pexels)"})
        if len(out) >= n:
            return out
    data = _get_json(f"https://api.pexels.com/v1/search?query={q}&orientation={orientation}&per_page={n}", hdr)
    out += [{"url": p["src"]["large2x"], "ext": ".jpg", "credit": f"Foto: {p['photographer']} (Pexels)"}
            for p in data.get("photos", [])]
    return out[:n]


def _search_pixabay(query, orientation, n):
    """Pixabay: сначала видео (вертикальные — первыми для Shorts), потом фото. Нужен PIXABAY_API_KEY."""
    key = urllib.parse.quote(os.environ["PIXABAY_API_KEY"])
    q = urllib.parse.quote(query[:100])
    portrait = orientation == "portrait"
    data = _get_json(f"https://pixabay.com/api/videos/?key={key}&q={q}&video_type=film"
                     f"&safesearch=true&per_page={max(3, n * 3)}")
    candidates = []
    for h in data.get("hits", []):
        files = [f for f in h.get("videos", {}).values() if f.get("url") and f.get("width") and f.get("height")]
        if not files:
            continue
        good = [f for f in files if min(f["width"], f["height"]) >= 1080]
        best = min(good, key=lambda f: f["width"] * f["height"]) if good else \
            max(files, key=lambda f: f["width"] * f["height"])
        # Порядок: нужная ориентация, потом качество от 1080p, потом короткие (меньше качать).
        rank = ((best["height"] > best["width"]) != portrait, not good, h.get("duration") or 99)
        candidates.append((rank, {"url": best["url"], "ext": ".mp4",
                                  "credit": f"Video: {h.get('user', '?')} (Pixabay)"}))
    out = [c for _, c in sorted(candidates, key=lambda c: c[0])][:n]
    if len(out) >= n:
        return out
    data = _get_json(f"https://pixabay.com/api/?key={key}&q={q}&image_type=photo&safesearch=true"
                     f"&orientation={'vertical' if portrait else 'horizontal'}&per_page={max(3, n)}")
    out += [{"url": h["largeImageURL"], "ext": ".jpg", "credit": f"Foto: {h.get('user', '?')} (Pixabay)"}
            for h in data.get("hits", []) if h.get("largeImageURL")]
    return out[:n]


def _relevant(result, query):
    """Openverse ищет широко и легко подсовывает посторонних людей. Берём картинку, только если
    главное (самое длинное) слово запроса и хотя бы половина остальных есть в названии или тегах."""
    hay = " ".join([result.get("title") or ""] + [t.get("name", "") for t in result.get("tags") or []]).lower()
    words = [w for w in re.findall(r"[a-zäöüß]+", query.lower()) if len(w) > 2]
    if not words:
        return True
    hit = [w[:max(4, len(w) - 3)] in hay for w in words]  # грубая основа слова: strawberries ~ strawberry
    return hit[words.index(max(words, key=len))] and sum(hit) * 2 >= len(words)


def _search_openverse(query, orientation, n):
    q = urllib.parse.quote(query)
    aspect = "tall" if orientation == "portrait" else "wide"
    base = (f"https://api.openverse.org/v1/images/?q={q}&license_type=commercial,modification"
            f"&category=photograph&size=large&mature=false&page_size={n * 5}")
    # Flickr отдаёт файлы надёжнее всех; вертикальных фото мало, поэтому дальше берём любые,
    # кроме источников, которые запрещают скачивание (StockSnap отвечает 403).
    found = []
    for extra in (f"&source=flickr&aspect_ratio={aspect}", "&source=flickr", "&excluded_source=stocksnap"):
        found = [r for r in _get_json(base + extra).get("results", []) if _relevant(r, query)]
        if len(found) >= n:
            break
    return [{"url": r["url"], "ext": Path(urllib.parse.urlparse(r["url"]).path).suffix.lower() or ".jpg",
             "credit": f"\"{r.get('title') or 'Image'}\" by {r.get('creator') or 'unknown'}, "
                       f"{r['license'].upper()} {r.get('license_version') or ''} ({r.get('foreign_landing_url')})"}
            for r in found[:n]]


def fetch_media(query, orientation, n):
    """Файл/папка из assets/media или поиск: Pexels/Pixabay (если есть ключ) -> Openverse. Возвращает [(путь, автор)]."""
    local = ROOT / "assets" / "media" / query
    if local.is_file():
        return [(local, None)]
    if local.is_dir():
        return [(p, None) for p in sorted(local.iterdir()) if p.suffix.lower() in MEDIA_EXT]

    provider = stock_provider() or "openverse"
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40]
    folder = CACHE / "media" / f"{slug}-{orientation}-{provider}-{hashlib.md5(query.encode()).hexdigest()[:6]}"
    meta = folder / "meta.json"
    if meta.exists():
        items = json.loads(meta.read_text())
    else:
        items = []
        try:
            if provider == "pexels":
                items = _search_pexels(query, orientation, n)
            elif provider == "pixabay":
                items = _search_pixabay(query, orientation, n)
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
                frame_size(dest)  # битый файл отсеиваем сразу
                ok.append({**it, "file": dest.name})
            except BaseException:
                dest.unlink(missing_ok=True)
        if len(ok) < len(items[:n]):
            print(f"  · '{query}': скачано {len(ok)} из {len(items[:n])} (остальные источники недоступны)")
        items = ok
        if items:
            meta.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    return [(folder / it["file"], it["credit"]) for it in items]


def stock_provider():
    """Сток с ключом: Pexels или Pixabay. None — только бесплатный Openverse без ключа."""
    if os.environ.get("PEXELS_API_KEY"):
        return "pexels"
    if os.environ.get("PIXABAY_API_KEY"):
        return "pixabay"
    return None


def resolve_query(q, cfg):
    """@shocked -> поисковый запрос реакции из config.yaml.

    Без ключа стока реакции пропускаются (None): в Openverse по таким запросам
    находится что попало, лучше показать картинки по теме фразы."""
    if q.startswith("@"):
        if not stock_provider():
            return None
        return cfg.get("reactions", {}).get(q[1:], f"{q[1:]} face reaction")
    return q


# ---------- Эмодзи и звуки ----------

def emoji_file(emoji, animated=True):
    """Анимированный (GIF) или статичный (PNG) эмодзи Noto. None, если такого нет."""
    codes = [f"{ord(c):x}" for c in emoji]
    variants = ["_".join(c for c in codes if c != "fe0f"), "_".join(codes)]
    exts = ["gif", "png"] if animated else ["png"]
    folder = CACHE / "emoji"
    folder.mkdir(parents=True, exist_ok=True)
    for ext in exts:
        for code in dict.fromkeys(variants):
            dest = folder / f"{code}.{ext}"
            missing = folder / f"{code}.{ext}.missing"
            if dest.exists():
                return dest
            if missing.exists():
                continue
            try:
                _download(EMOJI_URL.format(code=code, ext=ext), dest)
                return dest
            except Exception:
                dest.unlink(missing_ok=True)
                missing.touch()
    print(f"  ! эмодзи {emoji} не найден")
    return None


SFX = {  # синтез звуков ffmpeg'ом: свои файлы в assets/sfx/<имя>.mp3|.wav имеют приоритет
    "whoosh": ("anoisesrc=d=0.45:c=pink:a=0.8",
               "highpass=f=300,lowpass=f=4000,afade=t=in:d=0.3:curve=exp,afade=t=out:st=0.3:d=0.15"),
    "pop": ("aevalsrc='0.8*sin(2*PI*(500+2500*t)*t)*exp(-30*t)':d=0.15:s=48000", "anull"),
    "boom": ("aevalsrc='0.9*sin(2*PI*(70-25*t)*t)*exp(-3.5*t)+0.25*(random(0)-0.5)*exp(-12*t)':d=1.3:s=48000",
             "lowpass=f=600"),
    "ding": ("aevalsrc='0.45*sin(2*PI*1568*t)*exp(-4*t)+0.25*sin(2*PI*2352*t)*exp(-6*t)':d=1.2:s=48000", "anull"),
    "scratch": ("anoisesrc=d=0.5:c=brown:a=0.9",
                "bandpass=f=1400:w=900,tremolo=f=18:d=0.9,afade=t=out:st=0.3:d=0.2"),
}


def sfx_file(name):
    for ext in (".mp3", ".wav", ".m4a"):
        own = ROOT / "assets" / "sfx" / f"{name}{ext}"
        if own.exists():
            return own
    if name not in SFX:
        print(f"  ! звук '{name}' не найден (есть: {', '.join(SFX)})")
        return None
    dest = CACHE / "sfx" / f"{name}.wav"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        src, af = SFX[name]
        raw = dest.with_suffix(".raw.wav")
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src, "-af", af, "-ar", "48000", "-ac", "2", str(raw)])
        peak = re.search(r"max_volume: (-?[\d.]+) dB",
                         subprocess.run([ff_bins()[0], "-i", str(raw), "-af", "volumedetect", "-f", "null", "-"],
                                        capture_output=True, text=True).stderr)
        gain = -3.0 - float(peak.group(1)) if peak else 0.0  # пик на -3 dB
        run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af", f"volume={gain:.1f}dB", str(dest)])
        raw.unlink()
    return dest


# ---------- Монтаж ----------

def _fill(src_w, src_h, W, H, label_in, label_out):
    """Заполнить кадр W×H: обрезкой, а если ориентация не совпадает — фото целиком поверх размытого фона."""
    if (src_w > src_h) == (W > H) or abs(src_w / src_h - W / H) < 0.25:
        return (f"[{label_in}]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1[{label_out}]")
    return (f"[{label_in}]split[fa][fb];"
            f"[fa]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=12:2,eq=brightness=-0.1,scale={W}:{H},setsar=1[fbg];"
            f"[fb]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[ffg];"
            f"[fbg][ffg]overlay=(W-w)/2:(H-h)/2[{label_out}]")


def render_cut(job):
    """Одна склейка: заполнение, «удар» зумом, дрейф, + тряска/глитч/вспышка/эмодзи на начале фразы."""
    W, H, fps, frames, out = job["W"], job["H"], job["fps"], job["frames"], job["out"]
    src = job["src"]
    inputs, graph = [], []
    if src is None:
        colors = ["0x1d3557", "0x6a040f", "0x14213d", "0x3a0ca3"]
        inputs += ["-f", "lavfi", "-i", f"color=c={colors[job['variant'] % 4]}:s={W}x{H}:r={fps}"]
        graph.append("[0:v]null[base]")
    elif src.suffix.lower() in VIDEO_EXT:
        sw, sh = frame_size(src)
        length = duration(src)
        offset = (job["variant"] * 2.3) % max(0.1, length - frames / fps) if length > frames / fps + 0.5 else 0
        inputs += ["-ss", f"{offset:.2f}", "-stream_loop", "-1", "-i", str(src)]
        graph.append(_fill(sw, sh, W, H, "0:v", "f0"))
        graph.append(f"[f0]fps={fps},scale=w='{W}*(1+0.18*max(0,1-t/0.18))':h=-2:eval=frame,"
                     f"crop={W}:{H},setsar=1[base]")
    else:
        sw, sh = frame_size(src)
        inputs += ["-i", str(src)]
        # Кадр собирается в 2x размере (меньше дрожания при зуме), потом zoompan.
        graph.append(_fill(sw, sh, W * 2, H * 2, "0:v", "f0"))
        drift = "1.0+0.0016*(on-5)" if job["variant"] % 2 == 0 else "1.12-0.0016*(on-5)"
        graph.append(f"[f0]zoompan=z='if(lt(on,5),1.28-0.04*on,{drift})':d={frames}:s={W}x{H}:fps={fps}:"
                     f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',setsar=1[base]")

    fx = []
    if job.get("shake"):
        fx.append(f"crop=w=iw-60:h=ih-60:x='30+if(lt(t,0.5),28*sin(t*90),0)':"
                  f"y='30+if(lt(t,0.5),28*cos(t*77),0)',scale={W}:{H}")
    if job.get("transition"):
        fx.append("rgbashift=rh=-16:bh=16:enable='lt(t,0.12)'")
        fx.append("fade=t=in:st=0:d=0.12:color=white")
    graph.append(f"[base]{','.join(fx) or 'null'}[fx]")

    last = "fx"
    if job.get("emoji"):
        e = job["emoji"]
        if e.suffix == ".gif":
            inputs += ["-ignore_loop", "0", "-i", str(e)]
        else:
            inputs += ["-loop", "1", "-i", str(e)]
        S, ex, ey = job["emoji_size"], job["emoji_x"], job["emoji_y"]
        pop = "if(lt(t,0.15),t/0.15*1.2,if(lt(t,0.27),1.2-(t-0.15)/0.12*0.2,1))"
        graph.append(f"[1:v]format=rgba,scale=w='max(8,{S}*{pop})':h=-1:eval=frame[emo]")
        graph.append(f"[fx][emo]overlay=x='{ex}-w/2':y='{ey}-h/2'[em]")
        last = "em"

    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph),
         "-map", f"[{last}]", "-frames:v", str(frames), "-r", str(fps), "-an", "-threads", "2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)])


# ---------- Субтитры и стикеры ----------

def ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words, segments, timeline, highlights, total, W, H, fc, style, channel, path):
    fs, y, per = fc["font_size"], int(H * fc["caption_y"]), fc.get("words_per_caption", 1)
    hl_color, hl = style["highlight_color"], {str(h).lower() for h in highlights}
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Word,{style['font']},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(6, fs // 12)},4,5,40,40,0,1",
        f"Style: Pop,{style['font']},{int(fs * 1.05)},&H00FFFFFF,&H00FFFFFF,{style['pop_color']},&H00000000,"
        f"-1,0,0,0,100,100,0,0,3,{max(10, fs // 7)},0,5,40,40,0,1",
        f"Style: Mark,{style['font']},{max(28, min(W, H) // 24)},&H8CFFFFFF,&H8CFFFFFF,&HB4000000,&H00000000,"
        f"-1,0,0,0,100,100,4,0,1,2,0,2,40,40,{int(H * 0.05)},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def is_key(t):
        return any(ch.isdigit() for ch in t) or t.lower() in hl

    # Слова группируются по `per` штук внутри предложения; текущее слово в группе подсвечивается.
    chunks = []
    for w in words:
        if (chunks and chunks[-1][0]["seg"] == w["seg"] and len(chunks[-1]) < per
                and not chunks[-1][-1].get("brk")):
            chunks[-1].append(w)
        else:
            chunks.append([w])
    k = 0
    for chunk in chunks:
        for wi, w in enumerate(chunk):
            end = words[k + 1]["start"] if k + 1 < len(words) else min(total, w["end"] + 0.4)
            k += 1
            parts = []
            for j, other in enumerate(chunk):
                t = ass_escape(other["text"])
                if (per > 1 and j == wi) or (per == 1 and is_key(other["text"])):
                    t = f"{{\\c{hl_color}}}{t}{{\\c&H00FFFFFF&}}"
                parts.append(t)
            pop = "\\fscx125\\fscy125\\t(0,90,\\fscx100\\fscy100)" if (per == 1 or wi == 0) else ""
            out.append(f"Dialogue: 2,{ass_time(w['start'])},{ass_time(end)},Word,,0,0,0,,"
                       f"{{\\an5\\pos({W // 2},{y}){pop}}}{' '.join(parts)}")

    px, py = int(W * fc["pop_x"]), int(H * fc["pop_y"])
    for seg, tl in zip(segments, timeline):
        if seg.get("pop"):
            ang = random.Random(str(seg["pop"])).choice([-6, -4, 4, 6])
            end = tl["start"] + min(1.5, tl["dur"])
            out.append(f"Dialogue: 3,{ass_time(tl['start'])},{ass_time(end)},Pop,,0,0,0,,"
                       f"{{\\an5\\pos({px},{py})\\frz{ang}\\fscx40\\fscy40\\t(0,120,\\fscx110\\fscy110)"
                       f"\\t(120,200,\\fscx100\\fscy100)}}{ass_escape(str(seg['pop']))}")
    out.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Mark,,0,0,0,,{ass_escape(channel)}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------- Обложка ----------

def make_thumbnail(spec, media_path, out):
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1280, 720
    canvas = Image.new("RGB", (W, H), (20, 20, 30))
    if media_path:
        src = media_path
        if src.suffix.lower() in VIDEO_EXT:
            src = out.with_suffix(".frame.png")
            run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(media_path), "-frames:v", "1", str(src)])
        img = Image.open(src).convert("RGB")
        scale = max(W / img.width, H / img.height)
        img = img.resize((math.ceil(img.width * scale), math.ceil(img.height * scale)))
        canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
        if src != media_path:
            src.unlink()
    # Затемнение слева направо, чтобы текст читался на любой картинке.
    shade = Image.new("L", (W, 1))
    shade.putdata([int(210 * max(0, 1 - x / (W * 0.75))) for x in range(W)])
    canvas.paste((0, 0, 0), (0, 0), shade.resize((W, H)))

    text = str(spec.get("text", "")).upper()
    cyr = any("Ѐ" <= c <= "ӿ" for c in text)
    font_path = FONTS / ("Montserrat-Black-cyrillic.ttf" if cyr else "Montserrat-Black-latin.ttf")
    lines = []
    for w in text.split():  # не больше 3 строк
        if lines and len(lines[-1] + " " + w) <= 12:
            lines[-1] += " " + w
        else:
            lines.append(w)
    lines = lines[:3] or [""]
    draw = ImageDraw.Draw(canvas)
    size = 150
    while size > 50:
        font = ImageFont.truetype(str(font_path), size)
        if (max(draw.textlength(line, font=font) for line in lines) <= W * 0.62
                and size * 1.08 * len(lines) <= H * 0.8):
            break
        size -= 6
    y = (H - size * 1.08 * len(lines)) / 2
    for i, line in enumerate(lines):
        draw.text((60, y), line, font=font, fill=(255, 229, 0) if i == 0 else (255, 255, 255),
                  stroke_width=max(6, size // 12), stroke_fill=(0, 0, 0))
        y += size * 1.08
    if spec.get("emoji") and (e := emoji_file(spec["emoji"], animated=False)):
        em = Image.open(e).convert("RGBA").resize((330, 330))
        canvas.paste(em, (W - 380, H - 400), em)
    canvas.save(out, quality=92)


# ---------- Сборка ----------

def build(script, name, fmt, cfg):
    lang = script.get("lang", "de")
    fc, style = cfg["formats"][fmt], cfg["style"]
    W, H, fps = fc["width"], fc["height"], style["fps"]
    orientation = "portrait" if H > W else "landscape"
    vcfg = {**cfg["voices"][lang], **script.get("voice", {})}
    segments = script["segments"]

    folder = OUTPUT / name
    work = folder / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    print(f"\n=== {name} ===\n[1/5] Озвучка ({vcfg['voice']}) — {len(segments)} фраз")
    timeline, words = synthesize(segments, vcfg, work)
    total = timeline[-1]["start"] + timeline[-1]["dur"]

    print("[2/5] Подбор картинок и видео")
    queries = [q for q in dict.fromkeys(resolve_query(q, cfg) for s in segments for q in s.get("visuals", [])) if q]
    thumb = script.get("thumbnail") if fmt == "long" else None
    if thumb and thumb.get("visual") and resolve_query(thumb["visual"], cfg):
        queries.append(resolve_query(thumb["visual"], cfg))
    with cf.ThreadPoolExecutor(3) as ex:
        media = dict(zip(queries, ex.map(lambda q: fetch_media(q, orientation, 4), queries)))

    credits, cuts = set(), []
    ex_, ey_, es_ = int(W * fc["emoji_x"]), int(H * fc["emoji_y"]), int(min(W, H) * fc["emoji_size"])
    pools = [[m for q in s.get("visuals", []) if (rq := resolve_query(q, cfg)) for m in media[rq]] for s in segments]
    for i, (seg, tl) in enumerate(zip(segments, timeline)):
        # Ничего не нашлось — берём картинки ближайшей фразы, а не пустой цветной фон.
        pool = pools[i] or next((pools[j] for j in sorted(range(len(pools)), key=lambda j: abs(j - i)) if pools[j]), [])
        k = max(1, round(tl["dur"] / fc["cut_seconds"]))
        emoji = emoji_file(seg["emoji"]) if seg.get("emoji") else None
        if emoji and emoji.suffix == ".gif":
            credits.add(EMOJI_CREDIT)
        for j in range(k):
            src, credit = pool[j % len(pool)] if pool else (None, None)
            if credit:
                credits.add(credit)
            cuts.append({"src": src, "start": tl["start"] + tl["dur"] * j / k, "variant": len(cuts),
                         "transition": j == 0 and i > 0, "shake": j == 0 and seg.get("shake", False),
                         "emoji": emoji if j == 0 else None, "emoji_x": ex_, "emoji_y": ey_,
                         "emoji_size": es_, "W": W, "H": H, "fps": fps})
    # Границы склеек считаются в абсолютных кадрах, чтобы картинка не «уезжала» от голоса.
    total_frames = round(total * fps)
    for n, c in enumerate(cuts):
        nxt = round(cuts[n + 1]["start"] * fps) if n + 1 < len(cuts) else total_frames
        c["frames"] = max(1, nxt - round(c["start"] * fps))
        c["out"] = work / f"cut_{n:04d}.mp4"

    print(f"[3/5] Монтаж — {len(cuts)} склеек")
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(render_cut, cuts))
    (work / "cuts.txt").write_text("".join(f"file '{c['out'].name}'\n" for c in cuts))
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(work / "cuts.txt"),
         "-c", "copy", str(work / "video.mp4")])
    (work / "voice.txt").write_text("".join(f"file '{t['audio'].name}'\n" for t in timeline))
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(work / "voice.txt"),
         "-ar", "48000", "-ac", "2", str(work / "voice.wav")])

    print("[4/5] Субтитры, звуки, музыка")
    build_ass(words, segments, timeline, script.get("highlight", []), total, W, H, fc, style,
              cfg["channel_name"], work / "subs.ass")

    events = {}  # звук -> моменты: «вжух» на смене фразы, «поп» под эмодзи/стикер, свой звук фразы
    for i, (seg, tl) in enumerate(zip(segments, timeline)):
        if seg.get("sfx") == "none":
            continue
        names = [seg["sfx"]] if seg.get("sfx") else []
        if i > 0:
            names.append("whoosh")
        if seg.get("emoji") or seg.get("pop"):
            names.append("pop")
        for s in dict.fromkeys(names):
            events.setdefault(s, []).append(max(0.0, tl["start"] - (0.18 if s == "whoosh" else 0)))

    inputs = ["-i", str(work / "video.mp4"), "-i", str(work / "voice.wav")]
    music = sorted(p for p in (ROOT / "assets" / "music").iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a"})
    af = ["[1:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[voice][vsc]"]
    mix = ["[voice]"]
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(random.choice(music))]
        # Музыка автоматически тише, пока звучит голос (sidechain).
        af.append(f"[2:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={style['music_volume']}[mraw];"
                  "[mraw][vsc]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=400[music]")
        mix.append("[music]")
    else:
        af.append("[vsc]anullsink")
    for s, times in events.items():
        f = sfx_file(s)
        if not f:
            continue
        idx = len(inputs) // 2
        inputs += ["-i", str(f)]
        vol = style["sfx_volume"] * (0.6 if s == "pop" else 1.0)
        labels = [f"{s}{j}" for j in range(len(times))]
        af.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={vol},"
                  f"asplit={len(times)}" + "".join(f"[{lb}r]" for lb in labels))
        for lb, t0 in zip(labels, times):
            ms = int(t0 * 1000)
            af.append(f"[{lb}r]adelay={ms}|{ms}[{lb}]")
            mix.append(f"[{lb}]")
    # Громкость под стандарт YouTube (-14 LUFS).
    af.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0,"
              "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")

    print("[5/5] Финальный рендер")
    final = folder / f"{name}.mp4"
    subs = str(work / "subs.ass").replace("\\", "/").replace(":", "\\:")
    fonts = str(FONTS).replace("\\", "/").replace(":", "\\:")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex",
         f"[0:v]tpad=stop_mode=clone:stop_duration=1,ass='{subs}':fontsdir='{fonts}'[vout];" + ";".join(af),
         "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
         "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)])

    if thumb:
        tq = resolve_query(thumb["visual"], cfg) if thumb.get("visual") else None
        tmedia = media.get(tq) if tq else None
        make_thumbnail(thumb, tmedia[0][0] if tmedia else None, folder / "thumbnail.jpg")
        if tmedia and tmedia[0][1]:
            credits.add(tmedia[0][1])

    tags = script.get("hashtags", [])
    if fmt == "short" and "shorts" not in tags:
        tags = tags + ["shorts"]
    desc = [script.get("description", "").strip(), ""]
    chapters = [(tl["start"], s["chapter"]) for s, tl in zip(segments, timeline) if s.get("chapter")]
    if fmt == "long" and chapters:
        if chapters[0][0] > 0:
            chapters.insert(0, (0.0, "Intro"))
        desc += ["Kapitel:"] + [f"{int(t // 60)}:{int(t % 60):02d} {c}" for t, c in chapters] + [""]
    if script.get("sources"):
        desc += ["Quellen:"] + [f"- {s}" for s in script["sources"]] + [""]
    if credits:
        desc += ["Bildnachweise:"] + [f"- {c}" for c in sorted(credits)] + [""]
    desc += [" ".join(f"#{t}" for t in tags)]
    (folder / "upload.txt").write_text(
        f"TITLE:\n{script.get('title', '')}\n\nDESCRIPTION:\n" + "\n".join(desc) +
        f"\n\nTAGS:\n{', '.join(map(str, script.get('tags', [])))}\n", encoding="utf-8")

    shutil.rmtree(work)
    print(f"Готово: {final}  ({total:.1f} сек)")
    return final


def build_script(path, fmt_override, with_shorts):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    script = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    lang = script.get("lang", "de")
    fmt = fmt_override or script.get("format", "short")
    stem = Path(path).stem
    build(script, f"{stem}-{lang}-{fmt}", fmt, cfg)

    # Shorts из длинного сценария: те же фразы, но вертикально и отдельным роликом.
    # Отрезок задаётся главой (chapter: "...") или номерами фраз (from/to, с единицы).
    if with_shorts and fmt == "long":
        segs = script["segments"]
        for n, sh in enumerate(script.get("shorts", []), 1):
            if sh.get("chapter"):
                starts = [i for i, s in enumerate(segs) if s.get("chapter")]
                a = next(i for i in starts if segs[i]["chapter"] == sh["chapter"])
                b = next((i for i in starts if i > a), len(segs))
            else:
                a, b = sh["from"] - 1, sh["to"]
            part = [dict(s) for s in segs[a:b]]
            part[0].pop("pop", None)  # номер факта из длинного видео в Short не нужен
            if script.get("shorts_outro"):
                part.append({"visuals": part[-1].get("visuals", []), **script["shorts_outro"]})
            sub = {**script, **{k: v for k, v in sh.items() if k not in ("from", "to", "chapter")},
                   "segments": part}
            build(sub, f"{stem}-{lang}-short{n:02d}", "short", cfg)


def voice_samples(lang):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    text = cfg.get("sample_text", {}).get(lang, "Wusstest du, dass ein Oktopus drei Herzen hat?")
    voices = [v for v in asyncio.run(edge_tts.list_voices()) if v["Locale"].split("-")[0] == lang]
    folder = OUTPUT / "voice-samples" / lang
    folder.mkdir(parents=True, exist_ok=True)
    rate = cfg["voices"].get(lang, {}).get("rate", "+0%")
    for v in voices:
        tts(text, {"voice": v["ShortName"], "rate": rate}, folder / f"{v['ShortName']}.mp3")
        print(f"  {v['ShortName']:40s} {v['Gender']}")
    print(f"Образцы: {folder}\nВыбранный голос впишите в config.yaml -> voices -> {lang}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Сценарий YAML -> готовое видео")
    ap.add_argument("script", nargs="*", help="путь к сценарию(ям) .yaml")
    ap.add_argument("--format", choices=["short", "long"], help="переопределить формат из сценария")
    ap.add_argument("--no-shorts", action="store_true", help="не нарезать Shorts из длинного видео")
    ap.add_argument("--voice-samples", metavar="LANG", help="сделать образцы всех голосов языка (de, en, ru, es)")
    a = ap.parse_args()
    if a.voice_samples:
        voice_samples(a.voice_samples)
    elif not a.script:
        ap.print_help()
    for s in a.script:
        build_script(s, a.format, not a.no_shorts)
