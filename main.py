#!/usr/bin/env python3
"""
main.py - यूट्यूब ऑटो वीडियो बॉट (शॉर्ट्स + लॉन्ग)  [नया वर्ज़न]

नया क्या है:
  1. आज की ताज़ा खबरें (Google News RSS) लेकर उसी पर स्क्रिप्ट बनती है
  2. ज़्यादातर सीन में असली वीडियो क्लिप (image_finder.py से), बाकी में HD फ़ोटो
  3. आवाज़ स्मूथ: हर सीन की शुरू/आख़िर की चुप्पी कटती है और पूरी आवाज़ एक सतत ट्रैक बनती है
  4. Gemini की "thinking" बंद, ताकि लॉन्ग स्क्रिप्ट बीच में कटे नहीं

चलाने का तरीका:
    python main.py --channel fact --type short
    python main.py --channel movies --type long
    python main.py --channel fact --type short --no-upload   # सिर्फ़ टेस्ट

config.json में हर चैनल में ये वैकल्पिक चीज़ें जोड़ सकते हो:
    "news_queries": ["cricket India", "IPL"],   # खबरें किन शब्दों से खोजनी हैं (न दो तो themes से)
    "use_news": true,                           # false करो तो पुराने तरीके से themes से टॉपिक
    "voice_rate": "+6%",                        # आवाज़ की रफ़्तार
    "voice_pitch": "+0Hz"
"""
import argparse
import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, features

import image_finder as IF

ROOT = Path(__file__).parent
WORK = ROOT / "work"
OUT = ROOT / "out"
USED = ROOT / "used_topics.json"
CONFIG = ROOT / "config.json"
FPS = 30
GAP = 0.10   # दो सीन के बीच की स्वाभाविक छोटी साँस (सेकंड)


def log(*a):
    print(*a, flush=True)


def sh(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} फेल: {p.stderr[-900:]}")
    return p.stdout


# ================================================================ ताज़ा खबरें
def fetch_news(ch, recent):
    """Google News RSS से पिछले 1-2 दिन की ताज़ा खबरों की हेडलाइन लौटाता है"""
    queries = ch.get("news_queries") or ch.get("themes", [])[:3]
    heads, seen = [], set()
    recent_text = " ".join(recent[-40:])
    for q in queries[:6]:
        for when in ("2d", "7d"):
            try:
                r = requests.get("https://news.google.com/rss/search",
                                 params={"q": f"{q} when:{when}", "hl": "hi", "gl": "IN",
                                         "ceid": "IN:hi"},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                items = ET.fromstring(r.content).findall(".//item")
            except Exception as e:
                log(f"  खबर नहीं मिली ({q}): {str(e)[:80]}")
                items = []
            got = 0
            for it in items[:10]:
                t = (it.findtext("title") or "").strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                # बहुत मिलती-जुलती (पहले बन चुकी) खबर छोड़ दो
                words = [w for w in re.findall(r"\w+", t) if len(w) > 3][:6]
                if words and sum(w in recent_text for w in words) >= 4:
                    continue
                heads.append(t)
                got += 1
            if got:
                break
    log(f"  ताज़ा खबरें मिलीं: {len(heads)}")
    return heads[:18]


def fetch_trends():
    """Google Trends India: आज भारत में सबसे ज़्यादा खोजी जा रही चीज़ें (साथ में उनकी खबर की हेडलाइन)"""
    out = []
    for url in ("https://trends.google.com/trending/rss?geo=IN",
                "https://trends.google.com/trends/trendingsearches/daily/rss?geo=IN"):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            if r.status_code != 200:
                continue
            for it in ET.fromstring(r.content).findall(".//item")[:25]:
                title = (it.findtext("title") or "").strip()
                traffic, news = "", ""
                for ch_ in it:
                    tag = ch_.tag.split("}")[-1]
                    if tag == "approx_traffic":
                        traffic = (ch_.text or "").strip()
                    elif tag == "news_item":
                        for n in ch_:
                            if n.tag.split("}")[-1] == "news_item_title" and not news:
                                news = (n.text or "").strip()
                if title:
                    out.append(f"{title} ({traffic}) - {news}" if news else f"{title} ({traffic})")
            if out:
                break
        except Exception as e:
            log(f"  ट्रेंड्स नहीं मिले: {str(e)[:80]}")
    log(f"  भारत में ट्रेंडिंग: {len(out)}")
    return out


# ================================================================ Gemini
def list_gemini_models(key):
    """Gemini की तरफ़ से अभी चालू मॉडल की लिस्ट (नए/stable पहले)"""
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key, "pageSize": 200}, timeout=60)
    except Exception as e:
        log(f"  मॉडल लिस्ट नहीं मिली: {str(e)[:100]}")
        return []
    if r.status_code != 200:
        log(f"  ⚠ मॉडल लिस्ट माँगने पर {r.status_code}: {r.text[:200]}")
        log("  → शायद GEMINI_API_KEY गलत है या बंद है")
        return []
    skip = ("image", "tts", "audio", "live", "embedding", "vision", "robotics",
            "computer", "native", "thinking", "learnlm", "gemma", "aqa")
    found = []
    for m in r.json().get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "flash" not in name or any(x in name for x in skip):
            continue
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        ver = float(ver.group(1)) if ver else 0
        unstable = any(x in name for x in ("preview", "exp", "-0"))
        found.append((unstable, "lite" in name, -ver, name))
    found.sort()
    return [f[3] for f in found]


def _gemini_try(key, model, prompt, max_tokens):
    """एक मॉडल पर कोशिश। (नतीजा, कारण) लौटाता है; नतीजा None हो तो कारण देखो"""
    last = "कुछ नहीं"
    use_thinking_cfg = bool(re.search(r"gemini-(2\.5|3)", model)) and "pro" not in model
    for attempt in range(5):
        gen = {"temperature": 0.9, "responseMimeType": "application/json",
               "maxOutputTokens": max_tokens}
        if use_thinking_cfg:
            gen["thinkingConfig"] = {"thinkingBudget": 0}   # सोचने में टोकन खर्च न हों, स्क्रिप्ट न कटे
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen},
                timeout=300)
        except Exception as e:
            last = str(e)[:150]
            time.sleep(10)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = min(15 * 2 ** attempt, 120)
            last = f"{model} {r.status_code}"
            log(f"  Gemini {model} → {r.status_code}, {wait} सेकंड रुककर दोबारा")
            time.sleep(wait)
            continue
        if r.status_code == 404:
            log(f"  मॉडल {model} नहीं मिला (बंद हो चुका होगा)")
            return None, f"{model} 404"
        if r.status_code == 400 and use_thinking_cfg and "think" in r.text.lower():
            use_thinking_cfg = False       # इस मॉडल में thinkingConfig नहीं चलता, बिना उसके
            continue
        if r.status_code != 200:
            return None, f"{model} {r.status_code}: {r.text[:150]}"
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            txt = "".join(p.get("text", "") for p in parts).strip()
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
            return json.loads(txt), ""
        except Exception as e:
            last = f"JSON पढ़ने में दिक्कत: {str(e)[:100]}"
            time.sleep(5)
    return None, last


def gemini_json(prompt, models, max_tokens):
    key = os.environ["GEMINI_API_KEY"].strip()
    tried, last = set(), "कुछ नहीं"
    for model in models:
        tried.add(model)
        data, why = _gemini_try(key, model, prompt, max_tokens)
        if data is not None:
            return data
        last = why
    log("  लिखे हुए मॉडल नहीं चले, Gemini से चालू मॉडल की लिस्ट माँग रहे हैं")
    avail = [m for m in list_gemini_models(key) if m not in tried]
    log(f"  चालू मॉडल: {avail[:6]}")
    for model in avail[:4]:
        data, why = _gemini_try(key, model, prompt, max_tokens)
        if data is not None:
            log(f"  ✔ {model} चल गया। config.json के gemini_models में इसे सबसे ऊपर लिख दो")
            return data
        last = why
    raise RuntimeError(f"Gemini से स्क्रिप्ट नहीं मिली ({last})")


def make_script(ch, kind, models, recent, headlines, trends=None):
    if kind == "short":
        length = ("A YouTube Short. 6 to 8 scenes. Total narration 95-115 words "
                  "(about 40-50 seconds when spoken).")
        max_tokens = 8192
    else:
        length = ("A long YouTube video of about 7-8 minutes. 20 to 24 scenes, each narration "
                  "50-65 words. Total 1100-1400 words. Clear intro, well-structured body, short outro.")
        max_tokens = 30000

    today = datetime.utcnow().strftime("%d %B %Y")
    trend_txt = ""
    if trends:
        trend_txt = ("Trending searches in India RIGHT NOW (with the news behind them):\n"
                     + "\n".join(f"- {t}" for t in trends)
                     + "\nIf one of these trending items fits the channel, prefer it above everything else. "
                       "If none fits, use the headlines below.\n")
    focus = ch.get("focus_rule", "")
    if headlines:
        topic_rule = (f"Today is {today}. {focus}\n{trend_txt}Here are the LATEST news headlines (Hindi, newest first):\n"
                      + "\n".join(f"- {h}" for h in headlines)
                      + "\nPick ONE of these headlines that suits the channel best and make the video about "
                        "that news. Use ONLY what the headline itself states plus well-established background "
                        "facts. Never invent details, numbers, quotes or what-happened-next. "
                        "If the news is uncertain, say so honestly.")
    else:
        topic_rule = (f"Pick ONE fresh, specific topic from these themes: {', '.join(ch['themes'])}.")

    anchors = ch.get("visual_anchor", [])
    anchor_rule = (f"- EVERY item in image_queries MUST contain one of these words: {', '.join(anchors)}. "
                   f"Describe a real filmable stock-footage scene, e.g. '{anchors[0]} stadium crowd', "
                   f"'{anchors[0]} player celebrating'. Never write queries about buses, laptops, cities, "
                   f"offices or anything unrelated to {anchors[0]}.\n") if anchors else ""
    prompt = f"""You are the scriptwriter of a Hindi YouTube channel called "{ch['name']}".
Channel niche: {ch['niche']}
{topic_rule}
Do NOT repeat or closely resemble these recent topics: {json.dumps(recent[-40:], ensure_ascii=False)}.

Write {length}
Language of narration/title/description: simple spoken Hindi in Devanagari.

NARRATION STYLE (very important, it will be read by a text-to-speech voice):
- Write exactly how a friendly person TALKS, not like a book or a newspaper. Short everyday sentences (6-14 words).
- Use commas and full stops where a speaker would breathe. No brackets, no emojis, no hashtags, no quotes inside narration.
- The scenes are ONE continuous story. Do not greet or restart in every scene; each scene flows from the previous one.
- No English abbreviations in narration; write them the way they are spoken in Hindi.

Rules:
- Use only true, verifiable facts. If unsure, leave it out.
{anchor_rule}- Scene 1 must be a strong hook (surprising question or fact).
- The last scene: one short line asking viewers to subscribe.
- For each scene give:
  "narration" (Hindi),
  "caption" (Hindi, max 6 words, shown on screen),
  "image_queries" (2-3 English search phrases for STOCK footage/photos built from CONCRETE, filmable nouns: real places, crowds, stadium, city, animals, objects, famous landmarks. No abstract words, no person names unless world-famous),
  "entities" (0-2 exact English names of REAL people, teams, stadiums, films, places or organisations that this scene is about and that have a Wikipedia page, e.g. "Virat Kohli", "Nepal national cricket team", "Narendra Modi Stadium"; empty list if none),
  "visual_en" (one English sentence describing the ideal shot for this scene),
  "prefer_video" (true if motion helps, else false).
- "title": Hindi, under 70 characters, curiosity-driven but honest.
- "description": 2-3 Hindi lines.
- "tags": 8-12 tags mixing Hindi and English.
- "hashtags": 4-5 hashtags like "#Facts".
- "thumbnail_text": 2-4 Hindi words, punchy.

Return ONLY valid JSON in this shape:
{{"title":"","description":"","tags":[],"hashtags":[],"thumbnail_text":"",
"scenes":[{{"narration":"","caption":"","image_queries":["",""],"entities":[],"visual_en":"","prefer_video":false}}]}}"""
    for attempt in range(3):
        s = gemini_json(prompt, models, max_tokens)
        scenes = [x for x in s.get("scenes", []) if x.get("narration")]
        if len(scenes) >= (4 if kind == "short" else 10) and s.get("title"):
            s["scenes"] = scenes
            return s
        log("  स्क्रिप्ट अधूरी आई, दोबारा माँग रहे हैं")
    raise RuntimeError("स्क्रिप्ट ठीक से नहीं बनी")


# ================================================================ आवाज़
async def _tts(text, voice, path, rate, pitch):
    await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(path))


def tts(text, voice, path, rate="+6%", pitch="+0Hz"):
    for a in range(4):
        try:
            asyncio.run(_tts(text, voice, path, rate, pitch))
            if Path(path).exists() and Path(path).stat().st_size > 1000:
                return
        except Exception as e:
            log(f"  आवाज़ बनाने में दिक्कत ({str(e)[:80]}), दोबारा")
        time.sleep(3 * (a + 1))
    raise RuntimeError("आवाज़ नहीं बनी")


def tts_gemini(text, voice_name, model, path):
    """Gemini की इंसान जैसी आवाज़ (उसी GEMINI_API_KEY से)। सफल हो तो True"""
    key = os.environ["GEMINI_API_KEY"].strip()
    prompt = ("Read the following in natural, warm, conversational Hindi, like a friendly human "
              "storyteller. Smooth flow, natural short pauses, never robotic:\n\n" + text)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {
                                     "prebuiltVoiceConfig": {"voiceName": voice_name}}}}}
    for attempt in range(4):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key}, json=body, timeout=180)
        except Exception as e:
            log(f"  Gemini आवाज़: नेटवर्क दिक्कत {str(e)[:80]}")
            time.sleep(10)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = min(20 * (attempt + 1), 90)
            log(f"  Gemini आवाज़ {r.status_code}, {wait} सेकंड रुककर दोबारा")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            log(f"  Gemini आवाज़ {r.status_code}: {r.text[:200]}")
            return False
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            b64 = next(p["inlineData"]["data"] for p in parts if "inlineData" in p)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(base64.b64decode(b64))
            if Path(path).stat().st_size > 5000:
                return True
        except Exception as e:
            log(f"  Gemini आवाज़ में ऑडियो नहीं आया ({str(e)[:80]}), दोबारा")
            time.sleep(5)
    return False


def make_voice_files(scenes, ch, cfg, edge_voice, rate, pitch):
    """सारे सीन की आवाज़ पहले बनाता है। पूरी वीडियो में एक ही आवाज़ रहे, इसलिए
    Gemini किसी एक सीन पर भी फेल हो तो पूरी वीडियो edge-tts में दोबारा बनती है"""
    engine = ch.get("tts_engine", cfg.get("tts_engine", "gemini"))
    files, durs = [], []
    if engine == "gemini":
        model = ch.get("tts_model", cfg.get("tts_model", "gemini-2.5-flash-preview-tts"))
        vname = ch.get("gemini_voice", "Charon")
        ok = True
        for i, sc in enumerate(scenes):
            log(f"  आवाज़ {i + 1}/{len(scenes)} (Gemini {vname})")
            raw = WORK / f"g_{i:02d}.wav"
            if not tts_gemini(sc["narration"], vname, model, raw):
                ok = False
                break
            wav = WORK / f"a_{i:02d}.wav"
            durs.append(prepare_voice(raw, wav))
            files.append(wav)
            time.sleep(4)
        if ok:
            log("🎙 Gemini की आवाज़ इस्तेमाल हुई")
            return files, durs
        log("  Gemini आवाज़ नहीं चली; एक जैसी आवाज़ के लिए पूरी वीडियो edge-tts में बनेगी")
        files, durs = [], []
    for i, sc in enumerate(scenes):
        mp3 = WORK / f"a_{i:02d}.mp3"
        wav = WORK / f"a_{i:02d}.wav"
        tts(sc["narration"], edge_voice, mp3, rate, pitch)
        durs.append(prepare_voice(mp3, wav))
        files.append(wav)
    return files, durs


def check_voice(voice, want="Male"):
    """आवाज़ का नाम और लिंग (Male/Female) जाँचता है; गलत हो तो सही लिंग वाली हिंदी आवाज़ चुनता है"""
    try:
        voices = asyncio.run(edge_tts.list_voices())
    except Exception as e:
        log(f"  [warn] आवाज़ों की लिस्ट नहीं मिली ({str(e)[:80]}), {voice} ही चलाते हैं")
        return voice
    hi = [v for v in voices if v.get("Locale", "").startswith("hi-IN")]
    for v in hi:
        if v["ShortName"] == voice:
            log(f"🎙 आवाज़: {voice} ({v['Gender']})")
            if v["Gender"] == want:
                return voice
    for v in hi:
        if v["Gender"] == want:
            log(f"🎙 {want} आवाज़ बदलकर: {v['ShortName']}")
            return v["ShortName"]
    return voice


def duration(p):
    return float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(p)]).strip())


def prepare_voice(mp3, wav):
    """शुरू/आख़िर की चुप्पी काटता है, फिर पूरे फ़्रेम (1/30 सेकंड) पर नापकर छोटी साँस जोड़ता है।
    इससे सीन बदलने पर आवाज़ रुकती नहीं, एक ही बहाव में चलती है। लौटाता है: सीन की सटीक लंबाई"""
    tmp = Path(str(wav) + ".tmp.wav")
    trim = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.02,"
            "areverse,"
            "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.02,"
            "areverse")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-af", trim,
        "-ar", "44100", "-ac", "1", str(tmp)])
    d = duration(tmp)
    target = math.ceil((d + GAP) * FPS) / FPS
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
        "-af", f"apad=whole_dur={target:.4f}", "-t", f"{target:.4f}",
        "-ar", "44100", "-ac", "1", str(wav)])
    tmp.unlink(missing_ok=True)
    return target


# ================================================================ फ़ॉन्ट / कैप्शन / थंबनेल
def find_font():
    for q in ("Noto Sans Devanagari:bold", "Noto Sans Devanagari", "Lohit Devanagari"):
        try:
            out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", q],
                                 capture_output=True, text=True).stdout
            fam, _, path = out.partition("|")
            if "devanagari" in fam.lower() and os.path.exists(path):
                return path
        except Exception:
            pass
    return None


FONT = find_font()
CAPTIONS_OK = bool(FONT) and features.check("raqm")
log(f"फ़ॉन्ट: {FONT} | कैप्शन: {'चालू' if CAPTIONS_OK else 'बंद (फ़ॉन्ट/raqm नहीं मिला)'}")


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_caption_png(text, W, H, path):
    size = int(W * 0.065) if H > W else int(H * 0.06)
    font = ImageFont.truetype(FONT, size, layout_engine=ImageFont.Layout.RAQM)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines = wrap(d, text, font, W * 0.86)[:3]
    lh = int(size * 1.35)
    box_h = lh * len(lines) + 30
    bottom = int(H * 0.80) if H > W else int(H * 0.92)
    top = bottom - box_h
    d.rounded_rectangle([int(W * 0.05), top, int(W * 0.95), bottom], radius=18,
                        fill=(0, 0, 0, 150))
    y = top + 15
    for ln in lines:
        w = d.textlength(ln, font=font)
        d.text(((W - w) / 2, y), ln, font=font, fill=(255, 255, 255, 255),
               stroke_width=2, stroke_fill=(0, 0, 0, 255))
        y += lh
    img.save(path)


def cover(im, W, H):
    r = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1), Image.LANCZOS)
    x, y = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


def gradient_image(path, W, H):
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(20 + 40 * t), int(30 + 20 * t), int(70 + 90 * t)))
    im.save(path, quality=92)


PALETTES = [((15, 32, 80), (120, 40, 140)), ((10, 60, 70), (20, 20, 60)),
            ((90, 20, 30), (25, 25, 70)), ((20, 70, 40), (10, 30, 60)),
            ((70, 40, 10), (60, 15, 60))]


def make_text_card(text, W, H, path, seed=0):
    """फ़ोटो/क्लिप न मिलने पर: रंगीन बैकग्राउंड पर बड़े अक्षरों में कैप्शन।
    यह फ़ोटो की तरह चलता है, इसलिए build_scene में अपने-आप ज़ूम-इन/ज़ूम-आउट हो जाता है"""
    if not CAPTIONS_OK or not text:
        gradient_image(path, W, H)
        return
    c1, c2 = PALETTES[seed % len(PALETTES)]
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3)))
    size = int(W * 0.13) if H > W else int(H * 0.14)
    while True:
        font = ImageFont.truetype(FONT, size, layout_engine=ImageFont.Layout.RAQM)
        lines = wrap(d, text, font, W * 0.84)
        lh = int(size * 1.4)
        if (len(lines) <= 4 and lh * len(lines) < H * 0.6) or size < 40:
            break
        size = int(size * 0.9)
    lines = lines[:5]
    y = (H - lh * len(lines)) // 2
    bar_w = int(W * 0.25)
    d.rectangle([(W - bar_w) // 2, y - 40, (W + bar_w) // 2, y - 28], fill=(255, 235, 59))
    for ln in lines:
        w = d.textlength(ln, font=font)
        d.text(((W - w) / 2, y), ln, font=font, fill=(255, 255, 255),
               stroke_width=4, stroke_fill=(0, 0, 0))
        y += lh
    im.save(path, quality=94)


def make_thumbnail(img_path, text, out):
    """1280x720 थंबनेल: चमकदार फ़ोटो, नीचे गहरा ग्रेडिएंट, बड़े पीले अक्षर। 2MB से छोटा रखता है"""
    im = cover(Image.open(img_path).convert("RGB"), 1280, 720)
    im = ImageEnhance.Contrast(im).enhance(1.12)
    im = ImageEnhance.Color(im).enhance(1.2)
    im = ImageEnhance.Brightness(im).enhance(0.85)
    shade = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for y in range(720):
        a = int(max(0, (y - 220) / 500) * 200)
        sd.line([(0, y), (1280, y)], fill=(0, 0, 0, a))
    im = Image.alpha_composite(im.convert("RGBA"), shade).convert("RGB")
    d = ImageDraw.Draw(im)
    if CAPTIONS_OK and text:
        size = 130
        while True:
            font = ImageFont.truetype(FONT, size, layout_engine=ImageFont.Layout.RAQM)
            lines = wrap(d, text, font, 1160)
            if len(lines) <= 2 or size < 60:
                break
            size -= 10
        lines = lines[:2]
        lh = int(size * 1.3)
        y = 700 - lh * len(lines)
        for ln in lines:
            w = d.textlength(ln, font=font)
            d.text(((1280 - w) / 2, y), ln, font=font, fill=(255, 235, 59),
                   stroke_width=7, stroke_fill=(0, 0, 0))
            y += lh
    d.rectangle([0, 0, 1279, 719], outline=(255, 235, 59), width=8)
    for q in (90, 80, 70, 60):
        im.save(out, quality=q)
        if Path(out).stat().st_size < 1_900_000:
            break


def grab_frame(video, out):
    """वीडियो क्लिप से एक फ़्रेम निकालता है (थंबनेल के लिए)"""
    try:
        sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(video),
            "-frames:v", "1", "-q:v", "2", str(out)])
        return str(out) if Path(out).exists() else None
    except Exception:
        return None


# ================================================================ वीडियो बनाना (ffmpeg)
def build_scene(i, media, dur, W, H, cap_png):
    """सीन का सिर्फ़ वीडियो (बिना आवाज़) बनाता है। आवाज़ बाद में एक सतत ट्रैक की तरह जुड़ती है"""
    out = WORK / f"scene_{i:02d}.mp4"
    frames = int(round(dur * FPS))
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if media["kind"] == "video":
        cmd += ["-stream_loop", "-1", "-i", media["path"]]
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={W}:{H},setsar=1,fps={FPS},format=yuv420p")
    else:
        big = WORK / f"big_{i:02d}.jpg"
        cover(Image.open(media["path"]).convert("RGB"), int(W * 1.5), int(H * 1.5)).save(big, quality=94)
        cmd += ["-i", str(big)]
        z = ("min(1+0.0004*on,1.25)" if i % 2 == 0 else "max(1.25-0.0004*on,1)")
        base = (f"[0:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p")
    if cap_png:
        cmd += ["-i", str(cap_png)]
        fc = base + "[bg];[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    else:
        fc = base + "[v]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-an",
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-r", str(FPS), "-movflags", "+faststart", str(out)]
    sh(cmd)
    return out


def concat(files, out, kind_args=("-c", "copy")):
    lst = Path(str(out) + ".txt")
    lst.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in files))
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(lst), *kind_args, str(out)])


def mux(video, voice_wav, out):
    """वीडियो + पूरी आवाज़ जोड़ता है; आवाज़ को हल्का साफ़ और बराबर आवाज़-स्तर पर लाता है"""
    af = "highpass=f=70,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,loudnorm=I=-16:TP=-1.5:LRA=9"
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(voice_wav),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", af,
        "-c:a", "aac", "-ar", "44100", "-b:a", "160k", "-shortest",
        "-movflags", "+faststart", str(out)])


# ================================================================ यूट्यूब अपलोड
def upload(ch, video, thumb, s, kind, credits):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None, refresh_token=os.environ[ch["token_secret"]],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ[ch["client_id_secret"]],
        client_secret=os.environ[ch["client_secret_secret"]])
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    title = s["title"].strip()
    if kind == "short":
        title = title[:88].rstrip() + " #Shorts"
    title = title[:100]
    tags_hash = " ".join(s.get("hashtags", [])[:5])
    desc = s.get("description", "").strip() + "\n\n" + tags_hash
    if kind == "short":
        desc += " #Shorts"
    if credits:
        desc += "\n\nफ़ोटो/वीडियो क्रेडिट:\n" + "\n".join(credits[:25])
    desc = desc[:4800]
    tags, total = [], 0
    for t in s.get("tags", []):
        t = str(t).replace("<", "").replace(">", "")[:40]
        if total + len(t) + 1 > 450:
            break
        tags.append(t)
        total += len(t) + 1

    body = {"snippet": {"title": title, "description": desc, "tags": tags,
                        "categoryId": ch.get("category", "22"),
                        "defaultLanguage": "hi", "defaultAudioLanguage": "hi"},
            "status": {"privacyStatus": ch.get("privacy", "public"),
                       "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True,
                            mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, fails = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and fails < 5:
                fails += 1
                time.sleep(5 * fails)
                continue
            raise
    vid = resp["id"]
    log(f"✅ अपलोड हो गया: https://youtu.be/{vid}")
    if thumb:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
            log("   थंबनेल लग गया")
        except Exception as e:
            log(f"   [warn] थंबनेल यूट्यूब ने नहीं लिया: {str(e)[:300]}")
            log("   → अगर 'custom thumbnail' या forbidden दिखे: यूट्यूब चैनल को फ़ोन नंबर से वेरिफाई करो "
                "(youtube.com/verify), तभी API से थंबनेल लगता है")
    return vid


# ================================================================ मुख्य
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--type", choices=["short", "long"], required=True)
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    ch = cfg["channels"][args.channel]
    kind = args.type
    W, H = (720, 1280) if kind == "short" else (1280, 720)
    orient = "portrait" if kind == "short" else "landscape"
    models = os.environ.get("GEMINI_MODELS", "").split(",") if os.environ.get("GEMINI_MODELS") \
        else cfg.get("gemini_models", ["gemini-2.5-flash"])

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir()
    OUT.mkdir(exist_ok=True)
    used_topics = json.loads(USED.read_text(encoding="utf-8")) if USED.exists() else {}
    recent = used_topics.get(args.channel, [])

    if not args.no_upload:
        for k in ("client_id_secret", "client_secret_secret", "token_secret"):
            if not os.environ.get(ch[k], "").strip():
                raise RuntimeError(
                    f"GitHub Secret '{ch[k]}' नहीं मिला या खाली है। "
                    f"Settings → Secrets में यह नाम बनाओ (या config.json में सही नाम लिखो)")

    log(f"▶ चैनल: {ch['name']} | प्रकार: {kind}")
    for line in IF.diagnose():
        log(line)
    headlines = fetch_news(ch, recent) if ch.get("use_news", True) else []
    trends = fetch_trends() if ch.get("use_trends", True) else []
    s = make_script(ch, kind, models, recent, headlines, trends)
    log(f"  टॉपिक: {s['title']} | सीन: {len(s['scenes'])}")

    voice = check_voice(ch.get("voice", "hi-IN-MadhurNeural"), ch.get("gender", "Male"))
    rate, pitch = ch.get("voice_rate", "+6%"), ch.get("voice_pitch", "+0Hz")
    voice_files, durs = make_voice_files(s["scenes"], ch, cfg, voice, rate, pitch)
    used_ids, credits, scene_files, first_img = set(), [], [], None
    clip_count = 0
    card_count = 0
    for i, sc in enumerate(s["scenes"]):
        log(f"[सीन {i + 1}/{len(s['scenes'])}] {sc['narration'][:50]}...")
        dur = durs[i]

        # हर तीन में से दो सीन में वीडियो क्लिप की कोशिश
        want_video = bool(sc.get("prefer_video")) or (i % 3 != 2)
        try:
            media = IF.pick_media(sc.get("image_queries", []), sc.get("visual_en", ""),
                                  orient, WORK, i, used_ids, want_video,
                                  ch.get("visual_anchor", []), ch.get("generic_queries", []),
                                  sc.get("entities", []), cfg.get("web_images", True))
        except Exception as e:
            log(f"  [warn] फ़ोटो/क्लिप खोजने में दिक्कत: {str(e)[:150]}")
            media = None
        if not media:
            log("  कुछ नहीं मिला, बड़े अक्षरों वाला ज़ूम टेक्स्ट कार्ड लगा रहे हैं")
            g = WORK / f"card_{i:02d}.jpg"
            try:
                make_text_card(sc.get("caption") or sc["narration"][:40], W, H, g, i)
            except Exception as e:
                log(f"  [warn] टेक्स्ट कार्ड नहीं बना: {str(e)[:100]}")
                gradient_image(g, W, H)
            media = {"path": str(g), "kind": "image", "credit": "", "textcard": True}
            card_count += 1
        else:
            if media["kind"] == "video":
                clip_count += 1
            if media["credit"] and media["credit"] not in credits:
                credits.append(media["credit"])
        if first_img is None and media["kind"] == "image" and media["credit"]:
            first_img = media["path"]
        elif first_img is None and media["kind"] == "video":
            first_img = grab_frame(media["path"], WORK / f"frame_{i:02d}.jpg")

        cap_png = None
        if CAPTIONS_OK and sc.get("caption") and not media.get("textcard"):
            cap_png = WORK / f"cap_{i:02d}.png"
            try:
                make_caption_png(sc["caption"], W, H, cap_png)
            except Exception as e:
                log(f"  [warn] कैप्शन नहीं बना: {e}")
                cap_png = None
        try:
            scene_files.append(build_scene(i, media, dur, W, H, cap_png))
        except Exception as e:
            log(f"  [warn] सीन में दिक्कत, बिना कैप्शन दोबारा: {str(e)[:200]}")
            try:
                scene_files.append(build_scene(i, media, dur, W, H, None))
            except Exception as e2:
                log(f"  [warn] फिर भी नहीं बना, सादा बैकग्राउंड: {str(e2)[:200]}")
                g = WORK / f"grad_{i:02d}.jpg"
                gradient_image(g, W, H)
                scene_files.append(build_scene(i, {"path": str(g), "kind": "image"}, dur, W, H, None))

    log(f"  कुल क्लिप: {clip_count} / सीन: {len(s['scenes'])} | टेक्स्ट कार्ड वाले सीन: {card_count}")
    if card_count > len(s["scenes"]) // 2:
        log("⚠ आधे से ज़्यादा सीन में फ़ोटो/क्लिप नहीं मिली: ऊपर की 🔎 लाइनें देखो, कोई स्रोत बंद है")
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    final = OUT / f"{args.channel}_{kind}_{stamp}.mp4"
    silent = WORK / "silent.mp4"
    voice_all = WORK / "voice_all.wav"
    concat(scene_files, silent)
    concat(voice_files, voice_all)
    mux(silent, voice_all, final)
    log(f"🎬 वीडियो तैयार: {final.name} ({duration(final):.0f} सेकंड)")

    thumb = None
    if kind == "long":
        thumb = OUT / f"{args.channel}_{kind}_{stamp}.jpg"
        ttext = s.get("thumbnail_text") or s["title"][:20]
        try:
            if not first_img:                       # कोई फ़ोटो/फ़्रेम नहीं मिला तो रंगीन कार्ड
                first_img = str(WORK / "thumb_bg.jpg")
                make_text_card(ttext, 1280, 720, first_img, 1)
                ttext = ""
            make_thumbnail(first_img, ttext, thumb)
            log(f"🖼 थंबनेल बन गया: {thumb.name} ({thumb.stat().st_size // 1024} KB)")
        except Exception as e:
            log(f"  [warn] थंबनेल नहीं बना: {e}")
            thumb = None

    if args.no_upload:
        log("(--no-upload: अपलोड छोड़ दिया)")
        return
    upload(ch, final, thumb, s, kind, credits)
    used_topics.setdefault(args.channel, []).append(s["title"])
    used_topics[args.channel] = used_topics[args.channel][-100:]
    USED.write_text(json.dumps(used_topics, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ फेल: {e}")
        sys.exit(1)
