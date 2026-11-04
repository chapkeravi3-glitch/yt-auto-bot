# -*- coding: utf-8 -*-
"""
Auto Shorts maker v2
Gemini (स्क्रिप्ट) -> edge-tts (हिंदी आवाज़) -> असली वीडियो क्लिप + शब्द-दर-शब्द कैप्शन (ffmpeg)
-> YouTube upload.  GitHub Actions पर बिना स्क्रीन के चलता है।

बैकग्राउंड का क्रम (हर सीन के लिए):
  1) Pixabay वीडियो क्लिप  (अगर PIXABAY_API_KEY है)
  2) रिपॉज़िटरी के clips/ फ़ोल्डर की अपनी वीडियो क्लिप (.mp4)
  3) फ़ोटो (Pexels / Pixabay फ़ोटो / AI फ़ोटो)
  4) रंगीन बैकग्राउंड
बैकग्राउंड म्यूज़िक: रिपॉज़िटरी के music/ फ़ोल्डर में रखी फ़ाइल (.mp3), अपने-आप धीमी आवाज़ में चलेगी।
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

एक नए शॉर्ट वीडियो (लगभग 35-50 सेकंड) की स्क्रिप्ट बनाओ।
नियम:
- भाषा: सरल बोलचाल की हिंदी (देवनागरी)।
- सिर्फ़ ऐसे फ़ैक्ट्स जो पक्के, जाँचे हुए और सही हों। अंदाज़े, अफ़वाह, विवाद, किसी की निजी ज़िंदगी या बदनामी वाली बातें नहीं। जो बात पक्की न हो, उसे मत लिखो।
- पहला सीन छोटा (6-10 शब्द) और ज़बरदस्त हुक हो: चौंकाने वाली बात या सवाल, ताकि पहले 2 सेकंड में ही दर्शक रुक जाए।
- कुल {n} सीन। बाकी हर सीन का text 8 से 16 शब्द का, बोलने में सहज, छोटे वाक्य। तेज़ रफ़्तार रखो।
- कहानी की तरह आगे बढ़ाओ: हुक, फिर मज़ेदार तथ्य, और आख़िर में एक चौंकाने वाला मोड़।
- आख़िरी सीन में छोटी सी अपील: ऐसे और फ़ैक्ट्स के लिए चैनल सब्सक्राइब करें।
- हर सीन के लिए image_query अंग्रेज़ी में 2-3 शब्द, ऐसी चलती फ़ुटेज के लिए जो स्टॉक वीडियो साइट पर मिल जाए (जैसे "cinema theatre", "film camera", "city night", "crowd applause"), किसी असली व्यक्ति, फ़िल्म या ब्रांड का नाम नहीं।
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
    n = int(CFG.get("scenes", 8))
    recent = "\n".join("- " + h["title"] for h in hist[-40:]) or "(अभी कोई नहीं)"
    data = gemini_json(PROMPT.format(niche=CFG["niche"], n=n, recent=recent))
    scenes = [s for s in data.get("scenes", []) if str(s.get("text", "")).strip()]
    if len(scenes) < 4:
        raise RuntimeError("स्क्रिप्ट में सीन बहुत कम हैं")
    data["scenes"] = scenes[:12]
    return data


# ---------------------------------------------------------------- voice
async def _edge(text, out):
    import edge_tts

    voice = CFG.get("voice", "hi-IN-MadhurNeural")
    rate = CFG.get("rate", "+0%")
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    except TypeError:  # पुराना संस्करण
        comm = edge_tts.Communicate(text, voice, rate=rate)
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
    """आवाज़ बनाता है और (अगर मिले तो) हर शब्द का समय लौटाता है।"""
    for _ in range(3):
        try:
            words = asyncio.run(_edge(text, out))
            if pathlib.Path(out).stat().st_size > 2000:
                return words
        except Exception as e:
            print("edge-tts issue:", e)
            time.sleep(3)
    print("edge-tts नहीं चला, gTTS इस्तेमाल हो रहा है")
    from gtts import gTTS

    gTTS(text, lang="hi").save(str(out))
    return []


def duration(path):
    o = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)]
    )
    return float(o.strip())


def word_times(text, d, boundaries):
    """हर शब्द के (शुरू, ख़त्म, शब्द)। असली समय न मिले तो शब्द की लंबाई से अंदाज़ा।"""
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


# ---------------------------------------------------------------- fonts / text
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


def caption_images(words, idx):
    """3-3 शब्दों के समूह, जिनमें बोला जा रहा शब्द पीला चमकता है। (png, शुरू, ख़त्म) की सूची।"""
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
            y = int(H * 0.60)
            for k, wd in zip(chunk, widths):
                color = (255, 221, 0, 255) if k == active else (255, 255, 255, 255)
                d.text((x, y), words[k][2], font=font, fill=color,
                       stroke_width=8, stroke_fill=(0, 0, 0, 255))
                x += wd + gap
            path = WORK / f"cap{idx}_{active}.png"
            img.save(path)
            result.append((path, words[active][0], words[active][1]))
    return result


def watermark_image():
    mark = CFG.get("watermark", "")
    if not mark:
        return None
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = load_font(46)
    w = d.textlength(mark, font=f)
    d.text(((W - w) / 2, 130), mark, font=f, fill=(255, 215, 0, 255),
           stroke_width=3, stroke_fill=(0, 0, 0, 255))
    path = WORK / "mark.png"
    img.save(path)
    return path


# ---------------------------------------------------------------- backgrounds
def _img_from_bytes(content):
    img = Image.open(io.BytesIO(content))
    img.load()
    return img.convert("RGB")


def pixabay_video(query):
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not query:
        return None
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": key, "q": query[:90], "per_page": 20, "safesearch": "true"},
            timeout=30,
        )
        hits = r.json().get("hits", [])
        random.shuffle(hits)
        for h in hits[:6]:
            vids = h.get("videos", {})
            url = None
            for size in ("large", "medium", "small"):
                v = vids.get(size) or {}
                if v.get("url") and (v.get("size") or 0) < 45_000_000:
                    url = v["url"]
                    break
            if not url:
                continue
            data = requests.get(url, timeout=180).content
            if len(data) > 50_000:
                p = WORK / f"clip_{random.randint(1, 10**9)}.mp4"
                p.write_bytes(data)
                return p
    except Exception as e:
        print("pixabay video issue:", e)
    return None


def local_clip():
    files = []
    for ext in ("mp4", "mov", "webm", "mkv"):
        files += glob.glob(str(ROOT / "clips" / f"*.{ext}"))
    return pathlib.Path(random.choice(files)) if files else None


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


def pixabay_photo(query):
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
        print("pixabay photo issue:", e)
        return None


def ai_bg(query):
    """बिना key वाली AI फ़ोटो (Pollinations). न चले तो None लौटता है।"""
    if not query:
        return None
    prompt = (f"{query}, cinematic, dramatic lighting, vertical poster style, "
              "no text, no letters, no logos, no real people")
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
    for _ in range(2):
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


def gradient_bg():
    c1, c2 = random.choice(PALETTES)
    g = Image.linear_gradient("L").resize((W, H))
    return ImageOps.colorize(g, c1, c2).convert("RGB")


def make_bg_image(query, out):
    img = None
    for fn in (pexels_bg, pixabay_photo, ai_bg):
        img = fn(query)
        if img is not None:
            print("  फ़ोटो मिली:", fn.__name__, "|", query)
            break
    if img is None:
        print("  फ़ोटो नहीं मिली, रंगीन बैकग्राउंड:", query)
        img = gradient_bg()
    base = ImageOps.fit(img, (W, H))
    base = Image.blend(base, Image.new("RGB", (W, H), (0, 0, 0)), 0.45)
    base.save(out, quality=92)


# ---------------------------------------------------------------- video
def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2500:])
        raise RuntimeError("ffmpeg में दिक्कत आई")


def scene_video(kind, src, audio, out, d, caps, mark):
    total = d + 0.35
    inputs = []
    if kind == "video":
        try:
            cd = duration(src)
        except Exception:
            cd = 0
        start = random.uniform(0, cd - total - 0.5) if cd > total + 1 else 0
        inputs += ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", str(src)]
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,fps={FPS},"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.30:t=fill,format=yuv420p[bg]")
    else:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(src)]
        base = (f"[0:v]scale={W * 3 // 2}:{H * 3 // 2},"
                f"zoompan=z='min(1+0.0006*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:s={W}x{H}:fps={FPS},format=yuv420p[bg]")
    inputs += ["-i", str(audio)]
    for c, _, _ in caps:
        inputs += ["-i", str(c)]
    if mark:
        inputs += ["-i", str(mark)]

    fc = [base]
    cur = "bg"
    for i, (_, t0, t1) in enumerate(caps):
        nxt = f"v{i}"
        fc.append(f"[{cur}][{2 + i}:v]overlay=0:0:enable='between(t,{t0:.2f},{t1:.2f})'[{nxt}]")
        cur = nxt
    if mark:
        fc.append(f"[{cur}][{2 + len(caps)}:v]overlay=0:0,format=yuv420p[vout]")
    else:
        fc.append(f"[{cur}]format=yuv420p[vout]")

    run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", "1:a",
        "-af", "apad=pad_dur=0.35,aresample=44100",
        "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", str(out),
    ])


def pick_music():
    files = []
    for ext in ("mp3", "m4a", "wav", "ogg"):
        files += glob.glob(str(ROOT / "music" / f"*.{ext}"))
    return pathlib.Path(random.choice(files)) if files else None


def build_video(script):
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir()
    if not features.check("raqm"):
        print("चेतावनी: raqm नहीं मिला, हिंदी मात्राएँ थोड़ी बिगड़ सकती हैं")
    mark = watermark_image()
    clips = []
    for i, sc in enumerate(script["scenes"]):
        text = str(sc["text"]).strip()
        query = sc.get("image_query", "")
        a = WORK / f"a{i}.mp3"
        boundaries = tts(text, a)
        d = duration(a)
        words = word_times(text, d, boundaries)
        caps = caption_images(words, i)

        clip = pixabay_video(query) or local_clip()
        if clip:
            kind, src = "video", clip
            print(f"  वीडियो क्लिप मिली | {query}")
        else:
            kind, src = "image", WORK / f"bg{i}.jpg"
            make_bg_image(query, src)

        v = WORK / f"v{i}.mp4"
        scene_video(kind, src, a, v, d, caps, mark)
        clips.append(v)
        print(f"सीन {i + 1}/{len(script['scenes'])} तैयार ({d:.1f}s, {kind})")

    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    final = WORK / "final.mp4"
    music = pick_music()
    if music:
        vol = float(CFG.get("music_volume", 0.12))
        print("म्यूज़िक:", music.name)
        run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            f"[1:a]volume={vol}[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac",
            "-b:a", "128k", "-movflags", "+faststart", str(final),
        ])
    else:
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
