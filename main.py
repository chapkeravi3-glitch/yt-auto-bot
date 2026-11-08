# -*- coding: utf-8 -*-
"""
Auto Shorts maker v3 (Fully Tested & Validated)
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
                color = (255, 221, 0, 255) if k == active else (255, 255
