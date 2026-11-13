#!/usr/bin/env python3
"""
main.py - यूट्यूब ऑटो वीडियो बॉट (शॉर्ट्स + लॉन्ग)

चलाने का तरीका:
    python main.py --channel fact --type short
    python main.py --channel movies --type long
    python main.py --channel fact --type short --no-upload   # सिर्फ़ टेस्ट, अपलोड नहीं
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
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


def log(*a):
    print(*a, flush=True)


def sh(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} फेल: {p.stderr[-900:]}")
    return p.stdout


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
    for attempt in range(5):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.9,
                                           "responseMimeType": "application/json",
                                           "maxOutputTokens": max_tokens}},
                timeout=240)
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


def make_script(ch, kind, models, recent):
    if kind == "short":
        length = ("A YouTube Short. 6 to 8 scenes. Total narration 95-115 words "
                  "(about 40-50 seconds when spoken).")
        max_tokens = 8192
    else:
        length = ("A long YouTube video of about 8 minutes. 24 to 28 scenes, each narration "
                  "40-55 words. Total 1100-1400 words. Clear intro, well-structured body, short outro.")
        max_tokens = 24000
    prompt = f"""You are the scriptwriter of a Hindi YouTube channel called "{ch['name']}".
Channel niche: {ch['niche']}
Pick ONE fresh, specific topic from these themes: {', '.join(ch['themes'])}.
Do NOT repeat or closely resemble these recent topics: {json.dumps(recent[-40:], ensure_ascii=False)}.

Write {length}
Language of narration/title/description: simple spoken Hindi in Devanagari.

Rules:
- Use only true, well-known, verifiable facts. If unsure, leave it out. Never invent numbers, quotes or gossip.
- Scene 1 must be a strong hook (surprising question or fact).
- The last scene: one short line asking viewers to subscribe.
- For each scene give:
  "narration" (Hindi),
  "caption" (Hindi, max 6 words, shown on screen),
  "image_queries" (2-3 English search phrases built from CONCRETE nouns: real places, animals, objects, famous people, events. No abstract words),
  "visual_en" (one English sentence describing the ideal photo for this scene),
  "prefer_video" (true if motion helps, else false).
- "title": Hindi, under 70 characters, curiosity-driven but honest.
- "description": 2-3 Hindi lines.
- "tags": 8-12 tags mixing Hindi and English.
- "hashtags": 4-5 hashtags like "#Facts".
- "thumbnail_text": 2-4 Hindi words, punchy.

Return ONLY valid JSON in this shape:
{{"title":"","description":"","tags":[],"hashtags":[],"thumbnail_text":"",
"scenes":[{{"narration":"","caption":"","image_queries":["",""],"visual_en":"","prefer_video":false}}]}}"""
    for attempt in range(3):
        s = gemini_json(prompt, models, max_tokens)
        scenes = [x for x in s.get("scenes", []) if x.get("narration")]
        if len(scenes) >= (4 if kind == "short" else 10) and s.get("title"):
            s["scenes"] = scenes
            return s
        log("  स्क्रिप्ट अधूरी आई, दोबारा माँग रहे हैं")
    raise RuntimeError("स्क्रिप्ट ठीक से नहीं बनी")


# ================================================================ आवाज़
async def _tts(text, voice, path):
    await edge_tts.Communicate(text, voice, rate="+0%").save(str(path))


def tts(text, voice, path):
    for a in range(4):
        try:
            asyncio.run(_tts(text, voice, path))
            if Path(path).exists() and Path(path).stat().st_size > 1000:
                return
        except Exception as e:
            log(f"  आवाज़ बनाने में दिक्कत ({str(e)[:80]}), दोबारा")
        time.sleep(3 * (a + 1))
    raise RuntimeError("आवाज़ नहीं बनी")


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


def make_thumbnail(img_path, text, out):
    im = cover(Image.open(img_path).convert("RGB"), 1280, 720)
    im = ImageEnhance.Brightness(im).enhance(0.6)
    d = ImageDraw.Draw(im)
    if CAPTIONS_OK:
        font = ImageFont.truetype(FONT, 120, layout_engine=ImageFont.Layout.RAQM)
        lines = wrap(d, text, font, 1100)[:3]
        y = (720 - 150 * len(lines)) // 2
        for ln in lines:
            w = d.textlength(ln, font=font)
            d.text(((1280 - w) / 2, y), ln, font=font, fill=(255, 235, 59),
                   stroke_width=6, stroke_fill=(0, 0, 0))
            y += 150
    im.save(out, quality=88)


# ================================================================ वीडियो बनाना (ffmpeg)
def build_scene(i, media, audio, dur, W, H, cap_png):
    out = WORK / f"scene_{i:02d}.mp4"
    frames = int(dur * 30) + 2
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if media["kind"] == "video":
        cmd += ["-stream_loop", "-1", "-i", media["path"]]
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,fps=30,format=yuv420p")
    else:
        big = WORK / f"big_{i:02d}.jpg"
        cover(Image.open(media["path"]).convert("RGB"), int(W * 1.25), int(H * 1.25)).save(big, quality=92)
        cmd += ["-i", str(big)]
        z = ("min(1+0.0005*on,1.2)" if i % 2 == 0 else "max(1.2-0.0005*on,1)")
        base = (f"[0:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={W}x{H}:fps=30,setsar=1,format=yuv420p")
    cmd += ["-i", str(audio)]
    if cap_png:
        cmd += ["-i", str(cap_png)]
        fc = base + "[bg];[bg][2:v]overlay=0:0:format=auto,format=yuv420p[v]"
    else:
        fc = base + "[v]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "1:a", "-af", "apad",
            "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-r", "30", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
            "-movflags", "+faststart", str(out)]
    sh(cmd)
    return out


def concat(scene_files, out):
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in scene_files))
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(lst), "-c", "copy", str(out)])


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
        desc += "\n\nफ़ोटो/वीडियो क्रेडिट:\n" + "\n".join(credits)
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
            log(f"   [warn] थंबनेल नहीं लगा (चैनल वेरिफाइड न हो तो ऐसा होता है): {str(e)[:120]}")
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
    s = make_script(ch, kind, models, recent)
    log(f"  टॉपिक: {s['title']} | सीन: {len(s['scenes'])}")

    voice = check_voice(ch.get("voice", "hi-IN-MadhurNeural"), ch.get("gender", "Male"))
    used_urls, credits, scene_files, first_img = set(), [], [], None
    for i, sc in enumerate(s["scenes"]):
        log(f"[सीन {i + 1}/{len(s['scenes'])}] {sc['narration'][:50]}...")
        audio = WORK / f"a_{i:02d}.mp3"
        tts(sc["narration"], voice, audio)
        dur = duration(audio) + 0.3

        media = IF.pick_media(sc.get("image_queries", []), sc.get("visual_en", ""),
                              orient, WORK, i, used_urls, bool(sc.get("prefer_video")))
        if not media:
            log("  फ़ोटो नहीं मिली, सादा बैकग्राउंड लगा रहे हैं")
            g = WORK / f"grad_{i:02d}.jpg"
            gradient_image(g, W, H)
            media = {"path": str(g), "kind": "image", "credit": ""}
        elif media["credit"] and media["credit"] not in credits:
            credits.append(media["credit"])
        if first_img is None and media["kind"] == "image":
            first_img = media["path"]

        cap_png = None
        if CAPTIONS_OK and sc.get("caption"):
            cap_png = WORK / f"cap_{i:02d}.png"
            try:
                make_caption_png(sc["caption"], W, H, cap_png)
            except Exception as e:
                log(f"  [warn] कैप्शन नहीं बना: {e}")
                cap_png = None
        try:
            scene_files.append(build_scene(i, media, audio, dur, W, H, cap_png))
        except Exception as e:
            log(f"  [warn] सीन में दिक्कत, बिना कैप्शन दोबारा: {str(e)[:200]}")
            scene_files.append(build_scene(i, media, audio, dur, W, H, None))

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    final = OUT / f"{args.channel}_{kind}_{stamp}.mp4"
    concat(scene_files, final)
    log(f"🎬 वीडियो तैयार: {final.name} ({duration(final):.0f} सेकंड)")

    thumb = None
    if kind == "long" and first_img:
        thumb = OUT / f"{args.channel}_{kind}_{stamp}.jpg"
        try:
            make_thumbnail(first_img, s.get("thumbnail_text") or s["title"][:20], thumb)
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
