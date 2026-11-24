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
import base64
import html
import io
import json
import os
import random
import re
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
from PIL import Image, ImageStat

UA = {"User-Agent": "yt-auto-bot/1.0 (automated video maker; contact via GitHub) Mozilla/5.0"}
_CACHE = {}


def log(*a):
    print(*a, flush=True)


def _i(x):
    """चौड़ाई/ऊँचाई कभी अंक, कभी लिखावट ("1920") आती है: हमेशा पूर्णांक बना देता है"""
    try:
        return int(float(x))
    except Exception:
        return 0


_DISABLED = set()      # जिस स्रोत की key गलत निकली, उसे इस रन में दोबारा नहीं बुलाते


def _disable(name, why):
    if name not in _DISABLED:
        _DISABLED.add(name)
        log(f"    ⚠ {name} की key गलत/बंद है ({why}); इस रन में इसे छोड़ रहे हैं")


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
                host = url.split('/')[2]
                if r.status_code in (400, 401, 403) and ("pixabay" in host or "pexels" in host):
                    _disable("pixabay" if "pixabay" in host else "pexels", f"{r.status_code}")
                else:
                    log(f"    [{host}] {r.status_code}: {r.text[:100]}")
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
    if not k or "pixabay" in _DISABLED:
        return []
    d = _get_json("https://pixabay.com/api/videos/",
                  {"key": k, "q": q[:100], "per_page": 20, "safesearch": "true"})
    out = []
    for h in (d or {}).get("hits", []):
        vids = h.get("videos", {})
        order = ("large", "medium") if orient == "portrait" else ("medium", "large")
        for name in order:
            v = vids.get(name) or {}
            if v.get("url") and _i(v.get("width")) >= 1280:
                out.append({"url": v["url"], "id": f"pxv{h['id']}", "text": h.get("tags", ""),
                            "credit": f"वीडियो: {h.get('user', '')} / Pixabay"})
                break
    return out


def pexels_videos(q, orient):
    k = _key("PEXELS_API_KEY")
    if not k or "pexels" in _DISABLED:
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
    if not k or "pixabay" in _DISABLED:
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
    if not k or "pexels" in _DISABLED:
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
        if not th or "logo" in orig or orig.endswith((".svg", ".gif")) or _i(th.get("width")) < 900:
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
        if not u or _i(info.get("width")) < 1200 or u.lower().endswith((".svg", ".gif")):
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
        w, h = _i(r.get("width")), _i(r.get("height"))
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
    if not k or "serper" in _DISABLED:
        return []
    try:
        r = requests.post("https://google.serper.dev/images",
                          headers={"X-API-KEY": k, "Content-Type": "application/json"},
                          json={"q": q, "gl": "in", "hl": "en", "num": 30}, timeout=30)
    except Exception as e:
        log(f"    [warn] Serper ({q}): {str(e)[:80]}")
        return []
    if r.status_code in (401, 403):
        _disable("serper", str(r.status_code))
        return []
    if r.status_code != 200:
        log(f"    [warn] Serper {r.status_code}: {r.text[:120]}")
        return []
    out = []
    for it in r.json().get("images", []):
        u = it.get("imageUrl") or ""
        w, h = _i(it.get("imageWidth")), _i(it.get("imageHeight"))
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
    def safe(fn):
        try:
            return fn(q, need_words)
        except Exception as e:
            log(f"    [warn] {fn.__name__}: {str(e)[:70]}")
            return []
    s, a, b = safe(serper_images), safe(ddg_images), safe(bing_images)
    log(f"    वेब सर्च '{q}': Serper {len(s)}, DuckDuckGo {len(a)}, Bing {len(b)} फ़ोटो")
    mixed = list(s)
    for x, y in zip(a, b):
        mixed += [y, x]
    mixed += a[len(b):] + b[len(a):]
    return mixed


def _try_web(names, queries, anchors, work, i, used, orient, ctx=None):
    plans = []
    fm = (ctx or {}).get("format")
    women = (ctx or {}).get("gender") == "women"
    for name in names[:2]:
        name = (name or "").strip()
        if name:
            words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2]
            if fm in ("test", "odi", "t20"):
                plans.append((f"{name} {fm}", words))
            if women and "women" not in name.lower():
                plans.append((f"{name} women", words))
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
        w, h = _i(r.get("width")), _i(r.get("height"))
        if not u or _bad_ext(u) or (w and h and (max(w, h) < 1000 or min(w, h) < 600)):
            continue
        tags = " ".join(t.get("name", "") for t in (r.get("tags") or []))
        lic = f"CC {r.get('license', '')} {r.get('license_version', '')}".strip()
        out.append({"url": u, "id": f"ov{r.get('id')}", "text": f"{r.get('title', '')} {tags}".lower(),
                    "credit": f"फ़ोटो: {(r.get('creator') or '')[:40]} / {r.get('source', 'Openverse')} ({lic})"})
    return out


def diagnose(library_root=None):
    """रन की शुरुआत में बताता है कि कौन-कौन से स्रोत चालू हैं (लॉग में पहली लाइनें देखो)"""
    lines = [f"🔎 SERPER key: {'हाँ' if _key('SERPER_API_KEY') else 'नहीं'} | "
             f"PIXABAY key: {'हाँ' if _key('PIXABAY_API_KEY') else 'नहीं'} | "
             f"PEXELS key: {'हाँ' if _key('PEXELS_API_KEY') else 'नहीं'}"]
    lines.append(f"🔎 अपनी फ़ोटो लाइब्रेरी: {library_stats(library_root)}")
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


# ---------------------------------------------------------------- अपनी फ़ोटो लाइब्रेरी (repo के photos/ फ़ोल्डर में)
# बनावट:  photos/<category>/<नाम-वाला-फ़ोल्डर>/फ़ोटो.jpg   जैसे photos/cricket/virat-kohli/..., photos/cricket/india-team/...
# photos/<category>/general/ में किसी भी टॉपिक की आम फ़ोटो रखो: कोई नाम न मिले तो उसी से उठेंगी
LIB_IMG = (".jpg", ".jpeg", ".png", ".webp")
LIB_VID = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
LIB_EXT = LIB_IMG + LIB_VID
LIB_STOP = {"the", "and", "for", "with"}
# ये आम शब्द फ़ोल्डर के नाम में हों तो मैचिंग में नहीं गिने जाते (जैसे india-team में सिर्फ़ 'india' गिना जाएगा)
LIB_GENERIC = {"team", "cricket", "national", "match", "matches", "player", "players", "film", "movie",
               "movies", "photo", "photos", "stadium", "world", "cup", "new",
               "women", "womens", "woman", "ladies", "female", "mahila", "wpl",
               "test", "tests", "odi", "t20", "t20i", "oneday"}

# फ़ोल्डर के नाम (या config के library_tags) से पता चलता है कि वह महिला/पुरुष और टेस्ट/वनडे/T20 का है
WOMEN_W = {"women", "womens", "woman", "ladies", "female", "mahila", "wpl",
           # महिला खिलाड़ियों के नाम: इनके फ़ोल्डर अपने-आप 'महिला' माने जाएँगे
           "harmanpreet", "smriti", "mandhana", "jemimah", "shafali", "renuka", "vastrakar",
           "mithali", "jhulan", "deepti", "richa", "lanning", "sciver", "ellyse", "amelia"}
FMT_W = {"test": "test", "tests": "test", "odi": "odi", "oneday": "odi", "t20": "t20", "t20i": "t20"}


def _folder_flags(name, tags=None):
    raw = set(re.findall(r"[a-z0-9]+", name.lower()))
    for t in (tags or {}).get(name, []):
        raw.add(str(t).lower())
    women = bool(raw & WOMEN_W)
    fmts = {FMT_W[w] for w in raw if w in FMT_W}
    return women, fmts


def _guard(name, ctx, block, tags, neutral_ok=False):
    """(चल सकता है?, नंबर बढ़ाना) लौटाता है। महिला/पुरुष और टेस्ट/वनडे/T20 में गड़बड़ न हो"""
    low = name.lower()
    if block and any(str(b).lower() in low for b in block):
        return False, 0.0
    women, fmts = _folder_flags(name, tags)
    g, fm = (ctx or {}).get("gender"), (ctx or {}).get("format")
    if g == "women" and not women and not (neutral_ok and not fmts):
        return False, 0.0
    if g == "men" and women:
        return False, 0.0
    if fm and fmts and fm not in fmts:
        return False, 0.0
    bonus = (0.5 if (g == "women" and women) else 0.0) + (0.5 if (fm and fm in fmts) else 0.0)
    return True, bonus


def _tok(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in LIB_STOP}


def library_stats(root):
    root = Path(root) if root else None
    if not root or not root.is_dir():
        return "फ़ोल्डर photos/ अभी नहीं है"
    n = sum(1 for p in root.rglob("*") if p.suffix.lower() in LIB_IMG)
    v = sum(1 for p in root.rglob("*") if p.suffix.lower() in LIB_VID)
    cats = sorted(d.name for d in root.iterdir() if d.is_dir())
    return f"{n} फ़ोटो, {v} वीडियो क्लिप, फ़ोल्डर: {', '.join(cats) if cats else '-'}"


def _lib_video_ok(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        w, h = [int(x) for x in out.split(",")[:2]]
        return max(w, h) >= 640
    except Exception:
        return False


def _lib_pick_file(files, used, want_video=False):
    """(फ़ाइल, 'image' या 'video') लौटाता है। want_video हो तो पहले क्लिप, वरना पहले फ़ोटो"""
    vids = [x for x in files if x.suffix.lower() in LIB_VID and str(x) not in used]
    imgs = [x for x in files if x.suffix.lower() in LIB_IMG and str(x) not in used]
    random.shuffle(vids)
    random.shuffle(imgs)
    order = [("video", vids), ("image", imgs)] if want_video else [("image", imgs), ("video", vids)]
    for kind, lst in order:
        for f in lst:
            if kind == "video":
                if _lib_video_ok(f):
                    return f, kind
                continue
            try:
                with Image.open(f) as im:
                    im.verify()
                with Image.open(f) as im:
                    if min(im.size) < 400:
                        continue
            except Exception:
                continue
            return f, kind
    return None, None


def _same(a, b):
    """दो शब्द लगभग एक जैसे हों (india/indian, shubam/shubman)"""
    if a == b:
        return True
    if len(a) >= 5 and len(b) >= 5:
        return (a.startswith(b) or b.startswith(a) or SequenceMatcher(None, a, b).ratio() >= 0.78
                or (a[:4] == b[:4] and abs(len(a) - len(b)) <= 2))
    return False


def _name_tokens(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower().replace("_", " "))
            if len(w) > 2 and w not in LIB_STOP and w not in LIB_GENERIC]


def _try_library(root, category, entities, queries, used, want_video=False,
                 ctx=None, block=None, tags=None, quiet=False):
    base = Path(root) / category
    if not base.is_dir():
        return None
    qtok = set()
    for s in list(entities or []) + list(queries or []):
        qtok |= set(_name_tokens(s))
    ents = [_name_tokens(e) for e in (entities or [])]
    ents = [e for e in ents if e]
    cands = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        if d.name.lower().startswith(("general", "misc", "common")):
            continue
        ok, bonus = _guard(d.name, ctx, block, tags)
        if not ok:
            continue
        order = _name_tokens(d.name.replace("-", " "))
        if not order:
            continue
        best = 0.0
        # 1) नाम (entity) के सारे शब्द फ़ोल्डर के नाम में मिलें (फ़ोल्डर का नाम लंबा हो तब भी चलेगा)
        for e in ents:
            found = [t for t in e if any(_same(t, x) for x in order)]
            if len(found) == len(e) and any(len(t) >= 4 for t in found):
                best = max(best, 1.0 + len(found) / len(order))
        # 2) फ़ोल्डर के लगभग सारे शब्द सीन के शब्दों में मिलें
        hit = [x for x in order if any(_same(x, t) for t in qtok)]
        if hit and (len(hit) / len(order) >= 0.75 or
                    (len(order) >= 2 and order[-1] in hit and len(order[-1]) >= 5)):
            best = max(best, len(hit) / len(order))
        if best > 0:
            files = [x for x in d.iterdir() if x.suffix.lower() in LIB_EXT]
            if files:
                cands.append((best + bonus, d, files))
    random.shuffle(cands)                               # बराबर नंबर वालों में बारी-बारी
    cands.sort(key=lambda x: round(x[0], 2), reverse=True)
    for _, d, files in cands:                           # सबसे सटीक फ़ोल्डर पहले
        f, kind = _lib_pick_file(files, used, want_video)
        if f:
            used.add(str(f))
            if not quiet:
                log(f"    ✔ अपनी लाइब्रेरी से {'क्लिप' if kind == 'video' else 'फ़ोटो'} ({d.name})")
            return {"path": str(f), "kind": kind, "credit": "", "library": True}
    # कोई नाम नहीं मिला: general फ़ोल्डर (general-test, general-women जैसे ख़ास फ़ोल्डर पहले)
    gens = []
    for d in sorted(p for p in base.iterdir() if p.is_dir() and p.name.lower().startswith("general")):
        ok, bonus = _guard(d.name, ctx, block, tags, neutral_ok=True)
        if ok:
            women, fmts = _folder_flags(d.name, tags)
            gens.append((bonus + (0.0 if (women or fmts) else -0.25), d))
    gens.sort(key=lambda x: x[0], reverse=True)
    for _, d in gens:
        f, kind = _lib_pick_file([x for x in d.iterdir() if x.suffix.lower() in LIB_EXT], used, want_video)
        if f:
            used.add(str(f))
            if not quiet:
                log(f"    ✔ अपनी लाइब्रेरी से आम {'क्लिप' if kind == 'video' else 'फ़ोटो'} ({d.name})")
            return {"path": str(f), "kind": kind, "credit": "", "library": True}
    return None


VIDEO_SOURCES = (pexels_videos, pixabay_videos)
IMAGE_SOURCES = (pexels_images, pixabay_images, openverse_images)


# ---------------------------------------------------------------- खोज के शब्द
def _query_variants(queries, anchors, generic, ctx=None):
    main = anchors[0] if anchors else ""
    women = (ctx or {}).get("gender") == "women"
    if women and main:
        main = f"women {main}"
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
    elif women:
        gen = [g if "women" in g.lower() else f"women {g}" for g in gen]
    random.shuffle(gen)
    seen, out = set(), []
    for q in qs[:6] + gen[:4]:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


# ---------------------------------------------------------------- मुख्य
def _pick_media_classic(queries, visual_en, orient, work, i, used, want_video=True,
               anchors=None, generic=None, entities=None, web_search=False,
               library=None, category="", ctx=None, block=None, tags=None):
    work = Path(work)
    anchors = [a for a in (anchors or []) if a]
    if library and category:                       # सबसे पहले अपनी लाइब्रेरी की फ़ोटो
        m = _try_library(library, category, entities, queries, used, want_video, ctx, block, tags)
        if m:
            return m
    if entities:                                   # नाम वाली चीज़ हो तो पहले उसकी असली फ़ोटो
        m = _try_entities(entities, work, i, used, orient)
        if m:
            return m
        if web_search:
            m = _try_web(entities, queries or [], anchors, work, i, used, orient, ctx)
            if m:
                return m
    anchors = [a for a in (anchors or []) if a]
    qs = _query_variants(queries, anchors, generic, ctx)
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
        return _try_web([], qs, anchors, work, i, used, orient, ctx)
    return None


# ================================================================ AI फ़ोटो-डायरेक्टर (Gemini देखकर चुनता है)
def _thumb_b64(path, size=448):
    im = Image.open(path).convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return base64.b64encode(buf.getvalue()).decode()


def ai_choose(cands, ai):
    """Gemini को उम्मीदवार फ़ोटो दिखाकर पूछता है कि सीन के लिए कौन-सी सही है।
    लौटाता है: चुनी हुई फ़ोटो का नंबर, -1 (कोई सही नहीं), या None (Gemini नहीं चला)"""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not cands:
        return None
    ctx = ai.get("ctx") or {}
    fm, gd = ctx.get("format"), ctx.get("gender")
    rules = []
    if fm == "test":
        rules.append("The video is about a TEST match: players must wear WHITE or cream clothes with a red ball. "
                     "Reject coloured-kit (ODI/T20) photos.")
    elif fm in ("odi", "t20"):
        rules.append("The video is about a limited-overs match: players wear COLOURED kits. "
                     "Reject photos of players in white Test clothes.")
    if ctx and gd == "women":
        rules.append("The video is about WOMEN's cricket: reject photos of men's teams or male players.")
    elif ctx and gd == "men":
        rules.append("The video is about MEN's cricket: reject photos of women's teams.")
    text = (f'You are the photo editor of a Hindi YouTube channel "{ai.get("channel", "")}". '
            f'Video title: {ai.get("title", "")}. Scene: {ai.get("scene", "")}. '
            f'People/teams/places this scene is about: {", ".join(ai.get("entities") or []) or "none"}.\n'
            "Below are candidate images (index, source/title text, then the image). "
            "Pick the ONE best ACCEPTABLE image for this scene.\n"
            "An image is acceptable when ALL of these are true:\n"
            "- it is a sharp, real photo (NOT a poster, collage, graphic, logo, screenshot, heavy watermark, "
            "big text overlay, blurry or very dark image);\n"
            "- it is about the same sport/topic as the video (for a cricket video ANY cricket photo is fine: "
            "stadium, crowd, players, bat, ball, celebration, trophy);\n"
            "- it obeys the hard rules listed below.\n"
            "The photo does NOT need to show the exact action described in the scene. The scene text is only "
            "a hint: prefer the closest match, but a general photo of the right topic is fine.\n"
            "For a named person or team: prefer a photo whose title/source text names them. If there is none, "
            "a generic photo of the same sport is acceptable, but never one whose text clearly names someone else. "
            "Never try to recognise people from their faces.\n"
            "Answer -1 ONLY if every image is unrelated to the topic, breaks a hard rule, or is unusable. "
            "If two images are equally good, pick the lower index.\n"
            + "".join(f"- {r}\n" for r in rules)
            + 'Return ONLY JSON: {"best": <index or -1>, "reason": "<short English reason>"}')
    parts = [{"text": text}]
    for k, c in enumerate(cands):
        parts.append({"text": f"Image {k}: {str(c.get('label', ''))[:160]}"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": _thumb_b64(c["path"])}})
    for model in (ai.get("models") or ["gemini-flash-latest"])[:3]:
        think = bool(re.search(r"gemini-(2\.5|3)", model)) and "pro" not in model
        for attempt in range(3):
            gen = {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 300}
            if think:
                gen["thinkingConfig"] = {"thinkingBudget": 0}
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": key}, json={"contents": [{"parts": parts}], "generationConfig": gen}, timeout=120)
            except Exception as e:
                log(f"    [warn] Gemini फ़ोटो-जाँच नेटवर्क: {str(e)[:60]}")
                time.sleep(5)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(8 * (attempt + 1))
                continue
            if r.status_code == 404:
                break
            if r.status_code == 400 and think and "think" in r.text.lower():
                think = False
                continue
            if r.status_code != 200:
                log(f"    [warn] Gemini फ़ोटो-जाँच {r.status_code}: {r.text[:100]}")
                break
            try:
                t = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
                d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip()))
                best = int(d.get("best", -1))
                log(f"    🤖 Gemini ने चुना: {best} ({str(d.get('reason', ''))[:90]})")
                time.sleep(1.5)
                return best if 0 <= best < len(cands) else -1
            except Exception as e:
                log(f"    [warn] Gemini जवाब समझ नहीं आया: {str(e)[:60]}")
                time.sleep(3)
    return None


def _ai_pick(queries, entities, orient, work, i, used, anchors, generic, web_search,
             library, category, ctx, block, tags, ai):
    """कई जगह से उम्मीदवार फ़ोटो जुटाता है, फिर Gemini से सही वाली चुनवाता है"""
    work = Path(work)
    cands, tried = [], [0]

    def add(url, label, credit, headers=None, cid=None):
        cid = cid or url
        if len(cands) >= 9 or tried[0] >= 18 or cid in used or any(c["id"] == cid for c in cands):
            return
        tried[0] += 1
        path = work / f"cand_{i:02d}_{len(cands)}.jpg"
        if not _download(url, path, max_mb=25, headers=headers):
            return
        if not _image_ok(path, orient, relaxed=True):
            path.unlink(missing_ok=True)
            return
        cands.append({"path": str(path), "label": label, "credit": credit, "id": cid})

    # 1) पहले सिर्फ़ अपनी लाइब्रेरी (4 तक): Gemini को पसंद आए तो बाहर कहीं नहीं जाते
    if library and category:
        tmp, lib = set(used), []
        for _ in range(4):
            m = _try_library(library, category, entities, queries, tmp, False, ctx, block, tags, quiet=True)
            if not m or m["kind"] != "image":
                break
            lib.append({"path": m["path"], "credit": "", "id": m["path"],
                        "label": "from our own library, folder: " + Path(m["path"]).parent.name})
        if lib:
            pick = ai_choose(lib, ai)
            if pick is None:                           # Gemini नहीं चला: लाइब्रेरी की पहली लो
                pick = 0
                log("    (Gemini की जाँच नहीं हो सकी, लाइब्रेरी की पहली फ़ोटो ली)")
            if pick >= 0:
                c = lib[pick]
                used.add(c["id"])
                log(f"    ✔ अपनी लाइब्रेरी से फ़ोटो पक्की ({Path(c['path']).parent.name})")
                return {"path": c["path"], "kind": "image", "credit": "", "library": True}
            log("    लाइब्रेरी की फ़ोटो इस सीन से मेल नहीं खाईं, अब बाहर से खोज रहे हैं")
    # 2) नाम वाली चीज़ें: Wikipedia/Commons, फिर वेब
    fm = (ctx or {}).get("format")
    women = (ctx or {}).get("gender") == "women"
    for name in (entities or [])[:2]:
        name = (name or "").strip()
        if not name:
            continue
        words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2]
        try:
            for c in wiki_images(name)[:1] + commons_entity_images(name)[:2]:
                add(c["url"], f"{name} - {c['credit']}", c["credit"], None, c["id"])
            if web_search:
                plans = []
                if women and "women" not in name.lower():
                    plans.append(f"{name} women")
                if fm in ("test", "odi", "t20"):
                    plans.append(f"{name} {fm} match")
                plans.append(name)
                for q in plans[:2]:
                    for c in web_images(q, words)[:4]:
                        add(c["url"], c["text"] or q, c["credit"], c.get("headers"), c["id"])
        except Exception as e:
            log(f"    [warn] नाम से फ़ोटो जुटाने में दिक्कत ({name}): {str(e)[:80]}")
    # 3) सीन के खोज-शब्दों से (वेब + स्टॉक)
    if len(cands) < 6:
        for q in _query_variants(queries, anchors, generic, ctx)[:4]:
            try:
                if web_search:
                    for c in web_images(q, [a.lower() for a in anchors] or None)[:3]:
                        add(c["url"], c["text"] or q, c["credit"], c.get("headers"), c["id"])
                for src in IMAGE_SOURCES:
                    for c in src(q, orient):
                        if _relevant(c, anchors, q):
                            add(c["url"], (c.get("text") or q)[:150], c["credit"], None, c["id"])
                            if len(cands) >= 9:
                                break
            except Exception as e:
                log(f"    [warn] खोज में दिक्कत ({q}): {str(e)[:60]}")
            if len(cands) >= 6:
                break
    def fallback():
        """कुछ भी जँचा नहीं: पहले स्टेडियम की फ़ोटो, फिर general की, वरना टेक्स्ट कार्ड"""
        p = _place_fallback(library, category, work, i, used, orient, ctx, block, tags, ai, web_search)
        if p:
            return p
        if library and category:
            g = _try_library(library, category, [], [], used, False, ctx, block, tags, quiet=True)
            if g and g["kind"] == "image":
                used.add(g["path"])
                log("    ✔ आख़िरी बचाव: general फ़ोल्डर की फ़ोटो (बिना जाँच)")
                return {"path": g["path"], "kind": "image", "credit": "", "library": True}
        return None

    if not cands:
        log("    ✘ कोई उम्मीदवार फ़ोटो मिली ही नहीं")
        return fallback()
    pick = ai_choose(cands, ai)
    if pick is None:                                   # Gemini नहीं चला: पहली (लाइब्रेरी वाली) लो
        pick = 0
        log("    (Gemini की जाँच नहीं हो सकी, पहली फ़ोटो ली)")
    if pick == -1:
        log("    ✘ Gemini को कोई फ़ोटो सीन से मेल खाती नहीं लगी")
        return fallback()
    c = cands[pick]
    used.add(c["id"])
    return {"path": c["path"], "kind": "image", "credit": c["credit"], "library": bool(c.get("library"))}


def _place_fallback(library, category, work, i, used, orient, ctx, block, tags, ai, web_search):
    """कुछ न मिलने पर: अलग-अलग स्टेडियम (या चैनल के हिसाब से जगह) की फ़ोटो।
    पहले आपके 'stadium' फ़ोल्डर से, फिर Wikipedia/Commons से मशहूर जगहों की फ़ोटो, फिर वेब से"""
    folder = str((ai or {}).get("fallback_folder") or "").lower()
    places = list((ai or {}).get("fallback_places") or [])
    work = Path(work)
    # 1) अपनी लाइब्रेरी का stadium फ़ोल्डर (जितने तरह के स्टेडियम डालोगे, उतनी विविधता)
    if folder and library and category:
        base = Path(library) / category
        if base.is_dir():
            files = []
            for d in base.iterdir():
                if d.is_dir() and folder in d.name.lower() and _guard(d.name, ctx, block, tags, True)[0]:
                    files += [x for x in d.iterdir() if x.suffix.lower() in LIB_IMG]
            f, kind = _lib_pick_file(files, used)
            if f:
                used.add(str(f))
                log(f"    ✔ आख़िरी बचाव: {folder} फ़ोल्डर की फ़ोटो ({f.parent.name})")
                return {"path": str(f), "kind": "image", "credit": "", "stadium": True}
    # 2) मशहूर स्टेडियमों की मुफ़्त लाइसेंस वाली फ़ोटो
    random.shuffle(places)
    for name in places[:5]:
        try:
            cs = wiki_images(name)[:1] + commons_entity_images(name)[:2]
            if not cs and web_search:
                cs = web_images(f"{name} stadium", [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 3] or None)[:3]
        except Exception as e:
            log(f"    [warn] {name}: {str(e)[:60]}")
            continue
        for c in cs:
            if c["id"] in used:
                continue
            path = work / f"place_{i:02d}.jpg"
            if not _download(c["url"], path, max_mb=25, headers=c.get("headers")):
                continue
            if not _image_ok(path, orient, relaxed=True):
                path.unlink(missing_ok=True)
                continue
            used.add(c["id"])
            log(f"    ✔ आख़िरी बचाव: {name} की फ़ोटो - {c['credit']}")
            return {"path": str(path), "kind": "image", "credit": c["credit"], "stadium": True}
    return None


def _frame_ok(path, work, i, ai, label):
    """वीडियो क्लिप का एक फ़्रेम Gemini को दिखाकर पूछता है कि वह सीन के लायक है या नहीं"""
    fr = Path(work) / f"fc_{i:02d}.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(path),
                        "-frames:v", "1", "-q:v", "3", str(fr)], timeout=60)
        if not fr.exists():
            return True
        r = ai_choose([{"path": str(fr), "label": label}], ai)
        return r != -1
    except Exception:
        return True
    finally:
        fr.unlink(missing_ok=True)


def _stock_video(qs, anchors, orient, work, i, used, ai, ctx):
    for q in qs:
        cands = []
        for src in VIDEO_SOURCES:
            try:
                cands += src(q, orient)
            except Exception as e:
                log(f"    [warn] {src.__name__}: {str(e)[:80]}")
        cands = [c for c in cands if c["id"] not in used and _relevant(c, anchors, q)]
        random.shuffle(cands)
        for c in cands[:3]:
            path = Path(work) / f"media_{i:02d}.mp4"
            if not _download(c["url"], path, max_mb=80) or not _video_ok(path):
                path.unlink(missing_ok=True)
                continue
            if ctx and not _frame_ok(path, work, i, ai, c.get("text") or q):
                log("    ✘ क्लिप सीन/फ़ॉर्मेट से मेल नहीं खाई, अगली देखते हैं")
                path.unlink(missing_ok=True)
                continue
            used.add(c["id"])
            log(f"    ✔ क्लिप मिली ({q}) - {c['credit']}")
            return {"path": str(path), "kind": "video", "credit": c["credit"]}
    return None


def pick_media(queries, visual_en, orient, work, i, used, want_video=True,
               anchors=None, generic=None, entities=None, web_search=False,
               library=None, category="", ctx=None, block=None, tags=None, ai=None):
    ai_on = bool(ai and ai.get("enabled") and os.environ.get("GEMINI_API_KEY", "").strip())
    if not ai_on:                                      # AI बंद हो तो पुराना तरीका
        return _pick_media_classic(queries, visual_en, orient, work, i, used, want_video, anchors, generic,
                                   entities, web_search, library, category, ctx, block, tags)
    anchors = [a for a in (anchors or []) if a]
    if want_video:
        if library and category:                       # अपनी लाइब्रेरी की क्लिप
            m = _try_library(library, category, entities, queries, used, True, ctx, block, tags)
            if m and m["kind"] == "video":
                return m
            if m:
                used.discard(m["path"])
        qs = _query_variants(queries, anchors, generic, ctx)
        m = _stock_video(qs, anchors, orient, work, i, used, ai, ctx)
        if m:
            return m
    return _ai_pick(queries, entities, orient, work, i, used, anchors, generic, web_search,
                    library, category, ctx, block, tags, ai)
