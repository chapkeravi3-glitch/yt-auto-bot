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
import html
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path

import requests
from PIL import Image, ImageStat

UA = {"User-Agent": "yt-auto-bot/1.0 (automated video maker; contact via GitHub) Mozilla/5.0"}
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
def _image_ok(path, orient, relaxed=False):
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return False
    w, h = im.size
    if relaxed:
        if min(w, h) < 450 or max(w, h) < 800:
            return False
    elif min(w, h) < 700 or max(w, h) < 1000:
        return False
    small = im.resize((64, 64))
    st = ImageStat.Stat(small.convert("L"))
    if st.mean[0] < 28 or st.stddev[0] < 14:            # लगभग काली / सपाट फ़ोटो
        return False
    sat = ImageStat.Stat(small.convert("HSV")).mean[1] / 255.0
    if sat < (0.04 if relaxed else 0.10):                # काली-सफ़ेद / पुरानी फ़ोटो
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


# ---------------------------------------------------------------- नाम वाली चीज़ों की असली फ़ोटो (Wikipedia / Commons)
# खिलाड़ी, टीम, स्टेडियम, फ़िल्म, जगह जैसे नाम के लिए: मुफ़्त लाइसेंस वाली असली फ़ोटो, क्रेडिट के साथ
def wiki_images(name):
    d = _get_json("https://en.wikipedia.org/w/api.php",
                  {"action": "query", "format": "json", "generator": "search", "gsrsearch": name,
                   "gsrlimit": 2, "prop": "pageimages", "piprop": "original|thumbnail",
                   "pithumbsize": 1600})
    pages = sorted(((d or {}).get("query", {}).get("pages", {}) or {}).values(),
                   key=lambda p: p.get("index", 99))
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2]
    out = []
    for p in pages[:1]:
        title = p.get("title", "").lower()
        if words and not any(w in title for w in words):
            continue
        th = p.get("thumbnail") or {}
        orig = (p.get("original") or {}).get("source", "").lower()
        if not th or "logo" in orig or orig.endswith((".svg", ".gif")) or th.get("width", 0) < 900:
            continue
        out.append({"url": th["source"], "id": f"wp{p['pageid']}", "text": "",
                    "credit": f"फ़ोटो: Wikipedia/Wikimedia Commons ({p.get('title', '')})"})
    return out


def commons_entity_images(name):
    d = _get_json("https://commons.wikimedia.org/w/api.php",
                  {"action": "query", "format": "json", "generator": "search",
                   "gsrsearch": f"filetype:bitmap {name}", "gsrnamespace": 6, "gsrlimit": 12,
                   "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1600})
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2]
    pages = sorted(((d or {}).get("query", {}).get("pages", {}) or {}).values(),
                   key=lambda p: p.get("index", 99))
    out = []
    for p in pages:
        title = p.get("title", "").lower()
        if any(x in title for x in ("logo", "signature", "flag", "map", "poster", "stamp")):
            continue
        if words and not any(w in title for w in words):
            continue
        info = (p.get("imageinfo") or [{}])[0]
        u = info.get("thumburl") or info.get("url")
        if not u or info.get("width", 0) < 1200 or u.lower().endswith((".svg", ".gif")):
            continue
        meta = info.get("extmetadata", {})
        yr = re.search(r"(19|20)\d\d", (meta.get("DateTimeOriginal", {}) or {}).get("value", ""))
        if yr and int(yr.group(0)) < 2010:               # बहुत पुरानी फ़ोटो नहीं
            continue
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist", {}) or {}).get("value", ""))[:40]
        lic = (meta.get("LicenseShortName", {}) or {}).get("value", "")
        out.append({"url": u, "id": f"wc{p.get('pageid')}", "text": "",
                    "credit": f"फ़ोटो: {artist} / Wikimedia Commons {lic}".strip()})
    return out


def _try_entities(entities, work, i, used, orient):
    for name in entities[:2]:
        name = (name or "").strip()
        if not name:
            continue
        try:
            cands = wiki_images(name) + commons_entity_images(name)
        except Exception as e:
            log(f"    [warn] नाम से फ़ोटो खोजने में दिक्कत ({name}): {str(e)[:80]}")
            continue
        cands = [c for c in cands if c["id"] not in used]
        for c in cands[:6]:
            path = work / f"media_{i:02d}.jpg"
            if not _download(c["url"], path, max_mb=25):
                continue
            if not _image_ok(path, orient):
                path.unlink(missing_ok=True)
                continue
            used.add(c["id"])
            log(f"    ✔ असली फ़ोटो मिली ({name}) - {c['credit']}")
            return {"path": str(path), "kind": "image", "credit": c["credit"]}
    return None


# ---------------------------------------------------------------- वेब इमेज सर्च (इमेज डाउनलोडर ऐप जैसा)
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _domain(u):
    return re.sub(r"^https?://(www\.)?", "", u or "").split("/")[0]


def _bad_ext(u):
    return u.lower().split("?")[0].endswith((".svg", ".gif"))


def ddg_images(q, need_words=None):
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log("    [warn] ddgs लाइब्रेरी नहीं है: requirements.txt में 'ddgs' जोड़ो")
            return []
    res = []
    for attempt in range(2):
        try:
            res = list(DDGS().images(q, region="in-en", safesearch="moderate", max_results=30))
            break
        except Exception as e:
            log(f"    [warn] DuckDuckGo ({q}): {str(e)[:80]}")
            time.sleep(4)
    out = []
    for r in res:
        u = r.get("image") or ""
        w, h = r.get("width") or 0, r.get("height") or 0
        if not u or _bad_ext(u):
            continue
        if w and h and (max(w, h) < 800 or min(w, h) < 450):
            continue
        title = ((r.get("title") or "") + " " + u).lower()
        if need_words and not any(x in title for x in need_words):
            continue
        out.append({"url": u, "id": f"ddg{abs(hash(u))}", "text": title,
                    "credit": f"फ़ोटो: {_domain(r.get('url') or u)}", "headers": BROWSER_UA})
    return out


def bing_images(q, need_words=None):
    """Bing इमेज सर्च का पेज पढ़कर बड़ी फ़ोटो निकालता है (बिना key)"""
    try:
        r = requests.get("https://www.bing.com/images/search",
                         params={"q": q, "form": "HDRSC2", "first": 1,
                                 "qft": "+filterui:imagesize-large"},
                         headers={**BROWSER_UA, "Accept-Language": "en-IN,en;q=0.9"}, timeout=30)
    except Exception as e:
        log(f"    [warn] Bing ({q}): {str(e)[:80]}")
        return []
    if r.status_code != 200:
        log(f"    [warn] Bing ({q}): {r.status_code}")
        return []
    out = []
    for m in re.findall(r'class="iusc"[^>]*?\sm="([^"]+)"', r.text):
        try:
            d = json.loads(html.unescape(m))
        except Exception:
            continue
        u = d.get("murl") or ""
        if not u or _bad_ext(u):
            continue
        title = (re.sub(r"[\ue000\ue001]", "", d.get("t") or "") + " " + u).lower()
        if need_words and not any(x in title for x in need_words):
            continue
        out.append({"url": u, "id": f"bing{abs(hash(u))}", "text": title,
                    "credit": f"फ़ोटो: {_domain(d.get('purl') or u)}", "headers": BROWSER_UA})
    return out


def serper_images(q, need_words=None):
    """Google Images के नतीजे (serper.dev API)। GitHub के सर्वर पर भी चलता है, ऐप जैसी ही फ़ोटो मिलती हैं"""
    k = _key("SERPER_API_KEY")
    if not k:
        return []
    try:
        r = requests.post("https://google.serper.dev/images",
                          headers={"X-API-KEY": k, "Content-Type": "application/json"},
                          json={"q": q, "gl": "in", "hl": "en", "num": 30}, timeout=30)
    except Exception as e:
        log(f"    [warn] Serper ({q}): {str(e)[:80]}")
        return []
    if r.status_code != 200:
        log(f"    [warn] Serper {r.status_code}: {r.text[:120]}")
        return []
    out = []
    for it in r.json().get("images", []):
        u = it.get("imageUrl") or ""
        w, h = it.get("imageWidth") or 0, it.get("imageHeight") or 0
        if not u or _bad_ext(u):
            continue
        if w and h and (max(w, h) < 800 or min(w, h) < 450):
            continue
        title = ((it.get("title") or "") + " " + u).lower()
        if need_words and not any(x in title for x in need_words):
            continue
        out.append({"url": u, "id": f"srp{abs(hash(u))}", "text": title,
                    "credit": f"फ़ोटो: {it.get('source') or _domain(it.get('link') or u)}",
                    "headers": BROWSER_UA})
    return out


def web_images(q, need_words=None):
    """वेब से फ़ोटो: DuckDuckGo + Bing दोनों (ImageSearchMan जैसा)"""
    s = serper_images(q, need_words)
    a = ddg_images(q, need_words)
    b = bing_images(q, need_words)
    log(f"    वेब सर्च '{q}': Serper {len(s)}, DuckDuckGo {len(a)}, Bing {len(b)} फ़ोटो")
    mixed = list(s)
    for x, y in zip(a, b):
        mixed += [y, x]
    mixed += a[len(b):] + b[len(a):]
    return mixed


def _try_web(names, queries, anchors, work, i, used, orient):
    plans = []
    for name in names[:2]:
        name = (name or "").strip()
        if name:
            words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2]
            plans.append((f"{name} {anchors[0]}" if anchors else name, words))
            plans.append((name, words))
    for q in queries[:2]:
        plans.append((q, [a.lower() for a in anchors]))
    for q, words in plans:
        dl_fail = bad = 0
        for c in web_images(q, words)[:20]:
            if c["id"] in used:
                continue
            path = work / f"media_{i:02d}.jpg"
            if not _download(c["url"], path, max_mb=25, headers=c.get("headers")):
                dl_fail += 1
                continue
            if not _image_ok(path, orient, relaxed=True):
                bad += 1
                path.unlink(missing_ok=True)
                continue
            used.add(c["id"])
            log(f"    ✔ वेब सर्च से फ़ोटो मिली ({q}) - {c['credit']}")
            return {"path": str(path), "kind": "image", "credit": c["credit"]}
        log(f"    ✘ '{q}': डाउनलोड फेल {dl_fail}, फ़ोटो ख़राब/छोटी {bad}")
    return None


def openverse_images(q, orient):
    """Openverse: बिना key, खुले (CC) लाइसेंस की फ़ोटो - Pixabay/Pexels key न हो तब भी चलती है"""
    d = _get_json("https://api.openverse.org/v1/images/",
                  {"q": q[:100], "page_size": 20, "mature": "false",
                   "aspect_ratio": "tall" if orient == "portrait" else "wide"},
                  {"User-Agent": UA["User-Agent"]})
    out = []
    for r in (d or {}).get("results", []):
        u = r.get("url") or ""
        w, h = r.get("width") or 0, r.get("height") or 0
        if not u or _bad_ext(u) or (w and h and (max(w, h) < 1000 or min(w, h) < 600)):
            continue
        tags = " ".join(t.get("name", "") for t in (r.get("tags") or []))
        lic = f"CC {r.get('license', '')} {r.get('license_version', '')}".strip()
        out.append({"url": u, "id": f"ov{r.get('id')}", "text": f"{r.get('title', '')} {tags}".lower(),
                    "credit": f"फ़ोटो: {(r.get('creator') or '')[:40]} / {r.get('source', 'Openverse')} ({lic})"})
    return out


def diagnose():
    """रन की शुरुआत में बताता है कि कौन-कौन से स्रोत चालू हैं (लॉग में पहली लाइनें देखो)"""
    lines = [f"🔎 SERPER key: {'हाँ' if _key('SERPER_API_KEY') else 'नहीं'} | "
             f"PIXABAY key: {'हाँ' if _key('PIXABAY_API_KEY') else 'नहीं'} | "
             f"PEXELS key: {'हाँ' if _key('PEXELS_API_KEY') else 'नहीं'}"]
    try:
        import ddgs  # noqa: F401
        lines.append("🔎 ddgs लाइब्रेरी: हाँ")
    except ImportError:
        try:
            import duckduckgo_search  # noqa: F401
            lines.append("🔎 ddgs लाइब्रेरी: पुरानी duckduckgo_search है")
        except ImportError:
            lines.append("🔎 ddgs लाइब्रेरी: नहीं (requirements.txt में 'ddgs' जोड़ो)")
    try:
        lines.append(f"🔎 टेस्ट सर्च 'cricket stadium': Bing {len(bing_images('cricket stadium'))}, "
                     f"Serper {len(serper_images('cricket stadium'))}, "
                     f"Openverse {len(openverse_images('cricket stadium', 'landscape'))}, "
                     f"Wikipedia {len(wiki_images('Virat Kohli'))}")
    except Exception as e:
        lines.append(f"🔎 टेस्ट सर्च फेल: {str(e)[:100]}")
    return lines


VIDEO_SOURCES = (pexels_videos, pixabay_videos)
IMAGE_SOURCES = (pexels_images, pixabay_images, openverse_images)


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
               anchors=None, generic=None, entities=None, web_search=False):
    work = Path(work)
    anchors = [a for a in (anchors or []) if a]
    if entities:                                   # नाम वाली चीज़ हो तो पहले उसकी असली फ़ोटो
        m = _try_entities(entities, work, i, used, orient)
        if m:
            return m
        if web_search:
            m = _try_web(entities, queries or [], anchors, work, i, used, orient)
            if m:
                return m
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
    if web_search:                                 # आख़िरी कोशिश: वेब सर्च
        return _try_web([], qs, anchors, work, i, used, orient)
    return None
