# -*- coding: utf-8 -*-
"""
Auto Shorts maker
Gemini (स्क्रिप्ट) -> edge-tts (हिंदी आवाज़) -> Pillow+ffmpeg (वीडियो) -> YouTube upload
GitHub Actions पर बिना स्क्रीन के चलता है।
"""
import asyncio
import datetime
import glob
import io
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import time
import urllib.parse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, features

ROOT = pathlib.Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
WORK = ROOT / "work"
HIST = ROOT / "state" / "history.json"
W, H = 1080, 1920
FPS = 25

PALETTES = [
    ((20, 20, 60), (120, 30, 90)),
    ((10, 40, 60), (20, 120, 110)),
    ((60, 20, 20), (160, 80, 20)),
    ((25, 25, 25), (80, 60, 140)),
    ((5, 30, 20), (40, 110, 60)),
]

PROMPT = """तुम एक यूट्यूब शॉर्ट्स स्क्रिप्ट राइटर हो। चैनल का विषय: {niche}

एक नए शॉर्ट वीडियो (लगभग 40-55 सेकंड) की स्क्रिप्ट बनाओ।
नियम:
- भाषा: सरल बोलचाल की हिंदी (देवनागरी)।
- सिर्फ़ ऐसे फ़ैक्ट्स जो पक्के, जाँचे हुए और सही हों। अंदाज़े, अफ़वाह, विवाद, किसी की निजी ज़िंदगी या बदनामी वाली बातें नहीं। जो बात पक्की न हो, उसे मत लिखो।
- पहला सीन ऐसा हुक हो कि लोग रुककर देखें (सवाल या चौंकाने वाली बात)।
- कुल {n} सीन। हर सीन का text 12 से 22 शब्द का, बोलने में सहज।
- आख़िरी सीन में छोटी सी अपील: ऐसे और फ़ैक्ट्स के लिए चैनल सब्सक्राइब करें।
- हर सीन के लिए image_query अंग्रेज़ी में 2-3 शब्द (फोटो खोजने के लिए, जैसे "cinema theatre", "film camera"), किसी असली व्यक्ति या ब्रांड का नाम नहीं।
- ये टॉपिक/टाइटल पहले बन चुके हैं, इनसे अलग नया विषय चुनो:
{recent}

सिर्फ़ यह JSON लौटाओ:
{{"title": "...", "description": "...", "tags": ["..."], "scenes": [{{"text": "...", "image_query": "..."}}]}}
title हिंदी में, 70 अक्षरों तक, आकर्षक। description 2-3 लाइन। tags 8-12 (हिंदी और अंग्रेज़ी मिलाकर)।"""


# ---------------------------------------------------------------- history
def load_hist():
    try:
        return json.loads(HIST.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_hist(items):
    HIST.parent.mkdir(exist_ok=True)
    HIST.write_text(json.dumps(items[-300:], ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- gemini
def gemini_json(prompt):
    key = os.environ["GEMINI_API_KEY"]
    models = CFG.get("gemini_models", ["gemini-2.5-flash", "gemini-flash-latest"])
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.0, "responseMimeType": "application/json"},
    }
    last = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(5):
            try:
                r = requests.post(
                    url,
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body,
                    timeout=120,
                )
            except Exception as e:
                last = str(e)
                time.sleep(5)
                continue
            if r.status_code == 200:
                try:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
                    return json.loads(txt)
                except Exception as e:
                    last = f"parse error: {e}"
                    continue
            last = f"{model} {r.status_code}: {r.text[:200]}"
            if r.status_code in (400, 404):
                break
            time.sleep(10 * (attempt + 1))
        print("Gemini issue:", last)
    raise RuntimeError("Gemini से स्क्रिप्ट नहीं मिली: " + last)


def make_script(hist):
    n = int(CFG.get("scenes", 7))
    recent = "\n".join("- " + h["title"] for h in hist[-40:]) or "(अभी कोई नहीं)"
    data = gemini_json(PROMPT.format(niche=CFG["niche"], n=n, recent=recent))
    scenes = [s for s in data.get("scenes", []) if str(s.get("text", "")).strip()]
    if len(scenes) < 4:
        raise RuntimeError("स्क्रिप्ट में सीन बहुत कम हैं")
    data["scenes"] = scenes[:10]
    return data


# ---------------------------------------------------------------- voice
async def _edge(text, out):
    import edge_tts

    comm = edge_tts.Communicate(
        text, CFG.get("voice", "hi-IN-MadhurNeural"), rate=CFG.get("rate", "+0%")
    )
    await comm.save(str(out))


def tts(text, out):
    for _ in range(3):
        try:
            asyncio.run(_edge(text, out))
            if pathlib.Path(out).stat().st_size > 2000:
                return
        except Exception as e:
            print("edge-tts issue:", e)
            time.sleep(3)
    print("edge-tts नहीं चला, gTTS इस्तेमाल हो रहा है")
    from gtts import gTTS

    gTTS(text, lang="hi").save(str(out))


def duration(path):
    o = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)]
    )
    return float(o.strip())


# ---------------------------------------------------------------- images
def find_font():
    files = []
    for p in ("/usr/share/fonts/**/NotoSansDevanagari*.ttf",
              "/usr/share/fonts/**/NotoSerifDevanagari*.ttf"):
        files += glob.glob(p, recursive=True)
    if not files:
        raise RuntimeError("हिंदी फ़ॉन्ट नहीं मिला (fonts-noto-core इंस्टॉल करें)")
    files.sort(key=lambda f: ("UI" in f, "Bold" not in f, len(f)))
    return files[0]


FONT_PATH = None


def load_font(size):
    global FONT_PATH
    if FONT_PATH is None:
        FONT_PATH = find_font()
    try:
        f = ImageFont.truetype(FONT_PATH, size, layout_engine=ImageFont.Layout.RAQM)
    except Exception:
        f = ImageFont.truetype(FONT_PATH, size)
    try:
        f.set_variation_by_name("Bold")
    except Exception:
        pass
    return f


def wrap(draw, text, font, maxw):
    lines, cur = [], ""
    for word in text.split():
        t = (cur + " " + word).strip()
        if draw.textlength(t, font=font) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _img_from_bytes(content):
    img = Image.open(io.BytesIO(content))
    img.load()
    return img.convert("RGB")


def pexels_bg(query):
    key = os.environ.get("PEXELS_API_KEY")
    if not key or not query:
        return None
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": key},
            params={"query": query, "orientation": "portrait", "per_page": 15},
            timeout=30,
        )
        photos = r.json().get("photos", [])
        if not photos:
            return None
        url = random.choice(photos)["src"]["portrait"]
        return _img_from_bytes(requests.get(url, timeout=60).content)
    except Exception as e:
        print("pexels issue:", e)
        return None


def pixabay_bg(query):
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not query:
        return None
    try:
        r = requests.get(
            "https://pixabay.com/api/",
            params={"key": key, "q": query[:90], "image_type": "photo",
                    "orientation": "vertical", "safesearch": "true", "per_page": 20},
            timeout=30,
        )
        hits = r.json().get("hits", [])
        if not hits:
            return None
        url = random.choice(hits).get("largeImageURL") or hits[0]["webformatURL"]
        return _img_from_bytes(requests.get(url, timeout=60).content)
    except Exception as e:
        print("pixabay issue:", e)
        return None


def ai_bg(query):
    """बिना key वाली AI फ़ोटो (Pollinations). न चले तो None लौटता है।"""
    if not query:
        return None
    prompt = (f"{query}, cinematic, dramatic lighting, vertical poster style, "
              "no text, no letters, no logos, no real people")
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
    for attempt in range(2):
        try:
            r = requests.get(
                url,
                params={"width": 1080, "height": 1920, "nologo": "true",
                        "seed": random.randint(1, 10**6)},
                timeout=120,
            )
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("image") and len(r.content) > 10000:
                return _img_from_bytes(r.content)
            print("ai image issue:", r.status_code, ctype)
        except Exception as e:
            print("ai image issue:", e)
        time.sleep(5)
    return None


def get_bg(query):
    for fn in (pexels_bg, pixabay_bg, ai_bg):
        img = fn(query)
        if img is not None:
            print("  फ़ोटो मिली:", fn.__name__, "|", query)
            return img
    print("  फ़ोटो नहीं मिली, रंगीन बैकग्राउंड:", query)
    return None


def gradient_bg():
    c1, c2 = random.choice(PALETTES)
    g = Image.linear_gradient("L").resize((W, H))
    return ImageOps.colorize(g, c1, c2).convert("RGB")


def make_card(text, query, out):
    bg = get_bg(query) or gradient_bg()
    base = ImageOps.fit(bg, (W, H))
    base = Image.blend(base, Image.new("RGB", (W, H), (0, 0, 0)), 0.5)
    d = ImageDraw.Draw(base)

    size = 84
    while True:
        font = load_font(size)
        lines = wrap(d, text, font, W - 160)
        if len(lines) <= 8 or size <= 46:
            break
        size -= 6
    lh = int(size * 1.6)
    y = (H - lh * len(lines)) // 2
    for ln in lines:
        w = d.textlength(ln, font=font)
        d.text(((W - w) / 2, y), ln, font=font, fill="white",
               stroke_width=5, stroke_fill="black")
        y += lh

    mark = CFG.get("watermark", "")
    if mark:
        f2 = load_font(46)
        w = d.textlength(mark, font=f2)
        d.text(((W - w) / 2, 120), mark, font=f2, fill=(255, 215, 0),
               stroke_width=3, stroke_fill="black")
    base.save(out, quality=92)


# ---------------------------------------------------------------- video
def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2000:])
        raise RuntimeError("ffmpeg में दिक्कत आई")


def scene_video(img, audio, out, d):
    total = d + 0.35
    vf = (
        f"scale={W * 3 // 2}:{H * 3 // 2},"
        f"zoompan=z='min(1+0.0006*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={W}x{H}:fps={FPS},format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(img),
        "-i", str(audio), "-vf", vf, "-af", "apad=pad_dur=0.35,aresample=44100",
        "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", str(out),
    ])


def build_video(script):
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir()
    if not features.check("raqm"):
        print("चेतावनी: raqm नहीं मिला, हिंदी मात्राएँ थोड़ी बिगड़ सकती हैं")
    clips = []
    for i, sc in enumerate(script["scenes"]):
        text = str(sc["text"]).strip()
        a = WORK / f"a{i}.mp3"
        tts(text, a)
        d = duration(a)
        img = WORK / f"c{i}.jpg"
        make_card(text, sc.get("image_query", ""), img)
        v = WORK / f"v{i}.mp4"
        scene_video(img, a, v, d)
        clips.append(v)
        print(f"सीन {i + 1}/{len(script['scenes'])} तैयार ({d:.1f}s)")
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    final = WORK / "final.mp4"
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac",
        "-b:a", "128k", "-movflags", "+faststart", str(final),
    ])
    print(f"वीडियो तैयार: {duration(final):.1f} सेकंड")
    return final


# ---------------------------------------------------------------- upload
def upload(video, title, description, tags):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    privacy = os.environ.get("PRIVACY") or CFG.get("privacy", "public")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": str(CFG.get("category_id", "24")),
            "defaultLanguage": "hi",
            "defaultAudioLanguage": "hi",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ---------------------------------------------------------------- main
def clean_tags(tags):
    out, total = [], 0
    for t in tags or []:
        t = str(t).strip().lstrip("#")
        if not t or len(t) > 40:
            continue
        if total + len(t) > 400:
            break
        out.append(t)
        total += len(t) + 1
    return out


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    hist = load_hist()
    script = make_script(hist)

    title = str(script.get("title", "रोचक फ़ैक्ट")).strip()
    if len(title) > 85:
        title = title[:85].rstrip()
    if "#shorts" not in title.lower():
        title += " #Shorts"
    hashtags = " ".join(CFG.get("hashtags", ["#Shorts"]))
    description = (
        str(script.get("description", "")).strip()
        + "\n\n" + hashtags
        + "\n\nयह वीडियो AI की मदद से बनाया गया है।"
    )
    tags = clean_tags(script.get("tags"))
    print("टाइटल:", title)

    video = build_video(script)

    if dry:
        print("DRY RUN: अपलोड नहीं किया। वीडियो:", video)
        return

    vid = upload(video, title, description, tags)
    print("अपलोड हो गया: https://youtube.com/shorts/" + vid)
    hist.append({"date": datetime.date.today().isoformat(), "title": title, "id": vid})
    save_hist(hist)


if __name__ == "__main__":
    main()
