# -*- coding: utf-8 -*-
"""
Auto Shorts maker v3 (API Key Free Version)
===========================================
  -> Gemini / Pollinations (स्क्रिप्ट के लिए)
  -> edge-tts (हिंदी आवाज़) + शब्द-दर-शब्द चमकते कैप्शन + शुरू में बड़ा हुक-टेक्स्ट
  -> बिना किसी Pexels/Pixabay key के:
       1. Wikimedia Commons (मुफ़्त-लाइसेंस वाली असली फ़ोटो)
       2. Local Clips (`clips/` फ़ोल्डर से वीडियो/क्लिप)
       3. सुंदर रंगीन ग्रेडिएंट बैकग्राउंड (आख़िरी विकल्प)
  -> YouTube upload. GitHub Actions पर बिना किसी रुकावट के चलता है।
"""
import asyncio
import datetime
import glob
import html
import io
import json
import math
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

ROOT = pathlib.Path(__file__).resolve().parent


def load_cfg():
    text = (ROOT / "config.json").read_text(encoding="utf-8").lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            obj, end = json.JSONDecoder().raw_decode(text.lstrip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        raise SystemExit(f"config.json में ग़लती है: लाइन {e.lineno}")


CFG = load_cfg()


def get_key(name):
    v = (os.environ.get(name) or "").strip()
    if not v:
        v = str(CFG.get(name.lower(), "") or "").strip()
    return v


WORK = ROOT / "work"
HIST = ROOT / "state" / "history.json"
W, H = 1080, 1920
FPS = 25
UA = {"User-Agent": "yt-auto-bot/3.0 (https://github.com/chapkeravi3-glitch/yt-auto-bot; hobby project) 
DEFAULT_MODELS = [
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash",
]


PALETTES = [
    ((30, 60, 140), (160, 50, 120)),
    ((0, 90, 120), (40, 170, 150)),
    ((140, 40, 40), (230, 130, 40)),
    ((60, 60, 130), (140, 100, 200)),
    ((20, 100, 70), (90, 180, 100)),
]

JSON_SHAPE = (
    '{"title": "...", "hook": "...", "description": "...", "tags": ["..."], '
    '"scenes": [{"text": "...", "queries": ["...", "..."]}]}'
)

COMMON_RULES = """
सख़्त नियम:
- भाषा: सरल बोलचाल की हिंदी (देवनागरी)।
- अपनी तरफ़ से कोई नई बात या अफ़वाह मत गढ़ो।
- "hook": स्क्रीन पर बड़ा लिखा जाने वाला 3-6 शब्दों का चौंकाने वाला वाक्य।
- पहले सीन का text ज़बरदस्त हुक हो (6-10 शब्द)।
- कुल {n} सीन। बाकी हर सीन 6 से 12 शब्द का।
- queries में उस विषय से जुड़े 2-3 अंग्रेज़ी शब्द लिखो (जैसे "Taj Mahal", "crowd clapping").
सिर्फ़ यह JSON लौटाओ:
""" + JSON_SHAPE.replace("{", "{{").replace("}", "}}")

PROMPT_NEWS = """तुम एक यूट्यूब शॉर्ट्स स्क्रिप्ट राइटर हो। चैनल का विषय: {niche}
आज की ख़बरें/ट्रेंड हैं:
{headlines}
इनमें से सबसे दिलचस्प कहानी चुनो और शॉर्ट वीडियो बनाओ।
""" + COMMON_RULES


class GeminiDown(Exception):
    pass


def load_hist():
    try:
        return json.loads(HIST.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_hist(items):
    HIST.parent.mkdir(exist_ok=True)
    HIST.write_text(json.dumps(items[-300:], ensure_ascii=False, indent=1), encoding="utf-8")


def summary(text):
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n\n")
        except Exception:
            pass


def _rss_items(url):
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        if title:
            out.append((title, desc))
    return out


def fetch_headlines():
    region = CFG.get("region", "IN")
    q = (CFG.get("news_query") or CFG.get("niche", "")).strip()[:60]
    lines, seen = [], set()
    try:
        url = "https://news.google.com/rss/search?q=" + requests.utils.quote(q + " when:2d") + f"&hl=hi&gl={region}&ceid={region}:hi"
        for t, d in _rss_items(url)[:10]:
            if t not in seen:
                seen.add(t)
                lines.append(f"- {t} :: {d[:120]}")
    except Exception:
        pass
    return lines[:20]


def _parse_json(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", (txt or "").strip(), flags=re.M).strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def gemini_json(prompt):
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise GeminiDown("GEMINI_API_KEY नहीं मिली")
    for model in DEFAULT_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                              json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            if r.status_code == 200:
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_json(txt)
        except Exception:
            continue
    raise GeminiDown("Gemini से स्क्रिप्ट नहीं मिली")


def make_script(hist):
    n = int(CFG.get("scenes", 12))
    recent = "\n".join("- " + h["title"] for h in hist[-30:]) or "(अभी कोई नहीं)"
    heads = fetch_headlines()
    prompt = PROMPT_NEWS.format(niche=CFG["niche"], headlines="\n".join(heads) if heads else "सामान्य रोचक तथ्य", n=n, recent=recent)
    try:
        data = gemini_json(prompt)
    except Exception as e:
        raise GeminiDown(str(e))
    scenes = []
    for s in data.get("scenes", []):
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        qs = [str(q).strip() for q in s.get("queries", []) if str(q).strip()][:3]
        scenes.append({"text": text, "queries": qs})
    if len(scenes) < 5:
        raise RuntimeError("स्क्रिप्ट में सीन बहुत कम हैं")
    data["scenes"] = scenes[:15]
    return data


async def _edge(text, out):
    import edge_tts
    voice = CFG.get("voice", "hi-IN-MadhurNeural")
    comm = edge_tts.Communicate(text, voice, rate=CFG.get("rate", "+0%"))
    words = []
    with open(out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 1e7
                words.append((s, s + chunk["duration"] / 1e7, chunk["text"]))
    return words


def tts(text, out):
    try:
        return asyncio.run(_edge(text, out))
    except Exception:
        from gtts import gTTS
        gTTS(text, lang="hi").save(str(out))
        return []


def duration(path):
    o = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)])
    return float(o.strip())


def word_times(text, d, boundaries):
    toks = text.split()
    if boundaries and len(boundaries) >= max(2, int(len(toks) * 0.6)):
        items = [(s, e, w) for s, e, w in boundaries if str(w).strip()]
    else:
        weights = [len(t) + 1 for t in toks]
        total = float(sum(weights))
        span = max(d - 0.1, 0.5)
        items, t = [], 0.05
        for tok, wt in zip(toks, weights):
            dt = span * wt / total
            items.append((t, t + dt, tok))
            t += dt
    out = []
    for i, (s, e, w) in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else d + 0.35
        out.append((s if i else 0.0, nxt, w))
    return out


def find_font():
    files = glob.glob("/usr/share/fonts/**/*.ttf", recursive=True)
    if not files:
        raise RuntimeError("हिंदी फ़ॉन्ट नहीं मिला")
    files.sort(key=lambda f: ("NotoSansDevanagari" in f, len(f)))
    return files[0]


FONT_PATH = None


def load_font(size):
    global FONT_PATH
    if FONT_PATH is None:
        FONT_PATH = find_font()
    return ImageFont.truetype(FONT_PATH, size)


def caption_images(words, idx):
    n = len(words)
    result = []
    for start in range(0, n, 3):
        chunk = list(range(start, min(start + 3, n)))
        size = 96
        while True:
            font = load_font(size)
            probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
            gap = max(int(size * 0.28), 10)
            widths = [probe.textlength(words[k][2], font=font) for k in chunk]
            total = sum(widths) + gap * (len(chunk) - 1)
            if total <= W - 110 or size <= 50:
                break
            size -= 6
        for active in chunk:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            x = (W - total) / 2
            y = int(H * 0.62)
            for k, wd in zip(chunk, widths):
                color = (255, 221, 0, 255) if k == active else (255, 255, 255, 255)
                d.text((x, y), words[k][2], font=font, fill=color, stroke_width=8, stroke_fill=(0, 0, 0, 255))
                x += wd + gap
            path = WORK / f"cap{idx}_{active}.png"
            img.save(path)
            result.append((path, words[active][0], words[active][1]))
    return result


def hook_image(text):
    text = (text or "").strip()
    if not text:
        return None
    font = load_font(100)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=font)
    d.rounded_rectangle([50, 350, W - 50, 550], radius=30, fill=(0, 0, 0, 180))
    d.text(((W - tw) / 2, 420), text, font=font, fill=(255, 221, 0, 255), stroke_width=6, stroke_fill=(0, 0, 0, 255))
    path = WORK / "hook.png"
    img.save(path)
    return path


def watermark_image():
    mark = CFG.get("watermark", "")
    if not mark:
        return None
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = load_font(46)
    w = d.textlength(mark, font=f)
    d.text(((W - w) / 2, 130), mark, font=f, fill=(255, 215, 0, 255), stroke_width=3, stroke_fill=(0, 0, 0, 255))
    path = WORK / "mark.png"
    img.save(path)
    return path


USED = set()


def _download(url, min_bytes=20_000):
    try:
        r = requests.get(url, timeout=15, headers=UA)
        if r.status_code == 200 and len(r.content) >= min_bytes:
            return r.content
    except Exception:
        pass
    return None


def commons_photo(query):
    """Wikimedia Commons से मुफ़्त और असली फ़ोटो लेना"""
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 5,
        "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 1200,
    }
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", params=params, headers=UA, timeout=15)
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            if url and url not in USED:
                data = _download(url)
                if data:
                    img = Image.open(io.BytesIO(data)).convert("RGB")
                    USED.add(url)
                    return img
    except Exception:
        pass
    return None


def local_clip():
    """आपके 'clips/' फ़ोल्डर से लोकल वीडियो क्लिप चुनना"""
    files = []
    for ext in ("mp4", "mov", "webm", "mkv"):
        files += glob.glob(str(ROOT / "clips" / f"*.{ext}"))
    return pathlib.Path(random.choice(files)) if files else None


def gradient_bg():
    c1, c2 = random.choice(PALETTES)
    g = Image.linear_gradient("L").resize((W, H))
    return ImageOps.colorize(g, c1, c2).convert("RGB")


def compose_photo(img):
    bg = ImageOps.fit(img, (W, H)).filter(ImageFilter.GaussianBlur(30))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.4)
    iw, ih = img.size
    area_h = int(H * 0.55)
    scale = min(W / iw, area_h / ih)
    fg = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)
    x = (W - fg.size[0]) // 2
    y = int(H * 0.15) + (area_h - fg.size[1]) // 2
    bg.paste(fg, (x, y))
    return bg


def get_visual(scene, idx):
    queries = scene.get("queries") or ["background"]
    
    # 1. पहले Wikimedia Commons से असली फ़ोटो की कोशिश करेंगे
    for q in queries:
        img = commons_photo(q)
        if img:
            p = WORK / f"bg{idx}.jpg"
            compose_photo(img).save(p, quality=92)
            return "image", p, 1

    # 2. दूसरा विकल्प: अगर आपके 'clips/' फ़ोल्डर में कोई वीडियो है
    clip = local_clip()
    if clip:
        return "video", clip, 1

    # 3. आख़िरी विकल्प: सुंदर रंगीन बैकग्राउंड (कभी फेल नहीं होगा)
    p = WORK / f"bg{idx}.jpg"
    gradient_bg().save(p, quality=92)
    return "image", p, 0.5


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg error")


def scene_video(kind, src, audio, out, d, overlays, mark):
    total = d + 0.35
    if kind == "video":
        inputs = ["-stream_loop", "-1", "-i", str(src)]
        base = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},format=yuv420p[bg]"
    else:
        inputs = ["-loop", "1", "-framerate", str(FPS), "-i", str(src)]
        base = f"[0:v]scale={W}*1.2:{H}*1.2,zoompan=z='min(1+0.0006*on,1.12)':d=1:s={W}x{H}:fps={FPS},format=yuv420p[bg]"

    inputs += ["-i", str(audio)]
    for c, _, _ in overlays:
        inputs += ["-i", str(c)]
    if mark:
        inputs += ["-i", str(mark)]

    fc = [base]
    cur = "bg"
    for i, (_, t0, t1) in enumerate(overlays):
        nxt = f"v{i}"
        fc.append(f"[{cur}][{2 + i}:v]overlay=0:0:enable='between(t,{t0:.2f},{t1:.2f})'[{nxt}]")
        cur = nxt
    if mark:
        fc.append(f"[{cur}][{2 + len(overlays)}:v]overlay=0:0,format=yuv420p[vout]")
    else:
        fc.append(f"[{cur}]format=yuv420p[vout]")

    run([
        "ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", "1:a", "-af", "apad=pad_dur=0.35",
        "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", str(out),
    ])


def build_video(script):
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(exist_ok=True)
    USED.clear()
    mark = watermark_image()
    hook = hook_image(script.get("hook", ""))
    clips, ok_count = [], 0
    scenes = script["scenes"]
    
    for i, sc in enumerate(scenes):
        text = sc["text"]
        a = WORK / f"a{i}.mp3"
        boundaries = tts(text, a)
        d = duration(a)
        words = word_times(text, d, boundaries)
        overlays = list(caption_images(words, i))
        if i == 0 and hook:
            overlays.append((hook, 0.1, min(2.9, d + 0.2)))

        kind, src, ok = get_visual(sc, i)
        ok_count += ok
        v = WORK / f"v{i}.mp4"
        scene_video(kind, src, a, v, d, overlays, mark)
        clips.append(v)

    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    final = WORK / "final.mp4"
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", str(final),
    ])
    return final, {"total": len(scenes), "visual": ok_count}


def upload(video, title, description, tags):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    privacy = os.environ.get("PRIVACY") or CFG.get("privacy", "private")
    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": "24", "defaultLanguage": "hi"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4")
    resp = yt.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    return resp["id"]


def main():
    print("✅ सिस्टम तैयार: बिना किसी Pexels/Pixabay Key के वीडियो निर्माण शुरू हो रहा है...")
    try:
        script = make_script(load_hist())
    except GeminiDown as e:
        summary("⚠️ वीडियो नहीं बना: " + str(e))
        return

    title = str(script.get("title", "रोचक फ़ैक्ट")).strip()[:85]
    if "#shorts" not in title.lower():
        title += " #Shorts"

    video, stats = build_video(script)
    description = str(script.get("description", "")) + "\n\n#Shorts #YouTubeShorts"
    tags = ["Shorts", "Facts"]

    if os.environ.get("DRY_RUN") == "1":
        print("DRY RUN: वीडियो बन गया है, अपलोड नहीं किया।")
        return

    vid = upload(video, title, description, tags)
    summary(f"✅ वीडियो सफलतापूर्वक अपलोड हो गया: https://youtube.com/shorts/{vid}")
    hist = load_hist()
    hist.append({"date": datetime.date.today().isoformat(), "title": title, "id": vid})
    save_hist(hist)


if __name__ == "__main__":
    main()
