"""Metacritic Metascore and User score lookup for Film Tagger.

Metacritic has no public API. This module uses the same backend.metacritic.com
JSON endpoints the official movie page calls (search + critic/user stats), and
falls back to the HTML page (JSON-LD, __NUXT_DATA__, visible score labels) if
those fail. No Rotten Tomatoes. Fail softly: callers get None / not_found, never
an uncaught network exception.

Polite: a shared ~1 request/second limiter. Callers should also cache results
(~30 days) so a film is not looked up on every open.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
ORIGIN = "https://www.metacritic.com"
REQUEST_GAP_SEC = 1.0

CRITIC_STATS = (
    "https://backend.metacritic.com/reviews/metacritic/critic/movies/"
    "{slug}/stats/web?componentName=critic-score-summary"
    "&componentDisplayName=Critic+Score+Summary"
    "&componentType=MetaScoreSummary"
)
USER_STATS = (
    "https://backend.metacritic.com/reviews/metacritic/user/movies/"
    "{slug}/stats/web?componentName=user-score-summary"
    "&componentDisplayName=User+Score+Summary"
    "&componentType=MetaScoreSummary"
)
SEARCH_URL = (
    "https://backend.metacritic.com/finder/metacritic/search/{query}/web"
    "?offset=0&limit=20&mcoTypeId=2"
    "&componentName=search&componentType=SearchResult"
)

_next_req = 0.0


def set_rate_gap(seconds: float) -> None:
    """Tests set this to 0 so lookups do not sleep."""
    global REQUEST_GAP_SEC, _next_req
    REQUEST_GAP_SEC = max(0.0, float(seconds))
    _next_req = 0.0


def _throttle() -> None:
    global _next_req
    if REQUEST_GAP_SEC <= 0:
        return
    now = time.monotonic()
    if now < _next_req:
        time.sleep(_next_req - now)
        now = time.monotonic()
    _next_req = now + REQUEST_GAP_SEC


def slugify(title: str) -> str:
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def parse_slug(value: Any) -> Optional[str]:
    """Accept a slug or a metacritic.com/movie/<slug>/ URL. None if empty/invalid."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    m = re.search(r"metacritic\.com/movie/([^/?#]+)", s, re.I)
    if m:
        s = m.group(1)
    s = s.strip("/").lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    return s or None


def page_url(slug: str, kind: Optional[str] = None) -> str:
    slug = parse_slug(slug) or slug
    base = f"{ORIGIN}/movie/{slug}/"
    if kind == "critic":
        return base + "critic-reviews/"
    if kind == "user":
        return base + "user-reviews/"
    return base


def http_get(url: str, accept: str = "application/json, text/plain, */*") -> tuple[int, str, str]:
    """GET url -> (status, final_url, body). Network failures raise; HTTP errors return the status."""
    _throttle()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.geturl(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, exc.geturl(), body


def _get_json(url: str) -> tuple[int, Optional[Any]]:
    try:
        status, _, body = http_get(url, "application/json, text/plain, */*")
    except Exception:
        return 0, None
    if status != 200:
        return status, None
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, None


def _norm_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def search_movie(title: str, year: Optional[int] = None) -> Optional[dict]:
    """Best movie hit for title + year, or None if missing/ambiguous."""
    if not (title or "").strip():
        return None
    query = urllib.parse.quote(title.strip(), safe="")
    status, data = _get_json(SEARCH_URL.format(query=query))
    if status != 200 or not isinstance(data, dict):
        return None
    items = (data.get("data") or {}).get("items") or []
    movies = [i for i in items if isinstance(i, dict) and i.get("slug") and (i.get("type") in (None, "movie", "Movie") or not i.get("type"))]
    if not movies:
        return None
    want = _norm_title(title)
    exact = [i for i in movies if _norm_title(i.get("title") or "") == want]
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None
    def _year_of(i):
        y = i.get("premiereYear")
        try: return int(y) if y is not None else None
        except (TypeError, ValueError): return None
    def _unique(hits):
        """One hit, or several with the same slug; None if the slugs disagree."""
        if not hits: return None
        slugs = {h.get("slug") for h in hits}
        return hits[0] if len(slugs) == 1 else None
    if year is not None:
        year_exact = [i for i in exact if _year_of(i) == year]
        hit = _unique(year_exact)
        if hit: return hit
        # Festival vs theatrical often differs by a year. Prefer exact year above; accept a
        # unique exact-title hit one year away; if two ±1 titles exist, do not guess.
        year_close = [i for i in exact if _year_of(i) is not None and abs(_year_of(i) - year) == 1]
        hit = _unique(year_close)
        if hit: return hit
        if year_close: return None
        return None
    if exact and len(exact) == 1:
        return exact[0]
    if not exact and len(movies) == 1:
        return movies[0]
    return None


def _stats_item(payload: Any) -> dict:
    item = ((payload or {}).get("data") or {}).get("item") or {}
    return {
        "score": item.get("score"),
        "reviewCount": item.get("reviewCount"),
        "max": item.get("max"),
        "sentiment": item.get("sentiment"),
    }


def scores_from_stats(slug: str) -> dict:
    """Fetch critic + user stats JSON. Missing endpoints leave those keys None."""
    out = {"slug": slug, "metascore": None, "critic_count": None, "user_score": None, "user_count": None, "ok_any": False}
    st_c, critic = _get_json(CRITIC_STATS.format(slug=slug))
    st_u, user = _get_json(USER_STATS.format(slug=slug))
    if st_c == 200 and critic:
        item = _stats_item(critic)
        out["metascore"] = item.get("score")
        out["critic_count"] = item.get("reviewCount")
        out["ok_any"] = True
    if st_u == 200 and user:
        item = _stats_item(user)
        out["user_score"] = item.get("score")
        out["user_count"] = item.get("reviewCount")
        out["ok_any"] = True
    return out


def _extract_json_ld(html: str) -> dict:
    m = re.search(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    )
    if not m:
        return {}
    try:
        data = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return {}
    agg = data.get("aggregateRating") or {}
    return {"metascore": agg.get("ratingValue"), "critic_count": agg.get("reviewCount"), "title": data.get("name")}


def _deref_nuxt(payload: list, idx: Any, seen: Optional[set] = None, depth: int = 0) -> Any:
    if not isinstance(idx, int) or idx < 0 or idx >= len(payload):
        return idx
    if seen is None:
        seen = set()
    if idx in seen or depth > 30:
        return None
    seen = seen | {idx}
    item = payload[idx]
    if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and item[0] in {
        "ShallowReactive", "Reactive", "Ref", "ShallowRef", "EmptyRef",
    }:
        return _deref_nuxt(payload, item[1], seen, depth + 1)
    if isinstance(item, dict):
        return {k: _deref_nuxt(payload, v, seen, depth + 1) for k, v in item.items()}
    if isinstance(item, list):
        return [_deref_nuxt(payload, v, seen, depth + 1) for v in item]
    return item


def _extract_nuxt(html: str, slug: str) -> dict:
    m = re.search(
        r'<script[^>]*id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    )
    if not m:
        return {}
    try:
        payload = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, list):
        return {}
    out: dict[str, Any] = {}
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        if "score" in item and "reviewCount" in item and "url" in item:
            resolved = _deref_nuxt(payload, i)
            if not isinstance(resolved, dict):
                continue
            url = resolved.get("url") or ""
            if isinstance(url, str) and "/critic-reviews" in url:
                out["metascore"] = resolved.get("score")
                out["critic_count"] = resolved.get("reviewCount")
            elif isinstance(url, str) and "/user-reviews" in url:
                out["user_score"] = resolved.get("score")
                out["user_count"] = resolved.get("reviewCount")
        if "criticScoreSummary" in item and item.get("slug") is not None:
            resolved = _deref_nuxt(payload, i)
            if isinstance(resolved, dict) and resolved.get("slug") == slug:
                summary = resolved.get("criticScoreSummary") or {}
                if isinstance(summary, dict):
                    out.setdefault("metascore", summary.get("score"))
                    out.setdefault("critic_count", summary.get("reviewCount"))
    return out


def _extract_labels(html: str) -> dict:
    out: dict[str, Any] = {}
    for kind, key in (("User score", "user_score"), ("Metascore", "metascore")):
        matches = re.findall(
            rf'title="{kind} ([0-9.]+) out of (?:10|100)"[^>]*data-testid="global-score-value-wrapper"',
            html, re.I,
        )
        if not matches:
            matches = re.findall(
                rf'data-testid="global-score-value-wrapper"[^>]*title="{kind} ([0-9.]+) out of (?:10|100)"',
                html, re.I,
            )
        if matches:
            try:
                out[key] = float(matches[0]) if kind == "User score" else int(float(matches[0]))
            except ValueError:
                pass
    m = re.search(r"Based on\s+([\d,]+)\s+Critic Reviews", html, re.I)
    if m:
        out["critic_count"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Based on\s+([\d,]+)\s+User Ratings", html, re.I)
    if m:
        out["user_count"] = int(m.group(1).replace(",", ""))
    return out


def scores_from_page(slug: str) -> dict:
    """Parse one movie page. Returns the same score keys as scores_from_stats."""
    out = {"slug": slug, "metascore": None, "critic_count": None, "user_score": None, "user_count": None, "ok_any": False, "methods": []}
    try:
        status, _, html = http_get(f"{ORIGIN}/movie/{slug}/", "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
    except Exception:
        return out
    if status != 200:
        return out
    ld, nuxt, labels = _extract_json_ld(html), _extract_nuxt(html, slug), _extract_labels(html)
    if ld.get("metascore") is not None:
        out["metascore"], out["critic_count"] = ld.get("metascore"), ld.get("critic_count")
        out["methods"].append("json-ld")
    if nuxt.get("metascore") is not None:
        out["metascore"], out["critic_count"] = nuxt.get("metascore"), nuxt.get("critic_count")
        out["methods"].append("nuxt-critic")
    if nuxt.get("user_score") is not None:
        out["user_score"], out["user_count"] = nuxt.get("user_score"), nuxt.get("user_count")
        out["methods"].append("nuxt-user")
    if out["user_score"] is None and labels.get("user_score") is not None:
        out["user_score"], out["user_count"] = labels.get("user_score"), labels.get("user_count")
        out["methods"].append("html-label")
    if out["metascore"] is None and labels.get("metascore") is not None:
        out["metascore"], out["critic_count"] = labels.get("metascore"), labels.get("critic_count")
        out["methods"].append("html-label")
    out["ok_any"] = out["metascore"] is not None or out["user_score"] is not None
    return out


def _merge_scores(dst: dict, src: dict) -> None:
    for k in ("metascore", "critic_count", "user_score", "user_count"):
        if dst.get(k) is None and src.get(k) is not None:
            dst[k] = src[k]


def lookup(title: str, year: Optional[int] = None, slug: Optional[str] = None) -> dict:
    """Look up scores for a title+year, or a known slug.

    Returns {"ok": True, "slug", "metascore", "critic_count", "user_score", "user_count", "methods"}
    or {"ok": False, "reason": "not_found"|"error", ...}.
    """
    methods: list[str] = []
    slug = parse_slug(slug)
    try:
        if not slug:
            hit = search_movie(title, year)
            if not hit or not hit.get("slug"):
                return {"ok": False, "reason": "not_found"}
            slug = hit["slug"]
            methods.append("search")
        stats = scores_from_stats(slug)
        if stats.get("ok_any"):
            methods.append("stats")
        if stats.get("metascore") is None or stats.get("user_score") is None:
            page = scores_from_page(slug)
            if page.get("ok_any"):
                _merge_scores(stats, page)
                methods.extend(page.get("methods") or ["page"])
        if stats.get("metascore") is None and stats.get("user_score") is None:
            return {"ok": False, "reason": "not_found", "slug": slug, "methods": methods}
        return {
            "ok": True,
            "slug": slug,
            "metascore": stats.get("metascore"),
            "critic_count": stats.get("critic_count"),
            "user_score": stats.get("user_score"),
            "user_count": stats.get("user_count"),
            "methods": methods,
        }
    except Exception as exc:
        return {"ok": False, "reason": "error", "error": str(exc), "slug": slug, "methods": methods}
