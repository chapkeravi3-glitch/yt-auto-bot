"""
image_finder.py
लाइसेंस वाली (फ्री) फ़ोटो और वीडियो क्लिप ढूँढने वाला मॉड्यूल।

सोर्स (बिना key के): Openverse, Wikimedia Commons, NASA
सोर्स (key हो तो):   Pixabay (फ़ोटो+वीडियो), Pexels (फ़ोटो+वीडियो)
सबसे सही फ़ोटो चुनने के लिए CLIP (ओपन-सोर्स AI) इस्तेमाल होता है।
"""
import io
import os
import re
import random
import requests
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

UA = {"User-Agent": "yt-auto-bot/1.0 (educational project; contact: github)"}
TIMEOUT = 25
MAX_CAND = 24          # एक सीन के लिए CLIP को कितनी फ़ोटो दिखानी हैं
MIN_SIDE = 600         # इससे छोटी फ़ोटो रिजेक्ट
DISABLED = set()       # जिस सोर्स की लिमिट पूरी हो गई, इस रन में बंद


# ---------------------------------------------------------------- helpers
def _get_json(url, source, params=None, headers=None):
    if source in DISABLED:
        return None
    try:
        r = requests.get(url, params=params, headers=headers or UA, timeout=TIMEOUT)
        if r.status_code == 429:
            print(f"  [warn] {source}: लिमिट पूरी, इस रन में बंद")
            DISABLED.add(source)
            return None
        if r.status_code in (400, 401, 403):
            # key गलत/बंद है। URL में key होती है, इसलिए URL लॉग में नहीं छापते
            print(f"  [warn] {source}: key या रिक्वेस्ट गलत ({r.status_code}) "
                  f"{r.text[:80]!r}, इस रन में बंद")
            DISABLED.add(source)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        msg = re.sub(r"key=[^&\s]+", "key=***", str(e))
        print(f"  [warn] {source}: {msg[:120]}")
        return None


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ---------------------------------------------------------------- sources
def _openverse(q, n=8):
    d = _get_json("https://api.openverse.org/v1/images/", "openverse", params={
        "q": q, "page_size": n, "license_type": "commercial,modification",
        "extension": "jpg"})
    out = []
    for r in (d or {}).get("results", []):
        if not r.get("url"):
            continue
        lic = f'{(r.get("license") or "").upper()} {r.get("license_version") or ""}'.strip()
        out.append({
            "kind": "image", "source": "openverse", "url": r["url"],
            "thumb": r.get("thumbnail") or r["url"],
            "w": r.get("width"), "h": r.get("height"),
            "credit": f'"{r.get("title") or "photo"}" by {r.get("creator") or "unknown"} '
                      f'({lic}) {r.get("foreign_landing_url") or ""}'.strip(),
        })
    return out


def _wikimedia(q, n=8):
    d = _get_json("https://commons.wikimedia.org/w/api.php", "wikimedia", params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": n,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": 640})
    out = []
    pages = ((d or {}).get("query") or {}).get("pages", {})
    for p in pages.values():
        info = (p.get("imageinfo") or [None])[0]
        if not info or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if min(info.get("width", 0), info.get("height", 0)) < MIN_SIDE:
            continue
        meta = info.get("extmetadata") or {}
        artist = _strip_html((meta.get("Artist") or {}).get("value"))[:60] or "unknown"
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        out.append({
            "kind": "image", "source": "wikimedia", "url": info["url"],
            "thumb": info.get("thumburl") or info["url"],
            "w": info.get("width"), "h": info.get("height"),
            "credit": f'{p.get("title", "").replace("File:", "")} by {artist} '
                      f'({lic}) via Wikimedia Commons',
        })
    return out


def _nasa(q, n=4):
    d = _get_json("https://images-api.nasa.gov/search", "nasa",
                  params={"q": q, "media_type": "image", "page_size": n})
    out = []
    items = ((d or {}).get("collection") or {}).get("items", [])
    for it in items[:n]:
        data = (it.get("data") or [{}])[0]
        nid = data.get("nasa_id")
        links = it.get("links") or []
        if not nid or not links:
            continue
        q_id = requests.utils.quote(nid)
        out.append({
            "kind": "image", "source": "nasa",
            "url": f"https://images-assets.nasa.gov/image/{q_id}/{q_id}~medium.jpg",
            "thumb": links[0]["href"], "w": None, "h": None,
            "credit": f'{data.get("title", "NASA image")} - NASA',
        })
    return out


def _pixabay(q, orient, want_video, n=8):
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    out = []
    d = _get_json("https://pixabay.com/api/", "pixabay", params={
        "key": key, "q": q[:100], "image_type": "photo", "per_page": n,
        "safesearch": "true", "min_width": 900,
        "orientation": "vertical" if orient == "portrait" else "horizontal"})
    for h in (d or {}).get("hits", []):
        out.append({
            "kind": "image", "source": "pixabay",
            "url": h.get("largeImageURL") or h.get("webformatURL"),
            "thumb": h.get("webformatURL"),
            "w": h.get("imageWidth"), "h": h.get("imageHeight"),
            "credit": f'Pixabay - {h.get("user", "")} - {h.get("pageURL", "")}',
        })
    if want_video:
        d = _get_json("https://pixabay.com/api/videos/", "pixabay", params={
            "key": key, "q": q[:100], "per_page": n, "safesearch": "true"})
        for h in (d or {}).get("hits", []):
            v = (h.get("videos") or {})
            pick = v.get("medium") if (v.get("medium") or {}).get("url") else v.get("small")
            if not pick or not pick.get("url"):
                continue
            out.append({
                "kind": "video", "source": "pixabay", "url": pick["url"],
                "thumb": pick.get("thumbnail"), "w": pick.get("width"), "h": pick.get("height"),
                "credit": f'Pixabay video - {h.get("user", "")} - {h.get("pageURL", "")}',
            })
    return [c for c in out if c.get("url") and c.get("thumb")]


def _pexels(q, orient, want_video, n=8):
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    hdr = {"Authorization": key, **UA}
    out = []
    d = _get_json("https://api.pexels.com/v1/search", "pexels", headers=hdr,
                  params={"query": q, "per_page": n, "orientation": orient})
    for p in (d or {}).get("photos", []):
        src = p.get("src", {})
        out.append({
            "kind": "image", "source": "pexels", "url": src.get("large2x") or src.get("large"),
            "thumb": src.get("medium"), "w": p.get("width"), "h": p.get("height"),
            "credit": f'Pexels - {p.get("photographer", "")} - {p.get("url", "")}',
        })
    if want_video:
        d = _get_json("https://api.pexels.com/videos/search", "pexels", headers=hdr,
                      params={"query": q, "per_page": n, "orientation": orient})
        for v in (d or {}).get("videos", []):
            files = [f for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and f.get("width")]
            if not files:
                continue
            best = min(files, key=lambda f: abs(f["width"] - 1280))
            out.append({
                "kind": "video", "source": "pexels", "url": best["link"],
                "thumb": v.get("image"), "w": best.get("width"), "h": best.get("height"),
                "credit": f'Pexels video - {(v.get("user") or {}).get("name", "")} - {v.get("url", "")}',
            })
    return [c for c in out if c.get("url") and c.get("thumb")]


def search_all(q, orient, want_video):
    c = []
    c += _wikimedia(q)
    c += _openverse(q)
    c += _pixabay(q, orient, want_video)
    c += _pexels(q, orient, want_video)
    c += _nasa(q)
    return c


# ---------------------------------------------------------------- ranking
_clip = None


def _load_clip():
    global _clip
    if _clip is None:
        try:
            from sentence_transformers import SentenceTransformer
            _clip = SentenceTransformer("clip-ViT-B-32")
            print("  CLIP मॉडल लोड हो गया")
        except Exception as e:
            print(f"  [warn] CLIP नहीं चला ({str(e)[:100]}), सादा रैंकिंग इस्तेमाल होगी")
            _clip = False
    return _clip


def _fetch_thumb(c):
    try:
        r = requests.get(c["thumb"], headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((448, 448))
        return im
    except Exception:
        return None


def _round_robin(cands):
    groups = {}
    for c in cands:
        groups.setdefault(c["source"], []).append(c)
    out = []
    while any(groups.values()):
        for k in list(groups):
            if groups[k]:
                out.append(groups[k].pop(0))
    return out


def rank(cands, text, orient, want_video):
    with ThreadPoolExecutor(8) as ex:
        thumbs = list(ex.map(_fetch_thumb, cands))
    pairs = [(c, im) for c, im in zip(cands, thumbs) if im is not None]
    if not pairs:
        return []
    model = _load_clip()
    if model:
        try:
            from sentence_transformers import util
            img_emb = model.encode([im for _, im in pairs], batch_size=16,
                                   convert_to_tensor=True, show_progress_bar=False)
            txt_emb = model.encode([text[:200]], convert_to_tensor=True,
                                   show_progress_bar=False)
            sims = util.cos_sim(txt_emb, img_emb)[0].tolist()
        except Exception as e:
            print(f"  [warn] CLIP स्कोर नहीं बना: {str(e)[:100]}")
            sims = [random.random() * 0.1 for _ in pairs]
    else:
        sims = [random.random() * 0.1 for _ in pairs]
    scored = []
    for (c, im), s in zip(pairs, sims):
        bonus = 0.0
        portrait = im.height > im.width
        if (orient == "portrait") == portrait:
            bonus += 0.02
        if want_video and c["kind"] == "video":
            bonus += 0.03
        scored.append((s + bonus, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ---------------------------------------------------------------- download
def download(c, work, idx):
    work = str(work)
    if c["kind"] == "video":
        path = os.path.join(work, f"media_{idx:02d}.mp4")
        try:
            with requests.get(c["url"], headers=UA, timeout=60, stream=True) as r:
                r.raise_for_status()
                size = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        size += len(chunk)
                        if size > 80 * (1 << 20):
                            raise RuntimeError("वीडियो बहुत बड़ा")
                        f.write(chunk)
            return path
        except Exception as e:
            print(f"  [warn] वीडियो डाउनलोड नहीं हुआ: {str(e)[:100]}")
            return None
    path = os.path.join(work, f"media_{idx:02d}.jpg")
    for u in (c["url"], c["thumb"]):
        try:
            r = requests.get(u, headers=UA, timeout=40)
            r.raise_for_status()
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
            if min(im.size) < MIN_SIDE:
                continue
            im.save(path, quality=92)
            return path
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- main API
def pick_media(queries, describe, orient, work, idx, used, want_video=False):
    """सीन के लिए सबसे सही फ़ोटो/क्लिप चुनकर डाउनलोड करता है।
    लौटाता है {"path","kind","credit"} या None"""
    qs = []
    for q in queries or []:
        q = (q or "").strip()
        if q and q not in qs:
            qs.append(q)
    if not qs:
        return None
    cands = []
    for q in qs[:2]:
        cands += search_all(q, orient, want_video)
    if len(cands) < 4:  # कम मिला तो खोज को छोटा करके दोबारा
        short_q = " ".join(qs[0].split()[:2])
        cands += search_all(short_q, orient, want_video)
    seen, uniq = set(), []
    for c in cands:
        if not c.get("url") or c["url"] in seen or c["url"] in used:
            continue
        seen.add(c["url"])
        uniq.append(c)
    if not uniq:
        return None
    uniq = _round_robin(uniq)[:MAX_CAND]
    ranked = rank(uniq, describe or " ".join(qs), orient, want_video)
    for score, c in ranked[:6]:
        p = download(c, work, idx)
        if p:
            used.add(c["url"])
            print(f"  चुनी: {c['source']} {c['kind']} (स्कोर {score:.3f})")
            return {"path": p, "kind": c["kind"], "credit": c.get("credit", "")}
    return None
