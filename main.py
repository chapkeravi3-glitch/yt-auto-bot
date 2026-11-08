# -*- coding: utf-8 -*-
"""
Auto Shorts maker v3 (Error-Free Final Version)
===============================================
"""
import asyncio
import datetime
import glob
import html
import io
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = pathlib.Path(__file__).resolve().parent


def load_cfg():
    text = (ROOT / "config.json").read_text(encoding="utf-8").lstrip("\ufeff")
    try:
        return json.loads(text)
    except Exception:
        return {"niche": "फ़िल्मी फ़ैक्ट्स", "scenes": 10}


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
UA = {"User-Agent": "yt-auto-bot/3.0 (PythonRequests)"}


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

    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    last_err = ""

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={key}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"}
        }
        try:
            r = requests.post(url, json=body, timeout=60)
            if r.status_code == 200:
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_json(txt)
            else:
                last_err = f"{model} HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:
            last_err = f"{model} error: {str(e)}"
            continue

    raise GeminiDown("Gemini सभी मॉडल फेल हो गए: " + last_err)


def make_script(hist):
    n = int(CFG.get("scenes", 10))
    heads = fetch_headlines()

    prompt = f"""तुम एक यूट्यूब शॉर्ट्स स्क्रिप्ट राइटर हो। चैनल का विषय: {CFG.get('niche', 'Facts')}
आज की मुख्य ख़बरें:
{chr(10).join(heads) if heads else "रोचक सामान्य ज्ञान"}

इन पर आधारित एक आकर्षक शॉर्ट्स स्क्रिप्ट बनाओ।
सख़्त नियम: केवल शुद्ध JSON फॉर्मेट में उत्तर दो, जिसमें ये फील्ड हों:
{{
  "title": "वीडियो का टाइटल (हिंदी में, #Shorts के साथ)",
  "hook": "चौंकाने वाला हुक वाक्य",
  "description": "छोटी सी डिस्क्रिप्शन",
  "tags": ["Shorts", "Facts"],
  "scenes": [
    {{"text": "सीन का हिंदी टेक्स्ट (6-10 शब्द)", "queries": ["English search keyword 1", "keyword 2"]}}
  ]
}}
कुल {n} सीन होने चाहिए।"""

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
        scenes.append({"text": text, "queries": qs or ["cinema", "fact"]})

    if len(scenes) < 3:
        raise RuntimeError("स्क्रिप्ट में पर्याप्त सीन नहीं मिले")
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
        size = 80
        font = load_font(size)
        probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        
        # सरल और सुरक्षित कैप्शन लेआउट
        for active in chunk:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            text_str = words[active][2]
            tw = probe.textlength(text_str, font=font)
            x = (W - tw) / 2
            y = int(H * 0.65)
            
            # बैकग्राउंड बॉक्स ताकि टेक्स्ट साफ़ दिखे
            d.rounded_rectangle([x - 20, y - 10, x + tw + 20, y + size + 10], radius=15, fill=(0, 0, 0, 160))
            d.text((x, y), text_str, font=font, fill=(255, 221, 0, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255))
            
            path = WORK / f"cap{idx}_{active}.png"
            img.save(path)
            result.append((path, words[active][0], words[active][1]))
    return result


def hook_image(text):
    text = (text or "").strip()
    if not text:
        return None
    font = load_font(90)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=font)
    d.rounded_rectangle([50, 350, W - 50, 550], radius=30, fill=(0, 0, 0, 180))
    d.text(((W - tw) / 2, 420), text, font=font, fill=(255, 221, 0, 255), stroke_width=5, stroke_fill=(0, 0, 0, 255))
    path = WORK / "hook.png"
    img.save(path)
    return path


def watermark_image():
    mark = CFG.get("watermark", "")
    if not mark:
        return None
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = load_font(40)
    w = d.textlength(mark, font=f)
    d.text(((W - w) / 2, 130), mark, font=f, fill=(255, 215, 0, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
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
    files = []
    for ext in ("mp4", "mov", "webm", "mkv"):
        files += glob.glob(str(ROOT / "clips" / f"*.{ext}"))
    return pathlib.Path(random.choice(files)) if files else None


def gradient_bg():
    c1, c2 = random.choice([((30, 60, 140), (160, 50, 120)), ((0, 90, 120), (40, 170, 150))])
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
    queries = scene.get("queries") or ["cinema"]
    for q in queries:
        img = commons_photo(q)
        if img:
            p = WORK / f"bg{idx}.jpg"
            compose_photo(img).save(p, quality=92)
            return "image", p, 1

    clip = local_clip()
    if clip:
        return "video", clip, 1

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
    print("✅ सिस्टम तैयार: वीडियो निर्माण शुरू हो रहा है...")
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
    tags = script.get("tags", ["Shorts", "Facts"])

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
