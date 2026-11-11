import asyncio
import copy
import json
import logging
import os
import re
import time
import urllib.parse
import weakref
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Input limits
# ---------------------------------------------------------------------------
# Only this much free text is ever parsed for links / IDs, and at most this many
# entries are returned. Anything beyond is silently ignored.
MAX_PARSE_CHARS = 100_000
MAX_PARSED_ENTRIES = 200
# A single URL longer than this is not a real YouTube link; skip it unparsed.
MAX_URL_TOKEN_CHARS = 2048
MAX_SEARCH_QUERY_CHARS = 200
MAX_SEARCH_RESULTS = 20
MAX_TEXT_FIELD_CHARS = 300
# Clip start/end times above this are rejected (no YouTube video is longer than a day).
MAX_CLIP_SECONDS = 24 * 60 * 60

RAW_VIDEO_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')
_VIDEO_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

# Candidate URL tokens in free text. Every alternative is a fixed prefix followed by
# one greedy character class with nothing after it, so matching is linear.
_URL_TOKEN_PATTERN = re.compile(
    r'https?://[^\s"\'<>]+|(?:www\.|m\.|music\.)?youtube\.com/[^\s"\'<>]+|youtu\.be/[^\s"\'<>]+'
)
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
_YOUTU_BE_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_ID_PATH_PREFIXES = frozenset({"embed", "v", "shorts"})

PLAYLIST_URL_PARAM_PATTERN = re.compile(r'[?&]list=([A-Za-z0-9_-]{10,})')
RAW_PLAYLIST_ID_PATTERN = re.compile(r'^(?:PL|OLAK5uy_|UU|FL|RD)[A-Za-z0-9_-]{8,}$')

# ---------------------------------------------------------------------------
# Outbound request headers
# ---------------------------------------------------------------------------
# Honest identification for API calls (YouTube Data API, oEmbed).
API_HEADERS = {
    "User-Agent": "Webjam/0.1 (+https://github.com/dbolser/webjam)",
    "Accept": "application/json",
}

# LEGACY scrape headers, used only by the HTML scraping paths (/embed playability,
# the dev-only /results search fallback and /playlist import). These are slated
# for removal in Phase 4 when everything moves to the Data API. Never use them
# for API calls.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgJnPpwY; CONSENT=YES+cb",
}

YOUTUBE_API_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_API_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"
_THUMBNAIL_PREFIX = "https://i.ytimg.com/"

# ---------------------------------------------------------------------------
# Caching and outbound concurrency
# ---------------------------------------------------------------------------
EMBED_CACHE_TTL_SECONDS = 6 * 60 * 60
EMBED_NEGATIVE_CACHE_TTL_SECONDS = 60 * 60
EMBED_CACHE_MAX_ENTRIES = 5000
SEARCH_CACHE_TTL_SECONDS = 60 * 60
SEARCH_CACHE_MAX_ENTRIES = 500
MAX_CONCURRENT_OUTBOUND = 4


class _TTLCache:
    """Small in-process TTL cache with LRU eviction. Values are deep-copied in and
    out so callers can mutate what they get back without touching the cache."""

    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._data: "OrderedDict[Any, Tuple[float, Any]]" = OrderedDict()

    def get(self, key: Any) -> Any:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return copy.deepcopy(value)

    def set(self, key: Any, value: Any, ttl_seconds: Optional[float] = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._data[key] = (time.monotonic() + ttl, copy.deepcopy(value))
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# video_id -> embeddability + oEmbed metadata result dict
_EMBED_CACHE = _TTLCache(EMBED_CACHE_TTL_SECONDS, EMBED_CACHE_MAX_ENTRIES)
# (mode, query, max_results) -> list of result dicts
_SEARCH_CACHE = _TTLCache(SEARCH_CACHE_TTL_SECONDS, SEARCH_CACHE_MAX_ENTRIES)

# One semaphore per event loop (asyncio primitives must not cross loops, and the
# test client / lifespan may run several loops over the process lifetime).
_OUTBOUND_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _outbound_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _OUTBOUND_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_OUTBOUND)
        _OUTBOUND_SEMAPHORES[loop] = sem
    return sem


def clear_youtube_caches() -> None:
    """Drop all cached YouTube lookups (used by tests)."""
    _EMBED_CACHE.clear()
    _SEARCH_CACHE.clear()
    _search_budget.update(day="", used=0)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def is_valid_video_id(value: Any) -> bool:
    return isinstance(value, str) and RAW_VIDEO_ID_PATTERN.fullmatch(value) is not None


def _leading_video_id(segment: str) -> Optional[str]:
    """Return the 11-char video id at the start of segment if it is followed by a
    non-id character (or nothing)."""
    candidate = segment[:11]
    if not is_valid_video_id(candidate):
        return None
    if len(segment) > 11 and segment[11] in _VIDEO_ID_CHARS:
        return None
    return candidate


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_TEXT_FIELD_CHARS]


def _safe_thumbnail(url: Any, video_id: str) -> str:
    if isinstance(url, str) and url.startswith(_THUMBNAIL_PREFIX) and len(url) <= MAX_URL_TOKEN_CHARS:
        return url
    return _default_thumbnail(video_id)


def _default_thumbnail(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{urllib.parse.quote(video_id, safe='')}/hqdefault.jpg"


def parse_timestamp_seconds(val: Any) -> Optional[int]:
    """
    Parse a timestamp into integer seconds.
    Supports:
      - int / float / numeric strings: "90" -> 90
      - mm:ss or hh:mm:ss: "1:30" -> 90, "01:02:03" -> 3723
      - YouTube duration strings: "1h2m30s", "2m15s", "45s" -> 135, 45
    Returns None if empty, invalid, negative or longer than MAX_CLIP_SECONDS.
    """
    sec = _parse_timestamp_raw(val)
    if sec is None or sec < 0 or sec > MAX_CLIP_SECONDS:
        return None
    return sec


def _parse_timestamp_raw(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        try:
            return int(val)
        except (OverflowError, ValueError):  # inf / nan
            return None

    s = str(val).strip().lower()
    if not s:
        return None

    if re.match(r'^\d+$', s):
        return int(s)

    # Check mm:ss or hh:mm:ss
    if ':' in s:
        parts = s.split(':')
        if all(p.isdigit() for p in parts) and 2 <= len(parts) <= 3:
            nums = [int(p) for p in parts]
            if len(nums) == 2:
                return nums[0] * 60 + nums[1]
            return nums[0] * 3600 + nums[1] * 60 + nums[2]

    # Check YouTube style 1h2m30s
    m = re.match(r'^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$', s)
    if m and any(m.groups()):
        h = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        secs = int(m.group(3) or 0)
        return h * 3600 + mins * 60 + secs

    return None


def _parse_youtube_url(token: str) -> Optional[Dict[str, Any]]:
    """
    Parse one URL-ish token with urllib.parse (no backtracking regex). Supports
    youtube.com/watch?v=..., youtu.be/..., youtube.com/embed|v|shorts/...,
    on www./m./music. hosts. Returns {video_id, start_seconds, end_seconds} or None.
    """
    if len(token) > MAX_URL_TOKEN_CHARS:
        return None
    try:
        parsed = urllib.parse.urlsplit(token if "://" in token else f"https://{token}")
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        qs = urllib.parse.parse_qs(parsed.query, max_num_fields=50)
    except ValueError:
        return None

    vid: Optional[str] = None
    if host in _YOUTU_BE_HOSTS:
        vid = _leading_video_id(parsed.path.lstrip("/").split("/", 1)[0])
    elif host in _YOUTUBE_HOSTS:
        parts = parsed.path.split("/")
        if len(parts) >= 2 and parts[1] == "watch":
            for value in qs.get("v", []):
                vid = _leading_video_id(value)
                if vid:
                    break
        elif len(parts) >= 3 and parts[1] in _ID_PATH_PREFIXES:
            vid = _leading_video_id(parts[2])
    if not vid:
        return None

    start_val = (qs.get("start") or qs.get("t") or [None])[0]
    end_val = (qs.get("end") or [None])[0]
    return {
        "video_id": vid,
        "start_seconds": parse_timestamp_seconds(start_val),
        "end_seconds": parse_timestamp_seconds(end_val),
    }


def extract_video_entries(text: str) -> List[Dict[str, Any]]:
    """
    Extract one or more YouTube video IDs along with optional start_seconds / end_seconds
    parsed from URL query parameters (?t=..., &start=..., &end=...).

    Runs in linear time; only the first MAX_PARSE_CHARS characters are read and at most
    MAX_PARSED_ENTRIES entries are returned.
    """
    if not text or not isinstance(text, str):
        return []
    text = text[:MAX_PARSE_CHARS].strip()
    if is_valid_video_id(text):
        return [{"video_id": text, "start_seconds": None, "end_seconds": None}]

    entries: List[Dict[str, Any]] = []
    seen = set()

    for match in _URL_TOKEN_PATTERN.finditer(text):
        entry = _parse_youtube_url(match.group(0))
        if not entry or entry["video_id"] in seen:
            continue
        seen.add(entry["video_id"])
        entries.append(entry)
        if len(entries) >= MAX_PARSED_ENTRIES:
            return entries

    if not entries:
        for token in re.split(r'[\s,]+', text):
            if is_valid_video_id(token) and token not in seen:
                seen.add(token)
                entries.append({"video_id": token, "start_seconds": None, "end_seconds": None})
                if len(entries) >= MAX_PARSED_ENTRIES:
                    break

    return entries


def extract_video_ids(text: str) -> List[str]:
    """Extract one or more 11-character YouTube video IDs from arbitrary text or URLs."""
    return [e["video_id"] for e in extract_video_entries(text)]


# ---------------------------------------------------------------------------
# Embeddability checks
# ---------------------------------------------------------------------------
# Unambiguous alternation (an escaped pair, or a char that is neither quote nor
# backslash) so a run of backslashes cannot backtrack exponentially.
_EMBED_PLAYER_RESPONSE_PATTERN = re.compile(r'"embedded_player_response":"((?:\\.|[^"\\])*)"')


def _parse_embed_playability(embed_html: str) -> Dict[str, Any]:
    """Parse previewPlayabilityStatus from YouTube's /embed/{video_id} HTML."""
    m = _EMBED_PLAYER_RESPONSE_PATTERN.search(embed_html)
    if not m:
        return {"embeddable": False, "status": "UNKNOWN", "reason": "No embed player response"}
    try:
        raw_json = json.loads('"' + m.group(1) + '"')
        data = json.loads(raw_json)
        ps = data.get("previewPlayabilityStatus", {})
        status = ps.get("status", "UNKNOWN")
        if not isinstance(status, str):
            status = "UNKNOWN"
        reason = _clean_text(ps.get("reason")) or None
        return {
            "embeddable": status == "OK",
            "status": status[:64],
            "reason": reason,
        }
    except Exception:
        # Never echo parser exception text back to clients.
        return {"embeddable": False, "status": "PARSE_ERROR", "reason": "Could not read embed status"}


def _invalid_video_result(video_id: Any) -> Dict[str, Any]:
    return {
        "video_id": video_id if is_valid_video_id(video_id) else "",
        "title": "Unknown YouTube Track",
        "channel_title": "YouTube Artist",
        "thumbnail_url": "",
        "embeddable": False,
        "status": "INVALID_ID",
        "reason": "Not a valid YouTube video ID",
    }


async def _fetch_embeddability(
    video_id: str, client: httpx.AsyncClient
) -> Tuple[Dict[str, Any], Optional[float]]:
    """Do the outbound requests for one (already validated) video id.
    Returns (result, cache_ttl_seconds or None if the result must not be cached)."""
    watch_url = f"https://www.youtube.com/watch?{urllib.parse.urlencode({'v': video_id})}"
    oembed_url = f"{YOUTUBE_OEMBED_URL}?{urllib.parse.urlencode({'url': watch_url, 'format': 'json'})}"
    # LEGACY scrape of the embed page (Phase 4 replaces this with videos.list +
    # the IFrame player's onError as the final word).
    embed_url = f"https://www.youtube.com/embed/{urllib.parse.quote(video_id, safe='')}"
    default_thumb = _default_thumbnail(video_id)
    headers_embed = {**DEFAULT_HEADERS, "Referer": "http://127.0.0.1:8000/"}

    o_resp, e_resp = await asyncio.gather(
        client.get(oembed_url, headers=API_HEADERS),
        client.get(embed_url, headers=headers_embed),
        return_exceptions=True,
    )

    not_found = {
        "video_id": video_id,
        "title": f"YouTube Track ({video_id})",
        "channel_title": "YouTube Artist",
        "thumbnail_url": default_thumb,
        "embeddable": False,
        "status": "NOT_FOUND",
        "reason": "Video unavailable or does not allow oEmbed",
    }
    # Network errors, 429/5xx and garbled replies say nothing about the video itself.
    unavailable = {
        **not_found,
        "status": "UNAVAILABLE",
        "reason": "YouTube could not be reached; try again in a moment",
    }
    if isinstance(o_resp, BaseException):
        return unavailable, None
    if o_resp.status_code != 200:
        # 4xx from oEmbed is a definitive answer (missing, private, embedding off);
        # 429/5xx are transient and must not be cached.
        if o_resp.status_code in (400, 401, 403, 404):
            return not_found, EMBED_NEGATIVE_CACHE_TTL_SECONDS
        return unavailable, None

    try:
        o_data = o_resp.json()
        if not isinstance(o_data, dict):
            raise ValueError("unexpected oEmbed payload")
    except ValueError:
        return unavailable, None

    title = _clean_text(o_data.get("title")) or f"YouTube Track ({video_id})"
    channel_title = _clean_text(o_data.get("author_name")) or "YouTube Artist"
    thumbnail_url = _safe_thumbnail(o_data.get("thumbnail_url"), video_id)

    if isinstance(e_resp, BaseException) or e_resp.status_code != 200:
        return (
            {
                "video_id": video_id,
                "title": title,
                "channel_title": channel_title,
                "thumbnail_url": thumbnail_url,
                "embeddable": True,
                "status": "OEMBED_OK",
                "reason": None,
            },
            None,
        )

    playability = _parse_embed_playability(e_resp.text)
    ttl = None if playability["status"] in ("UNKNOWN", "PARSE_ERROR") else EMBED_CACHE_TTL_SECONDS
    return (
        {
            "video_id": video_id,
            "title": title,
            "channel_title": channel_title,
            "thumbnail_url": thumbnail_url,
            "embeddable": playability["embeddable"],
            "status": playability["status"],
            "reason": playability["reason"],
        },
        ttl,
    )


async def check_video_embeddability(
    video_id: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Verify whether a YouTube video exists (via oEmbed) AND actually allows external
    IFrame embedding (by inspecting /embed/{video_id} previewPlayabilityStatus).

    Invalid ids never reach the network. Definitive results are cached in-process
    for EMBED_CACHE_TTL_SECONDS, and outbound checks are limited to
    MAX_CONCURRENT_OUTBOUND at a time.
    """
    if not is_valid_video_id(video_id):
        return _invalid_video_result(video_id)

    cached = _EMBED_CACHE.get(video_id)
    if cached is not None:
        return cached

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=5.5)
    try:
        async with _outbound_semaphore():
            result, ttl = await _fetch_embeddability(video_id, client)
    finally:
        if owns_client:
            await client.aclose()

    if ttl is not None:
        _EMBED_CACHE.set(video_id, result, ttl)
    return result


async def fetch_youtube_metadata(video_id: str, custom_title: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch YouTube video title, channel name, and embeddability status.
    Nothing proxies or touches media streams; only public page metadata is read.
    """
    info = await check_video_embeddability(video_id)
    if custom_title and custom_title.strip():
        info["title"] = custom_title.strip()
    return info


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _video_resource_to_result(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a Data API `videos` resource to our search/embeddability result shape."""
    vid = item.get("id")
    if not is_valid_video_id(vid):
        return None
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    details = item.get("contentDetails") or {}

    thumbs = snippet.get("thumbnails") or {}
    thumb_url = None
    for size in ("high", "medium", "default"):
        t = thumbs.get(size)
        if isinstance(t, dict) and t.get("url"):
            thumb_url = t["url"]
            break

    reason: Optional[str] = None
    if status.get("embeddable") is not True:
        reason = "The owner does not allow embedding"
    elif status.get("privacyStatus") not in ("public", "unlisted"):
        reason = "Video is private"
    elif status.get("uploadStatus") in ("deleted", "failed", "rejected"):
        reason = "Video is unavailable"
    elif (details.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
        reason = "Age-restricted videos cannot be embedded"

    return {
        "video_id": vid,
        "title": _clean_text(snippet.get("title")) or f"YouTube Track ({vid})",
        "channel_title": _clean_text(snippet.get("channelTitle")) or "YouTube Artist",
        "thumbnail_url": _safe_thumbnail(thumb_url, vid),
        "embeddable": reason is None,
        "status": "OK" if reason is None else "UNPLAYABLE",
        "reason": reason,
    }


async def _search_via_data_api(
    clean_q: str, max_results: int, api_key: str, client: httpx.AsyncClient
) -> List[Dict[str, Any]]:
    """search.list for candidates, then videos.list?part=status,contentDetails for
    embeddable/privacy/age status. Costs 101 quota units per call."""
    sem = _outbound_semaphore()
    search_params = {
        "part": "snippet",
        "type": "video",
        "videoEmbeddable": "true",
        "videoCategoryId": "10",
        "maxResults": str(min(max_results * 2, 25)),
        "q": clean_q,
        "key": api_key,
    }
    try:
        async with sem:
            resp = await client.get(YOUTUBE_API_SEARCH_URL, params=search_params, headers=API_HEADERS)
        if resp.status_code != 200:
            logger.warning("YouTube search.list failed with HTTP %s", resp.status_code)
            return []
        candidate_ids: List[str] = []
        for item in resp.json().get("items", []) or []:
            vid = (item.get("id") or {}).get("videoId") if isinstance(item, dict) else None
            if is_valid_video_id(vid) and vid not in candidate_ids:
                candidate_ids.append(vid)
        if not candidate_ids:
            return []

        video_params = {
            "part": "snippet,status,contentDetails",
            "id": ",".join(candidate_ids),
            "key": api_key,
        }
        async with sem:
            v_resp = await client.get(YOUTUBE_API_VIDEOS_URL, params=video_params, headers=API_HEADERS)
        if v_resp.status_code != 200:
            logger.warning("YouTube videos.list failed with HTTP %s", v_resp.status_code)
            return []
        by_id: Dict[str, Dict[str, Any]] = {}
        for item in v_resp.json().get("items", []) or []:
            if isinstance(item, dict):
                info = _video_resource_to_result(item)
                if info:
                    by_id[info["video_id"]] = info
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        # Log only the exception type: the request URL carries the API key.
        logger.warning("YouTube Data API search failed: %s", type(exc).__name__)
        return []

    verified: List[Dict[str, Any]] = []
    for vid in candidate_ids:
        info = by_id.get(vid)
        if not info:
            continue
        _EMBED_CACHE.set(vid, info)
        if info["embeddable"]:
            verified.append(info)
            if len(verified) >= max_results:
                break
    return verified


async def _search_via_scrape_dev_only(
    clean_q: str, max_results: int, client: httpx.AsyncClient
) -> List[Dict[str, Any]]:
    """
    DEV-ONLY FALLBACK, used only when YOUTUBE_API_KEY is not set. Scrapes
    youtube.com/results with browser-like headers, which is against YouTube's ToS.
    Production deployments must set YOUTUBE_API_KEY. Removed in Phase 4.
    """
    candidate_ids: List[str] = []
    seen = set()
    limit = max(max_results * 2, 14)
    try:
        async with _outbound_semaphore():
            resp = await client.get(
                "https://www.youtube.com/results",
                params={"search_query": clean_q},
                headers=DEFAULT_HEADERS,
            )
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    for match in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', resp.text):
        vid = match.group(1)
        if vid not in seen:
            seen.add(vid)
            candidate_ids.append(vid)
        if len(candidate_ids) >= limit:
            break
    if not candidate_ids:
        return []

    checks = await asyncio.gather(
        *(check_video_embeddability(vid, client=client) for vid in candidate_ids),
        return_exceptions=True,
    )
    verified: List[Dict[str, Any]] = []
    for item in checks:
        if isinstance(item, dict) and item.get("embeddable"):
            verified.append(item)
            if len(verified) >= max_results:
                break
    return verified


class SearchBudgetExceeded(Exception):
    """The daily YouTube Data API search budget is used up."""


# search.list costs 100 of the default 10,000 daily quota units; stop well before Google does.
_search_budget = {"day": "", "used": 0}


def _search_daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("WEBJAM_SEARCH_DAILY_CAP", "80")))
    except ValueError:
        return 80


def _charge_search_budget() -> None:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if _search_budget["day"] != day:
        _search_budget["day"], _search_budget["used"] = day, 0
    if _search_budget["used"] >= _search_daily_cap():
        raise SearchBudgetExceeded()
    _search_budget["used"] += 1


def _search_cache_key(query: str, max_results: Any) -> Optional[Tuple[str, str, int]]:
    clean_q = (query or "").strip()[:MAX_SEARCH_QUERY_CHARS].strip()
    if not clean_q:
        return None
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 8
    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    return ("api" if api_key else "scrape", clean_q, max_results)


def cached_search_results(query: str, max_results: int = 8) -> Optional[List[Dict[str, Any]]]:
    """Results already cached for this search (no outbound request), else None."""
    key = _search_cache_key(query, max_results)
    if key is None:
        return []
    return _SEARCH_CACHE.get(key)


async def search_embeddable_youtube_songs(query: str, max_results: int = 8) -> List[Dict[str, Any]]:
    """
    Search YouTube for music videos and filter to those verified as embeddable.

    If YOUTUBE_API_KEY is set, uses only the YouTube Data API v3: `search.list`
    (`type=video`, `videoEmbeddable=true`, `videoCategoryId=10`) for candidates, then
    `videos.list?part=status,contentDetails` for embeddable/privacy/age status. It never
    falls back to scraping. Without a key, a DEV-ONLY scraping fallback is used.

    The query is capped at MAX_SEARCH_QUERY_CHARS; non-empty results are cached.
    """
    cache_key = _search_cache_key(query, max_results)
    if cache_key is None:
        return []
    _mode, clean_q, max_results = cache_key
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if api_key:
        _charge_search_budget()  # raises SearchBudgetExceeded
    async with httpx.AsyncClient(timeout=6.5) as client:
        if api_key:
            results = await _search_via_data_api(clean_q, max_results, api_key, client)
        else:
            results = await _search_via_scrape_dev_only(clean_q, max_results, client)

    if results:
        _SEARCH_CACHE.set(cache_key, results)
    return results


# ---------------------------------------------------------------------------
# Playlist import
# ---------------------------------------------------------------------------
def extract_playlist_id(text: str) -> Optional[str]:
    """Extract a YouTube playlist ID from a URL or raw playlist ID string."""
    if not text:
        return None
    s = text[:MAX_PARSE_CHARS].strip()
    # If the user pasted multiple lines of video URLs, treat as URL list unless it's a single playlist link
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if len(lines) == 1:
        if RAW_PLAYLIST_ID_PATTERN.match(lines[0]):
            return lines[0]
        if "youtube.com/playlist" in lines[0] or ("list=" in lines[0] and "watch?v=" not in lines[0]):
            m = PLAYLIST_URL_PARAM_PATTERN.search(lines[0])
            if m:
                return m.group(1)
        # Also support watch?v=...&list=PL... when a single URL with list= is pasted
        m = PLAYLIST_URL_PARAM_PATTERN.search(lines[0])
        if m and m.group(1).startswith(("PL", "OLAK5uy_", "UU", "FL")):
            return m.group(1)
    return None


# `remastered[^)\]]*` swallows everything up to the closing bracket and is atomic, so it
# never overlaps with the `\s*` that follows (the old `remastered.*?\s*` was quadratic).
_MIGRATION_NOISE_PATTERN = re.compile(
    r"(\(|\[)\s*(official\s*(music\s*)?video|official\s*audio|lyric\s*video|hd|4k|hq|(?>remastered[^)\]]*)|visualizer)\s*(\)|\])",
    re.IGNORECASE,
)
MAX_MIGRATION_TITLE_CHARS = 300


def clean_migration_query(title: str, channel_title: str = "") -> str:
    """Build a clean search query to find embeddable alternatives for a restricted track."""
    title = (title or "")[:MAX_MIGRATION_TITLE_CHARS]
    channel_title = (channel_title or "")[:MAX_MIGRATION_TITLE_CHARS]
    cleaned = _MIGRATION_NOISE_PATTERN.sub("", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if " - " not in cleaned and channel_title and channel_title.lower() not in ("youtube", "youtube artist"):
        ch_clean = re.sub(r"\s*-\s*topic$", "", channel_title, flags=re.IGNORECASE).strip()
        ch_clean = re.sub(r"vevo$", "", ch_clean, flags=re.IGNORECASE).strip()
        if ch_clean and ch_clean.lower() not in cleaned.lower():
            return f"{ch_clean} - {cleaned}"
    return cleaned or title


def _extract_playlist_items_from_initial_data(data: Any) -> List[Dict[str, Any]]:
    """
    Walk ytInitialData to extract ordered playlist tracks from either
    YouTube's modern `lockupViewModel` or classic `playlistVideoRenderer`.
    """
    items: List[Dict[str, Any]] = []
    seen = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "lockupViewModel" in node and isinstance(node["lockupViewModel"], dict):
                lvm = node["lockupViewModel"]
                vid = lvm.get("contentId")
                ctype = lvm.get("contentType", "")
                if is_valid_video_id(vid) and (not ctype or "VIDEO" in ctype):
                    meta_vm = lvm.get("metadata", {}).get("lockupMetadataViewModel", {})
                    title = meta_vm.get("title", {}).get("content") or ""
                    channel = ""
                    rows = (
                        meta_vm.get("metadata", {})
                        .get("contentMetadataViewModel", {})
                        .get("metadataRows", [])
                    )
                    if rows and isinstance(rows, list):
                        parts = rows[0].get("metadataParts", [])
                        if parts and isinstance(parts, list):
                            channel = parts[0].get("text", {}).get("content") or ""
                    if vid not in seen:
                        seen.add(vid)
                        items.append(
                            {
                                "video_id": vid,
                                "title": _clean_text(title) or f"YouTube Track ({vid})",
                                "channel_title": _clean_text(channel) or "YouTube Artist",
                                "start_seconds": None,
                                "end_seconds": None,
                            }
                        )
            elif "playlistVideoRenderer" in node and isinstance(node["playlistVideoRenderer"], dict):
                pvr = node["playlistVideoRenderer"]
                vid = pvr.get("videoId")
                if is_valid_video_id(vid):
                    title_runs = pvr.get("title", {}).get("runs", [])
                    title = (
                        title_runs[0].get("text")
                        if title_runs
                        else pvr.get("title", {}).get("simpleText", "")
                    )
                    byline_runs = pvr.get("shortBylineText", {}).get("runs", [])
                    channel = byline_runs[0].get("text") if byline_runs else ""
                    if vid not in seen:
                        seen.add(vid)
                        items.append(
                            {
                                "video_id": vid,
                                "title": _clean_text(title) or f"YouTube Track ({vid})",
                                "channel_title": _clean_text(channel) or "YouTube Artist",
                                "start_seconds": None,
                                "end_seconds": None,
                            }
                        )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for elem in node:
                walk(elem)

    walk(data)
    return items


async def inspect_youtube_playlist_or_urls(
    source_input: str, max_tracks: int = 30
) -> Dict[str, Any]:
    """
    Parse either a YouTube playlist URL/ID or a batch of YouTube video URLs/IDs,
    verify embeddability for each track in parallel, and return a structured
    inspection report ready for the Import & Migrate Song Wizard.

    NOTE: the playlist branch scrapes youtube.com/playlist (legacy, replaced by
    playlistItems.list in Phase 4).
    """
    raw = (source_input or "")[:MAX_PARSE_CHARS].strip()
    if not raw:
        raise ValueError("Please provide a YouTube playlist URL, playlist ID, or list of video links.")

    playlist_id = extract_playlist_id(raw)
    playlist_title = ""
    playlist_description = ""
    raw_tracks: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        if playlist_id:
            pl_url = "https://www.youtube.com/playlist?" + urllib.parse.urlencode({"list": playlist_id})
            async with _outbound_semaphore():
                resp = await client.get(pl_url, headers=DEFAULT_HEADERS)
            if resp.status_code == 200:
                m_init = re.search(r"var ytInitialData = (\{.*?\});</script>", resp.text)
                if m_init:
                    try:
                        data = json.loads(m_init.group(1))
                        meta = data.get("metadata", {}).get("playlistMetadataRenderer", {})
                        playlist_title = _clean_text(meta.get("title"))
                        playlist_description = meta.get("description") or ""
                        if not isinstance(playlist_description, str):
                            playlist_description = ""
                        raw_tracks = _extract_playlist_items_from_initial_data(data)
                    except Exception:
                        pass

                if not raw_tracks:
                    seen_ids = set()
                    for match in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', resp.text):
                        vid = match.group(1)
                        if vid not in seen_ids:
                            seen_ids.add(vid)
                            raw_tracks.append(
                                {
                                    "video_id": vid,
                                    "title": "",
                                    "channel_title": "",
                                    "start_seconds": None,
                                    "end_seconds": None,
                                }
                            )
                            if len(raw_tracks) >= MAX_PARSED_ENTRIES:
                                break

        if not raw_tracks:
            entries = extract_video_entries(raw)
            for e in entries:
                raw_tracks.append(
                    {
                        "video_id": e["video_id"],
                        "title": "",
                        "channel_title": "",
                        "start_seconds": e.get("start_seconds"),
                        "end_seconds": e.get("end_seconds"),
                    }
                )

        if not raw_tracks:
            raise ValueError(
                "Could not find any public tracks in that YouTube playlist or link input. "
                "Make sure the YouTube playlist visibility is Public or Unlisted."
            )

        capped_tracks = raw_tracks[: max(1, min(max_tracks, 50))]
        sem = asyncio.Semaphore(10)

        async def verify_one(item: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                info = await check_video_embeddability(item["video_id"], client=client)
                # Preserve scraped playlist title/channel if oEmbed returned a generic fallback
                title = info.get("title") or ""
                if (not title or title.startswith("YouTube Track (")) and item.get("title"):
                    title = item["title"]
                channel = info.get("channel_title") or ""
                if (not channel or channel == "YouTube Artist") and item.get("channel_title"):
                    channel = item["channel_title"]
                return {
                    "video_id": item["video_id"],
                    "title": title or f"YouTube Track ({item['video_id']})",
                    "channel_title": channel or "YouTube Artist",
                    "thumbnail_url": info.get("thumbnail_url") or _default_thumbnail(item["video_id"]),
                    "embeddable": bool(info.get("embeddable")),
                    "status": info.get("status", "UNKNOWN"),
                    "reason": info.get("reason"),
                    "start_seconds": item.get("start_seconds"),
                    "end_seconds": item.get("end_seconds"),
                    "suggested_query": clean_migration_query(
                        title or item.get("title") or item["video_id"],
                        channel or item.get("channel_title") or "",
                    ),
                }

        verified_tracks = await asyncio.gather(*(verify_one(t) for t in capped_tracks))

    embeddable_count = sum(1 for t in verified_tracks if t["embeddable"])
    non_embeddable_count = len(verified_tracks) - embeddable_count

    return {
        "source_type": "youtube_playlist" if playlist_id else "url_list",
        "youtube_playlist_id": playlist_id,
        "title": playlist_title or ("Imported YouTube Playlist" if playlist_id else "Imported Crate"),
        "description": playlist_description
        or (f"Imported from YouTube playlist {playlist_id}" if playlist_id else "Imported into Webjam."),
        "total_tracks": len(verified_tracks),
        "embeddable_count": embeddable_count,
        "non_embeddable_count": non_embeddable_count,
        "tracks": list(verified_tracks),
    }
