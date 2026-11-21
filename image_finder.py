#!/usr/bin/env python3
"""
image_finder.py - टॉपिक से जुड़ी HD फ़ोटो और वीडियो क्लिप ढूँढता है  [v2: सिर्फ़ टॉपिक से मिलती चीज़ें]

v2 में बदलाव:
  - हर खोज में चैनल का "anchor" शब्द (जैसे cricket) जुड़ता है, ताकि बस/लैपटॉप जैसी बेमतलब चीज़ें न आएँ
  - हर नतीजे के टैग/नाम में anchor होना ज़रूरी; वरना वह फ़ोटो/क्लिप छोड़ दी जाती है
  - कुछ न मिले तो चैनल की "generic_queries" (जैसे cricket stadium crowd) आज़माई जाती हैं
  - पुरानी काली-सफ़ेद फ़ोटो और Wikimedia की इतिहास वाली फ़ोटो अब नहीं आतीं

स्रोत: Pexels वीडियो/फ़ोटो (अगर PEXELS_API_KEY हो), Pixabay वीडियो/फ़ोटो (PIXABAY_API_KEY)
"""
import os
import random
import re
import subprocess
import time
from pathlib import Path

import requests
from PIL import Image, ImageStat

UA = {"User-Agent": "Mozilla/5.0 (yt-auto-bot)"}
_CACHE = {}


def log(*a):
    print(*a, flush=True)


def _key(name):
    return os.environ.get(name, "").strip()


def _get_json(url, params=None, headers=None):
    ck = (url, tuple(sorted((params or {}).items())))
    if ck in _CACHE:
        return _CACHE[ck]
    data = None
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=30)
            if r.status_code == 429:
                time.sleep(20)
                continue
            if r.status_code == 200:
                data = r.json()
            else:
                log(f"    [{url.split('/')[2]}] {r.status_code}: {r.text[:100]}")
            break
        except Exception as e:
            log(f"    [{url.split('/')[2]}] नेटवर्क दिक्कत: {str(e)[:80]}")
            time.sleep(2)
    _CACHE[ck] = data
    return data


def _download(url, path, max_mb=80, headers=None):
    try:
        with requests.get(url, stream=True, timeout=60, headers=headers or UA) as r:
            if r.status_code != 200:
                return False
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if size > max_mb * (1 << 20):
                        return False
                    f.write(chunk)
        return size > 20_000
    except Exception:
        return False


# ---------------------------------------------------------------- जाँच
def _image_ok(path, orient):
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return False
    w, h = im.size
    if min(w, h) < 700 or max(w, h) < 1000:
        return False
    small = im.resize((64, 64))
    st = ImageStat.Stat(small.convert("L"))
    if st.mean[0] < 28 or st.stddev[0] < 14:            # लगभग काली / सपाट फ़ोटो
        return False
    sat = ImageStat.Stat(small.convert("HSV")).mean[1] / 255.0
    if sat < 0.10:                                       # काली-सफ़ेद / पुरानी फ़ोटो
        return False
    return True


def _video_ok(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        w, h = [int(x) for x in out.split(",")[:2]]
        return max(w, h) >= 1000
    except Exception:
        return False


def _relevant(c, anchors, q):
    """नतीजे के टैग/नाम में anchor (या न हो तो खोज के शब्द) होना चाहिए"""
    text = (c.get("text") or "").lower()
    if not text:
        return not anchors
    if anchors:
        return any(a.lower() in text for a in anchors)
    words = [w[:5] for w in re.findall(r"[a-z]+", q.lower()) if len(w) > 3]
    return (not words) or any(w in text for w in words)


# ---------------------------------------------------------------- स्रोत
# cand = {"url", "id", "credit", "text"}
def pixabay_videos(q, orient):
    k = _key("PIXABAY_API_KEY")
    if not k:
        return []
    d = _get_json("https://pixabay.com/api/videos/",
                  {"key": k, "q": q[:100], "per_page": 20, "safesearch": "true"})
    out = []
    for h in (d or {}).get("hits", []):
        vids = h.get("videos", {})
        order = ("large", "medium") if orient == "portrait" else ("medium", "large")
        for name in order:
            v = vids.get(name) or {}
            if v.get("url") and v.get("width", 0) >= 1280:
                out.append({"url": v["url"], "id": f"pxv{h['id']}", "text": h.get("tags", ""),
                            "credit": f"वीडियो: {h.get('user', '')} / Pixabay"})
                break
    return out


def pexels_videos(q, orient):
    k = _key("PEXELS_API_KEY")
    if not k:
        return []
    d = _get_json("https://api.pexels.com/videos/search",
                  {"query": q, "per_page": 15, "orientation": orient, "size": "medium"},
                  {"Authorization": k, **UA})
    out = []
    for v in (d or {}).get("videos", []):
        files = [f for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("link")
                 and 1000 <= max(f.get("width") or 0, f.get("height") or 0) <= 2200]
        if not files:
            continue
        files.sort(key=lambda f: abs(max(f["width"], f["height"]) - 1920))
        out.append({"url": files[0]["link"], "id": f"pev{v['id']}",
                    "text": (v.get("url", "") + " " + " ".join(v.get("tags", []) or [])).replace("-", " "),
                    "credit": f"वीडियो: {v.get('user', {}).get('name', '')} / Pexels"})
    return out


def pixabay_images(q, orient):
    k = _key("PIXABAY_API_KEY")
    if not k:
        return []
    d = _get_json("https://pixabay.com/api/",
                  {"key": k, "q": q[:100], "image_type": "photo", "per_page": 20,
                   "orientation": "vertical" if orient == "portrait" else "horizontal",
                   "min_width": 1000 if orient == "landscape" else 0,
                   "min_height": 1000 if orient == "portrait" else 0,
                   "safesearch": "true", "order": "popular"})
    out = []
    for h in (d or {}).get("hits", []):
        u = h.get("largeImageURL")
        if u:
            out.append({"url": u, "id": f"pxi{h['id']}", "text": h.get("tags", ""),
                        "credit": f"फ़ोटो: {h.get('user', '')} / Pixabay"})
    return out


def pexels_images(q, orient):
    k = _key("PEXELS_API_KEY")
    if not k:
        return []
    d = _get_json("https://api.pexels.com/v1/search",
                  {"query": q, "per_page": 15, "orientation": orient, "size": "large"},
                  {"Authorization": k, **UA})
    out = []
    for p in (d or {}).get("photos", []):
        u = p.get("src", {}).get("large2x") or p.get("src", {}).get("large")
        if u:
            out.append({"url": u, "id": f"pei{p['id']}",
                        "text": (p.get("alt", "") + " " + p.get("url", "")).replace("-", " "),
                        "credit": f"फ़ोटो: {p.get('photographer', '')} / Pexels"})
    return out


VIDEO_SOURCES = (pexels_videos, pixabay_videos)
IMAGE_SOURCES = (pexels_images, pixabay_images)


# ---------------------------------------------------------------- खोज के शब्द
def _query_variants(queries, anchors, generic):
    main = anchors[0] if anchors else ""
    qs = []
    for q in queries or []:
        q = (q or "").strip()
        if not q:
            continue
        if main and not any(a.lower() in q.lower() for a in anchors):
            q = f"{main} {q}"
        qs.append(q)
        w = q.split()
        if len(w) > 3:                                   # छोटा रूप, anchor साथ रखकर
            qs.append(" ".join(w[:3]))
    gen = list(generic or [])
    if not gen and main:
        gen = [f"{main} match", f"{main} crowd", f"{main} action", f"{main} team"]
    random.shuffle(gen)
    seen, out = set(), []
    for q in qs[:6] + gen[:4]:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


# ---------------------------------------------------------------- मुख्य
def pick_media(queries, visual_en, orient, work, i, used, want_video=True,
               anchors=None, generic=None):
    work = Path(work)
    anchors = [a for a in (anchors or []) if a]
    qs = _query_variants(queries, anchors, generic)
    order = ("video", "image") if want_video else ("image", "video")
    for kind in order:
        sources = VIDEO_SOURCES if kind == "video" else IMAGE_SOURCES
        for q in qs:
            cands = []
            for src in sources:
                try:
                    cands += src(q, orient)
                except Exception as e:
                    log(f"    [warn] {src.__name__}: {str(e)[:80]}")
            cands = [c for c in cands if c["id"] not in used and _relevant(c, anchors, q)]
            if not cands:
                continue
            top = cands[:10]
            random.shuffle(top)
            for c in top[:3]:
                ext = ".mp4" if kind == "video" else ".jpg"
                path = work / f"media_{i:02d}{ext}"
                if not _download(c["url"], path, max_mb=80 if kind == "video" else 25):
                    continue
                ok = _video_ok(path) if kind == "video" else _image_ok(path, orient)
                if not ok:
                    path.unlink(missing_ok=True)
                    continue
                used.add(c["id"])
                log(f"    ✔ {'क्लिप' if kind == 'video' else 'फ़ोटो'} मिली ({q}) - {c['credit']}")
                return {"path": str(path), "kind": kind, "credit": c["credit"]}
        log(f"    {'क्लिप' if kind == 'video' else 'फ़ोटो'} नहीं मिली, दूसरा तरीका")
    return None
