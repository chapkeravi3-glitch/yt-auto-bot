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
# -*- coding: utf-8 -*-
"""
Auto Shorts maker v3
====================
आज की असली ख़बरें/ट्रेंड (Google News + Google Trends, बिना key)
  -> Gemini (एक ही कॉल: स्क्रिप्ट + हर सीन के लिए सटीक खोज-शब्द)
  -> edge-tts (हिंदी आवाज़) + शब्द-दर-शब्द चमकते कैप्शन + शुरू में बड़ा हुक-टेक्स्ट
  -> हर सीन के लिए असली फ़ुटेज/फ़ोटो:
       "photo" सीन : Wikimedia Commons (मुफ़्त-लाइसेंस वाली असली फ़ोटो) -> Pexels फ़ोटो
       "stock" सीन : Pexels वीडियो -> Pixabay वीडियो
     सिर्फ़ वही क्लिप ली जाती है जिसके शीर्षक/टैग में खोज-शब्द मिलें।
  -> गुणवत्ता की शर्त: ज़्यादातर सीन में सही फ़ुटेज न मिले तो वीडियो अपलोड नहीं होता।
  -> YouTube upload.  GitHub Actions पर बिना स्क्रीन के चलता है।

AI से बनी धुंधली फ़ोटो अब बिल्कुल इस्तेमाल नहीं होतीं।

config.json की नई वैकल्पिक चाबियाँ (न दें तो डिफ़ॉल्ट चलेंगे):
  "news_query":        ख़बरें खोजने का शब्द, जैसे "बॉलीवुड" (न दें तो niche का शुरुआती हिस्सा)
  "region":            "IN"
  "scenes":            सीन की संख्या (डिफ़ॉल्ट 12)
  "min_visual_ratio":  0.7  (इतने प्रतिशत सीन में सही फ़ुटेज न मिले तो अपलोड नहीं)
  "low_quality_action": "skip" या "private"  (डिफ़ॉल्ट skip)
  "use_local_clips":   false  (true करने पर clips/ फ़ोल्डर की क्लिप आख़िरी विकल्प में आएँगी)
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
    """config.json पढ़ता है। आख़िर में फ़ालतू अक्षर (जैसे दो बार }) हों तो उन्हें अनदेखा करता है।
    असली ग़लती हो तो साफ़ बताता है कि कौन-सी लाइन में है।"""
    text = (ROOT / "config.json").read_text(encoding="utf-8").lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        try:
            obj, end = json.JSONDecoder().raw_decode(text.lstrip())
            if isinstance(obj, dict):
                print("चेतावनी: config.json के आख़िर में फ़ालतू अक्षर हैं "
                      f"(लाइन {e.lineno}), उन्हें अनदेखा किया। फ़ाइल ठीक कर लें।")
                return obj
        except Exception:
            pass
        lines = text.splitlines()
        near = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
        raise SystemExit(
            f"config.json में ग़लती है: लाइन {e.lineno}, कॉलम {e.colno} ({e.msg})\n"
            f"उस लाइन में लिखा है: {near!r}\n"
            "ध्यान दें: हर लाइन के अंत में कॉमा चाहिए (आख़िरी को छोड़कर) और फ़ाइल के अंत में सिर्फ़ एक }"
        )


CFG = load_cfg()


def get_key(name):
    """चाबी पहले GitHub Secret (env) से, न मिले तो config.json से (छोटे अक्षरों वाले नाम से)।
    आगे-पीछे की ख़ाली जगह/नई लाइन अपने-आप हटा देता है।"""
    v = (os.environ.get(name) or "").strip()
    if not v:
        v = str(CFG.get(name.lower(), "") or "").strip()
    return v
WORK = ROOT / "work"
HIST = ROOT / "state" / "history.json"
W, H = 1080, 1920
FPS = 25
# Wikimedia की नीति: पहचान और संपर्क वाला User-Agent चाहिए, वरना रोक देते हैं
UA = {"User-Agent": "yt-auto-bot/3.0 (https://github.com/chapkeravi3-glitch/yt-auto-bot; hobby project) python-requests"}

DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
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
    '"fallback_queries": ["...", "..."], '
    '"scenes": [{"text": "...", "kind": "photo", "queries": ["...", "..."]}]}'
)

COMMON_RULES = """
सख़्त नियम:
- भाषा: सरल बोलचाल की हिंदी (देवनागरी)।
- अपनी तरफ़ से कोई नई बात, आँकड़ा, तारीख़ या बयान मत गढ़ो। अफ़वाह, विवाद, किसी की निजी ज़िंदगी या बदनामी वाली बात नहीं। जो पक्का न हो उसे "रिपोर्ट्स के मुताबिक" कहो या छोड़ दो।
- "hook": स्क्रीन पर बड़ा लिखा जाने वाला 3-6 शब्दों का चौंकाने वाला वाक्य (जैसे "ये क्या हो गया?!").
- पहले सीन का text ज़बरदस्त हुक हो (6-10 शब्द): चौंकाने वाली बात या सवाल, ताकि पहले 2 सेकंड में दर्शक रुक जाए।
- कुल {n} सीन। बाकी हर सीन 6 से 12 शब्द का, छोटे वाक्य, तेज़ रफ़्तार, हर सीन में सिर्फ़ एक नई बात।
- बीच में एक सस्पेंस/सवाल रखो ताकि दर्शक रुका रहे। आख़िर में चौंकाने वाला मोड़ और छोटी अपील (सब्सक्राइब)। आख़िरी लाइन ऐसी हो कि वीडियो दोबारा शुरू से देखने का मन करे।
- हर सीन के लिए "kind" चुनो:
  * "photo": जब बात किसी ख़ास व्यक्ति, जगह, फ़िल्म, घटना या चीज़ की हो। queries में उसका सही अंग्रेज़ी नाम लिखो (जैसे "Shah Rukh Khan", "Taj Mahal").
  * "stock": जब आम माहौल दिखाना हो। queries में चलती फ़ुटेज के 2-3 अंग्रेज़ी शब्द (जैसे "crowd applause", "city night", "film camera").
- हर सीन के "queries" में 2-3 अंग्रेज़ी विकल्प हों: पहला सबसे सटीक, बाद वाले ज़्यादा आम।
- "fallback_queries": 4-5 आम पर विषय से जुड़ी अंग्रेज़ी फ़ुटेज-खोजें (जैसे "cinema theatre", "film reel", "red carpet", "audience clapping"), जो किसी भी सीन के पीछे ठीक लगें।
- title हिंदी में, 70 अक्षरों तक, जिज्ञासा जगाने वाला। description 2-3 लाइन। tags 8-12 (हिंदी और अंग्रेज़ी मिलाकर)।
- ये टाइटल पहले बन चुके हैं, इनसे अलग नई कहानी चुनो:
{recent}

सिर्फ़ यह JSON लौटाओ:
""" + JSON_SHAPE.replace("{", "{{").replace("}", "}}")  # .format() के लिए कोष्ठक दोहरे

PROMPT_NEWS = """तुम एक यूट्यूब शॉर्ट्स स्क्रिप्ट राइटर हो। चैनल का विषय: {niche}

नीचे आज की असली ख़बरें/ट्रेंड हैं:
{headlines}

इनमें से चैनल के विषय से जुड़ी सबसे दिलचस्प और ताज़ा एक कहानी चुनो और उस पर लगभग 35-50 सेकंड का शॉर्ट वीडियो बनाओ। सिर्फ़ वही बातें लिखो जो इन ख़बरों में साफ़ लिखी हैं या जो पक्का और सर्वमान्य तथ्य हैं।
""" + COMMON_RULES

PROMPT_EVERGREEN = """तुम एक यूट्यूब शॉर्ट्स स्क्रिप्ट राइटर हो। चैनल का विषय: {niche}

आज की ख़बरें नहीं मिल सकीं। इसलिए चैनल के विषय पर एक मशहूर, पक्का और जाँचा हुआ रोचक तथ्य चुनो और उस पर लगभग 35-50 सेकंड का शॉर्ट वीडियो बनाओ।
""" + COMMON_RULES


class GeminiDown(Exception):
    """सब मॉडल व्यस्त/कोटा ख़त्म: आज वीडियो छोड़ना है, जॉब लाल नहीं होना चाहिए।"""


# ---------------------------------------------------------------- history / summary
def load_hist():
    try:
        return json.loads(HIST.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_hist(items):
    HIST.parent.mkdir(exist_ok=True)
    HIST.write_text(json.dumps(items[-300:], ensure_ascii=False, indent=1), encoding="utf-8")


def summary(text):
    """GitHub Actions के रन-पन्ने पर साफ़ संदेश दिखाता है।"""
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n\n")
        except Exception:
            pass


# ---------------------------------------------------------------- ख़बरें / ट्रेंड
def _rss_items(url):
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        if title:
            out.append((title, desc))
    return out


def fetch_headlines():
    """Google News (हिंदी) और Google Trends से आज की ख़बरों की सूची। न मिले तो ख़ाली सूची।"""
    region = CFG.get("region", "IN")
    q = (CFG.get("news_query") or CFG.get("niche", "")).strip()
    q = re.sub(r"\s+", " ", q)[:60]
    lines, seen = [], set()

    def add(title, desc, tag):
        key = re.sub(r"\W+", "", title.lower())[:40]
        if key in seen:
            return
        seen.add(key)
        line = f"- [{tag}] {title}"
        if desc and desc.lower() not in title.lower():
            line += f" :: {desc[:160]}"
        lines.append(line)

    try:
        url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(q + " when:2d")
               + f"&hl=hi&gl={region}&ceid={region}:hi")
        for t, d in _rss_items(url)[:14]:
            add(t, d, "ख़बर")
    except Exception as e:
        print("news issue:", e)
    try:
        for t, d in _rss_items(f"https://trends.google.com/trending/rss?geo={region}")[:12]:
            add(t, d, "ट्रेंड")
    except Exception as e:
        print("trends issue:", e)
    print(f"ख़बरें मिलीं: {len(lines)}")
    return lines[:26]


# ---------------------------------------------------------------- gemini
def _parse_json(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", (txt or "").strip(), flags=re.M).strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _retry_delay(text):
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text or "")
    return float(m.group(1)) if m else 0.0


def gemini_json(prompt):
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise GeminiDown("GEMINI_API_KEY नहीं मिली")
    models = list(CFG.get("gemini_models") or [])
    for m in DEFAULT_MODELS:  # हर मॉडल का कोटा अलग होता है, इसलिए सब बारी-बारी से
        if m not in models:
            models.append(m)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.0, "responseMimeType": "application/json"},
    }
    last = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = requests.post(
                    url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body, timeout=120,
                )
            except Exception as e:
                last = f"{model}: {e}"
                time.sleep(4)
                continue
            if r.status_code == 200:
                try:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return _parse_json(txt)
                except Exception as e:
                    last = f"{model} parse error: {e}"
                    continue
            last = f"{model} {r.status_code}: {r.text[:160]}"
            print("Gemini issue:", last)
            if r.status_code in (400, 404):
                break
            if r.status_code == 429:
                delay = _retry_delay(r.text)
                # रोज़ का कोटा ख़त्म हो तो रुकने का फ़ायदा नहीं: अगला मॉडल
                if attempt == 0 and 0 < delay <= 25 and "PerDay" not in r.text:
                    time.sleep(delay + 1)
                    continue
                break
            time.sleep(6 * (attempt + 1))  # 500/503: थोड़ा रुककर
    raise GeminiDown("Gemini से स्क्रिप्ट नहीं मिली: " + last)


def pollinations_json(prompt):
    """Gemini न चले तो मुफ़्त बैकअप (बिना key)।"""
    r = requests.post(
        "https://text.pollinations.ai/",
        json={"messages": [{"role": "user", "content": prompt}], "model": "openai",
              "jsonMode": True},
        headers=UA, timeout=120,
    )
    r.raise_for_status()
    return _parse_json(r.text)


def make_script(hist):
    n = int(CFG.get("scenes", 12))
    recent = "\n".join("- " + h["title"] for h in hist[-40:]) or "(अभी कोई नहीं)"
    heads = fetch_headlines()
    if heads:
        prompt = PROMPT_NEWS.format(niche=CFG["niche"], headlines="\n".join(heads),
                                    n=n, recent=recent)
    else:
        prompt = PROMPT_EVERGREEN.format(niche=CFG["niche"], n=n, recent=recent)

    try:
        data = gemini_json(prompt)
    except GeminiDown as e:
        print(str(e))
        print("बैकअप AI (Pollinations) आज़मा रहे हैं...")
        try:
            data = pollinations_json(prompt)
        except Exception as e2:
            raise GeminiDown(str(e) + " | बैकअप भी नहीं चला: " + str(e2)[:120])

    scenes = []
    for s in data.get("scenes", []):
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        qs = s.get("queries") or ([s["image_query"]] if s.get("image_query") else [])
        qs = [str(q).strip() for q in qs if str(q).strip()][:3]
        kind = "photo" if str(s.get("kind", "")).lower() == "photo" else "stock"
        scenes.append({"text": text, "kind": kind, "queries": qs})
    if len(scenes) < 5:
        raise RuntimeError("स्क्रिप्ट में सीन बहुत कम हैं")
    data["scenes"] = scenes[:16]
    fb = [str(q).strip() for q in (data.get("fallback_queries") or []) if str(q).strip()]
    for q in CFG.get("fallback_queries", []):
        if q not in fb:
            fb.append(q)
    data["fallback_queries"] = fb[:8]
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
            y = int(H * 0.62)
            for k, wd in zip(chunk, widths):
                color = (255, 221, 0, 255) if k == active else (255, 255, 255, 255)
                d.text((x, y), words[k][2], font=font, fill=color,
                       stroke_width=8, stroke_fill=(0, 0, 0, 255))
                x += wd + gap
            path = WORK / f"cap{idx}_{active}.png"
            img.save(path)
            result.append((path, words[active][0], words[active][1]))
    return result


def hook_image(text):
    """पहले ढाई सेकंड स्क्रीन के ऊपर दिखने वाला बड़ा हुक-टेक्स्ट।"""
    text = (text or "").strip()
    if not text:
        return None
    words = text.split()
    size = 120
    while True:
        font = load_font(size)
        probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        lines, cur = [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if cur and probe.textlength(trial, font=font) > W - 140:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
        if len(lines) <= 3 or size <= 64:
            break
        size -= 8
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lh = int(size * 1.35)
    y = int(H * 0.20)
    pad = 36
    box_h = lh * len(lines) + pad
    d.rounded_rectangle([50, y - pad, W - 50, y - pad + box_h + pad],
                        radius=36, fill=(0, 0, 0, 150))
    for ln in lines:
        tw = d.textlength(ln, font=font)
        d.text(((W - tw) / 2, y), ln, font=font, fill=(255, 221, 0, 255),
               stroke_width=7, stroke_fill=(0, 0, 0, 255))
        y += lh
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
    d.text(((W - w) / 2, 130), mark, font=f, fill=(255, 215, 0, 255),
           stroke_width=3, stroke_fill=(0, 0, 0, 255))
    path = WORK / "mark.png"
    img.save(path)
    return path


# ---------------------------------------------------------------- फ़ुटेज/फ़ोटो खोज
STOP = set("a an the of in on at to for and with from by video footage shot view".split())
USED = set()
CLIP_POOL = []  # इस वीडियो में अब तक मिली क्लिप (फिर से इस्तेमाल के लिए)


def qwords(q):
    return [w for w in re.findall(r"[a-z0-9]+", (q or "").lower()) if len(w) > 2 and w not in STOP]


def match_score(words, text):
    t = (text or "").lower()
    return sum(1 for w in words if w in t)


def _download(url, min_bytes=40_000, max_bytes=45_000_000, headers=None):
    for _ in range(2):
        try:
            r = requests.get(url, timeout=180, headers=headers or UA, stream=True)
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(1 << 20):
                data += chunk
                if len(data) > max_bytes:
                    return None
            if len(data) >= min_bytes:
                return data
        except Exception as e:
            print("download issue:", str(e)[:100])
            time.sleep(2)
    return None


def _img_from_bytes(content):
    img = Image.open(io.BytesIO(content))
    img.load()
    return img.convert("RGB")


def pexels_videos(query):
    key = get_key("PEXELS_API_KEY")
    if not key or not query:
        return []
    out = []
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": query, "orientation": "portrait", "per_page": 15},
            timeout=30,
        )
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height")]
            portrait = [f for f in files if f["height"] >= f["width"] and f["width"] >= 540]
            pool = portrait or [f for f in files if f["width"] >= 1280]
            if not pool:
                continue
            best = max(pool, key=lambda f: min(f["width"], 1080))
            slug = (v.get("url", "").rstrip("/").split("/")[-1]).replace("-", " ")
            out.append({"id": f"pexels{v.get('id')}", "url": best["link"],
                        "dur": float(v.get("duration") or 0), "text": slug})
    except Exception as e:
        print("pexels video issue:", e)
    return out


def pixabay_videos(query):
    key = get_key("PIXABAY_API_KEY")
    if not key or not query:
        return []
    out = []
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": key, "q": query[:90], "per_page": 20, "safesearch": "true"},
            timeout=30,
        )
        for h in r.json().get("hits", []):
            vids = h.get("videos", {})
            url = None
            for size in ("large", "medium", "small"):
                v = vids.get(size) or {}
                if v.get("url") and (v.get("size") or 0) < 45_000_000:
                    url = v["url"]
                    break
            if url:
                out.append({"id": f"pixabay{h.get('id')}", "url": url,
                            "dur": float(h.get("duration") or 0), "text": h.get("tags", "")})
    except Exception as e:
        print("pixabay video issue:", e)
    return out


def pick_video(queries):
    """सिर्फ़ वही क्लिप जिसके शीर्षक/टैग में खोज-शब्द मिलें। लौटाता है (path, query) या None।"""
    for q in queries:
        words = qwords(q)
        if not words:
            continue
        px, pb = pexels_videos(q), pixabay_videos(q)
        cands = [c for c in px + pb if c["dur"] >= 4 and c["id"] not in USED]
        for c in cands:
            c["score"] = match_score(words, c["text"])
        good = [c for c in cands if c["score"] >= 1]
        print(f"    खोज '{q}': Pexels {len(px)}, Pixabay {len(pb)}, मेल खाईं {len(good)}")
        if not good:
            continue
        top = max(c["score"] for c in good)
        order = [c for c in good if c["score"] == top]
        random.shuffle(order)
        for c in order[:3]:
            data = _download(c["url"], min_bytes=80_000)
            if data:
                p = WORK / f"clip_{random.randint(1, 10**9)}.mp4"
                p.write_bytes(data)
                USED.add(c["id"])
                CLIP_POOL.append(p)
                return p, q
    return None


def _license_ok(lic):
    low = re.sub(r"[-_]", " ", (lic or "").lower())
    if re.search(r"\b(nc|nd)\b", low):
        return False
    return bool(re.search(r"cc by|cc0|public domain|^pd\b", low))


def commons_photo(query):
    """Wikimedia Commons की मुफ़्त-लाइसेंस वाली असली फ़ोटो। लौटाता है (img, credit) या None।"""
    words = qwords(query)
    if not words:
        return None
    need = max(1, math.ceil(len(words) * 0.6))
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 20,
        "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 1600,
    }
    try:
        r = None
        for attempt in range(2):
            r = requests.get("https://commons.wikimedia.org/w/api.php", params=params,
                             headers=UA, timeout=30)
            if r.status_code == 200:
                break
            print(f"commons issue: HTTP {r.status_code} {r.text[:80]!r}")
            time.sleep(3)
        if r is None or r.status_code != 200:
            return None
        pages = r.json().get("query", {}).get("pages", {})
    except Exception as e:
        print("commons issue:", e)
        return None
    cands = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if min(info.get("width", 0), info.get("height", 0)) < 700:
            continue
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if not _license_ok(lic):
            continue
        desc = re.sub(r"<[^>]+>", " ", (meta.get("ImageDescription") or {}).get("value", ""))
        title = page.get("title", "").replace("File:", "")
        sc = match_score(words, f"{title} {desc}")
        if sc < need:
            continue
        url = info.get("thumburl") or info.get("url")
        if url and url not in USED:
            artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()
            cands.append((sc, url, f"{title} - {artist or 'Wikimedia Commons'} ({lic})"))
    cands.sort(key=lambda x: -x[0])
    for _, url, credit in cands[:4]:
        data = _download(url, min_bytes=20_000)
        if not data:
            continue
        try:
            img = _img_from_bytes(data)
        except Exception:
            continue
        USED.add(url)
        return img, credit[:200]
    return None


def pexels_photo(query):
    key = get_key("PEXELS_API_KEY")
    words = qwords(query)
    if not key or not words:
        return None
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": key},
            params={"query": query, "orientation": "portrait", "per_page": 15},
            timeout=30,
        )
        photos = [p for p in r.json().get("photos", [])
                  if match_score(words, p.get("alt", "")) >= 1 and p["id"] not in USED]
        random.shuffle(photos)
        for p in photos[:3]:
            data = _download(p["src"]["portrait"], min_bytes=20_000)
            if data:
                USED.add(p["id"])
                return _img_from_bytes(data)
    except Exception as e:
        print("pexels photo issue:", e)
    return None


def compose_photo(img):
    """धुंधली पृष्ठभूमि + बीच में साफ़ पूरी फ़ोटो (कैप्शन के लिए नीचे जगह छोड़कर)।"""
    bg = ImageOps.fit(img, (W, H)).filter(ImageFilter.GaussianBlur(32))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.45)
    iw, ih = img.size
    if ih / iw >= 1.5:  # पहले से खड़ी (portrait) फ़ोटो: पूरी स्क्रीन भर दें
        return Image.blend(ImageOps.fit(img, (W, H)),
                           Image.new("RGB", (W, H), (0, 0, 0)), 0.12)
    area_h = int(H * 0.50)
    scale = min(W / iw, area_h / ih)
    fg = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)
    x = (W - fg.size[0]) // 2
    y = int(H * 0.10) + (area_h - fg.size[1]) // 2
    bg.paste(fg, (x, y))
    return bg


def gradient_bg():
    c1, c2 = random.choice(PALETTES)
    g = Image.linear_gradient("L").resize((W, H))
    return ImageOps.colorize(g, c1, c2).convert("RGB")


def local_clip():
    if not CFG.get("use_local_clips", False):
        return None
    files = []
    for ext in ("mp4", "mov", "webm", "mkv"):
        files += glob.glob(str(ROOT / "clips" / f"*.{ext}"))
    return pathlib.Path(random.choice(files)) if files else None


def get_visual(scene, idx, fallbacks=()):
    """सीन के लिए सबसे सही फ़ुटेज/फ़ोटो। लौटाता है (kind, src, ok, credit)।
    ok: 1 = सटीक/विषय से जुड़ी फ़ुटेज, 0.5 = पहले मिली क्लिप दोबारा, 0 = सादा रंगीन बैकग्राउंड।"""
    queries = scene.get("queries") or []
    kind = scene.get("kind", "stock")

    def photo_path(img):
        p = WORK / f"bg{idx}.jpg"
        compose_photo(img).save(p, quality=92)
        return p

    if kind == "photo":
        for q in queries:
            got = commons_photo(q)
            if got:
                print(f"  असली फ़ोटो (Commons) | {q}")
                return "image", photo_path(got[0]), 1, got[1]
        got = pick_video(queries)
        if got:
            print(f"  वीडियो क्लिप | {got[1]}")
            return "video", got[0], 1, None
        for q in queries:
            img = pexels_photo(q)
            if img is not None:
                print(f"  फ़ोटो (Pexels) | {q}")
                return "image", photo_path(img), 1, None
    else:
        got = pick_video(queries)
        if got:
            print(f"  वीडियो क्लिप | {got[1]}")
            return "video", got[0], 1, None
        for q in queries:
            img = pexels_photo(q)
            if img is not None:
                print(f"  फ़ोटो (Pexels) | {q}")
                return "image", photo_path(img), 1, None

    # विषय से जुड़ी आम फ़ुटेज (जैसे फ़िल्म वाले चैनल के लिए "film reel", "red carpet")
    fb = [q for q in fallbacks if q]
    random.shuffle(fb)
    for q in fb[:4]:
        got = pick_video([q])
        if got:
            print(f"  विषय से जुड़ी आम क्लिप | {got[1]}")
            return "video", got[0], 1, None
    if CLIP_POOL:
        print("  पहले मिली क्लिप दोबारा (नए हिस्से से)")
        return "video", random.choice(CLIP_POOL), 0.5, None
    clip = local_clip()
    if clip:
        print("  अपनी क्लिप (clips/ फ़ोल्डर)")
        return "video", clip, 0.5, None
    print(f"  सही फ़ुटेज नहीं मिली, रंगीन बैकग्राउंड | {queries[:1]}")
    p = WORK / f"bg{idx}.jpg"
    gradient_bg().save(p, quality=92)
    return "image", p, 0, None


# ---------------------------------------------------------------- video
def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2500:])
        raise RuntimeError("ffmpeg में दिक्कत आई")


def scene_video(kind, src, audio, out, d, overlays, mark):
    """overlays = [(png, शुरू, ख़त्म)] : हुक + कैप्शन। mark = वॉटरमार्क png या None।"""
    total = d + 0.35
    inputs = []
    grade = "eq=contrast=1.06:saturation=1.15"
    if kind == "video":
        try:
            cd = duration(src)
        except Exception:
            cd = 0
        start = random.uniform(0, cd - total - 0.5) if cd > total + 1 else 0
        inputs += ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", str(src)]
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,fps={FPS},{grade},"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{CFG.get('video_darkness', 0.12)}:t=fill,"
                f"format=yuv420p[bg]")
    else:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(src)]
        base = (f"[0:v]scale={W * 3 // 2}:{H * 3 // 2},"
                f"zoompan=z='min(1+0.0008*on,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:s={W}x{H}:fps={FPS},{grade},format=yuv420p[bg]")
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
    USED.clear()
    CLIP_POOL.clear()
    if not features.check("raqm"):
        print("चेतावनी: raqm नहीं मिला, हिंदी मात्राएँ थोड़ी बिगड़ सकती हैं")
    mark = watermark_image()
    hook = hook_image(script.get("hook", ""))
    clips, credits, ok_count = [], [], 0
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

        kind, src, ok, credit = get_visual(sc, i, script.get("fallback_queries", []))
        ok_count += ok
        if credit:
            credits.append(credit)

        v = WORK / f"v{i}.mp4"
        scene_video(kind, src, a, v, d, overlays, mark)
        clips.append(v)
        print(f"सीन {i + 1}/{len(scenes)} तैयार ({d:.1f}s, {kind}, सही फ़ुटेज: {ok:g})")

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
    return final, {"total": len(scenes), "visual": ok_count, "credits": credits}


# ---------------------------------------------------------------- upload
def upload(video, title, description, tags, privacy=None):
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
    privacy = privacy or os.environ.get("PRIVACY") or CFG.get("privacy", "public")
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


def _test_pixabay(key):
    try:
        r = requests.get("https://pixabay.com/api/videos/",
                         params={"key": key, "q": "cinema", "per_page": 3}, timeout=25)
        if r.status_code == 200:
            return True, f"चल रही है ({r.json().get('totalHits', 0)} वीडियो मिले)"
        return False, f"काम नहीं कर रही (HTTP {r.status_code}: {r.text[:50]!r})"
    except Exception as e:
        return False, f"जाँच नहीं हो सकी ({str(e)[:50]})"


def _test_pexels(key):
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": key},
                         params={"query": "cinema", "per_page": 1}, timeout=25)
        if r.status_code == 200:
            return True, "चल रही है"
        return False, f"काम नहीं कर रही (HTTP {r.status_code})"
    except Exception as e:
        return False, f"जाँच नहीं हो सकी ({str(e)[:50]})"


def key_status():
    """कौन-सी चाबियाँ मिलीं और वीडियो वाली चाबियाँ सचमुच चलती हैं या नहीं।
    सिर्फ़ हाँ/नहीं और लंबाई छापता है, चाबी की कीमत कभी नहीं।"""
    print("चाबियों की स्थिति:")
    usable = {}
    for n in ["GEMINI_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY",
              "YT_REFRESH_TOKEN", "YT_CLIENT_ID", "YT_CLIENT_SECRET"]:
        env_v = (os.environ.get(n) or "")
        v = get_key(n)
        if not v:
            note = "(Secret मिला पर खाली है)" if n in os.environ else "(Secret/env में नहीं)"
            print(f"  {n}: ❌ नहीं मिली {note}")
            usable[n] = False
            continue
        src = "Secret" if env_v.strip() else "config.json"
        extra = ""
        ok = True
        if n == "PIXABAY_API_KEY":
            ok, extra = _test_pixabay(v)
        elif n == "PEXELS_API_KEY":
            ok, extra = _test_pexels(v)
        print(f"  {n}: {'✅' if ok else '⚠️'} मिली ({src}, {len(v)} अक्षर) {extra}".rstrip())
        usable[n] = ok
    return usable


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    keys = key_status()
    if not keys["PEXELS_API_KEY"] and not keys["PIXABAY_API_KEY"]:
        summary("❌ रुक गया: असली वीडियो-फ़ुटेज के लिए PEXELS_API_KEY या PIXABAY_API_KEY "
                "(दोनों मुफ़्त) चाहिए, पर कोई भी चलती हुई नहीं मिली। ऊपर लॉग में हर चाबी की स्थिति देखें: "
                "'नहीं मिली' = Secret नहीं पहुँचा, '⚠️ काम नहीं कर रही' = चाबी ग़लत/पुरानी है।\n\n"
                "GitHub रेपो -> Settings -> Secrets and variables -> Actions में नाम बिल्कुल "
                "PIXABAY_API_KEY रखें और नई चाबी चिपकाएँ। (आख़िरी उपाय: private रेपो में config.json में "
                "\"pixabay_api_key\": \"...\" लिख सकते हैं।)")
        sys.exit(1)
    hist = load_hist()
    try:
        script = make_script(hist)
    except GeminiDown as e:
        summary("⚠️ आज वीडियो नहीं बना: Gemini अभी उपलब्ध नहीं (कोटा ख़त्म या सर्वर व्यस्त)। "
                "कल अपने-आप दोबारा कोशिश होगी।\n\nकारण: " + str(e)[:300])
        return

    title = str(script.get("title", "रोचक फ़ैक्ट")).strip()
    if len(title) > 85:
        title = title[:85].rstrip()
    if "#shorts" not in title.lower():
        title += " #Shorts"
    print("टाइटल:", title)

    video, stats = build_video(script)

    hashtags = " ".join(CFG.get("hashtags", ["#Shorts"]))
    description = str(script.get("description", "")).strip() + "\n\n" + hashtags
    if stats["credits"]:
        description += "\n\nफ़ोटो: " + " | ".join(stats["credits"][:6])
    description += "\n\nयह वीडियो AI की मदद से बनाया गया है।"
    tags = clean_tags(script.get("tags"))

    ratio = stats["visual"] / max(1, stats["total"])
    min_ratio = float(CFG.get("min_visual_ratio", 0.7))
    print(f"सही फ़ुटेज वाले सीन: {stats['visual']:g}/{stats['total']} ({ratio:.0%})")
    privacy = None
    if ratio < min_ratio:
        action = CFG.get("low_quality_action", "skip")
        if action != "private":
            summary(f"⏭️ वीडियो अपलोड नहीं किया: सिर्फ़ {stats['visual']:g}/{stats['total']} सीन में "
                    f"सही फ़ुटेज मिली (कम से कम {min_ratio:.0%} चाहिए)। टाइटल: {title}\n\n"
                    "बेकार वीडियो डालने से बेहतर है आज कुछ न डालना। work/final.mp4 देखने के लिए "
                    "रन के Artifacts खोलें।")
            hist.append({"date": datetime.date.today().isoformat(), "title": title,
                         "id": "", "skipped": True})
            save_hist(hist)
            return
        privacy = "private"
        print("कम गुणवत्ता: वीडियो private अपलोड होगा, आप ख़ुद देखकर तय करें")

    if dry:
        print("DRY RUN: अपलोड नहीं किया। वीडियो:", video)
        return

    vid = upload(video, title, description, tags, privacy)
    summary(f"✅ अपलोड हो गया: https://youtube.com/shorts/{vid}  (सही फ़ुटेज {stats['visual']:g}/{stats['total']})")
    hist.append({"date": datetime.date.today().isoformat(), "title": title, "id": vid})
    save_hist(hist)


if __name__ == "__main__":
    main()
