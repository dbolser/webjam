import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional
import urllib.parse
import httpx

# Matches standard youtube.com/watch?v=..., youtu.be/..., youtube.com/embed/..., youtube.com/shorts/..., music.youtube.com/watch?v=...
YOUTUBE_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})'
)
RAW_VIDEO_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgJnPpwY; CONSENT=YES+cb",
}

PLAYLIST_URL_PARAM_PATTERN = re.compile(r'[?&]list=([A-Za-z0-9_-]{10,})')
RAW_PLAYLIST_ID_PATTERN = re.compile(r'^(?:PL|OLAK5uy_|UU|FL|RD)[A-Za-z0-9_-]{8,}$')



YOUTUBE_FULL_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:[^\s"\'<>]*&)?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})([^\s"\'<>]*)'
)


def parse_timestamp_seconds(val: Any) -> Optional[int]:
    """
    Parse a timestamp into integer seconds.
    Supports:
      - int / float / numeric strings: "90" -> 90
      - mm:ss or hh:mm:ss: "1:30" -> 90, "01:02:03" -> 3723
      - YouTube duration strings: "1h2m30s", "2m15s", "45s" -> 135, 45
    Returns None if empty or invalid.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        sec = int(val)
        return sec if sec >= 0 else None

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


def extract_video_entries(text: str) -> List[Dict[str, Any]]:
    """
    Extract one or more YouTube video IDs along with optional start_seconds / end_seconds
    parsed from URL query parameters (?t=..., &start=..., &end=...).
    """
    if not text:
        return []
    text = text.strip()
    if RAW_VIDEO_ID_PATTERN.match(text):
        return [{"video_id": text, "start_seconds": None, "end_seconds": None}]

    entries: List[Dict[str, Any]] = []
    seen = set()

    for match in re.finditer(r'https?://[^\s"\'<>]+|(?:www\.|m\.|music\.)?youtube\.com/[^\s"\'<>]+|youtu\.be/[^\s"\'<>]+', text):
        token = match.group(0)
        m_vid = YOUTUBE_URL_PATTERN.search(token)
        if not m_vid:
            continue
        vid = m_vid.group(1)
        parsed = urllib.parse.urlparse(token if '://' in token else f'https://{token}')
        qs = urllib.parse.parse_qs(parsed.query)
        start_val = (qs.get('start') or qs.get('t') or [None])[0]
        end_val = (qs.get('end') or [None])[0]
        start_sec = parse_timestamp_seconds(start_val)
        end_sec = parse_timestamp_seconds(end_val)
        if vid not in seen:
            seen.add(vid)
            entries.append({
                "video_id": vid,
                "start_seconds": start_sec,
                "end_seconds": end_sec,
            })

    if not entries:
        for token in re.split(r'[\s,]+', text):
            token = token.strip()
            if RAW_VIDEO_ID_PATTERN.match(token) and token not in seen:
                seen.add(token)
                entries.append({"video_id": token, "start_seconds": None, "end_seconds": None})

    return entries


def extract_video_ids(text: str) -> List[str]:
    """Extract one or more 11-character YouTube video IDs from arbitrary text or URLs."""
    return [e["video_id"] for e in extract_video_entries(text)]


def _parse_embed_playability(embed_html: str) -> Dict[str, Any]:
    """Parse previewPlayabilityStatus from YouTube's /embed/{video_id} HTML."""
    m = re.search(r'"embedded_player_response":"((?:\\.|[^"])*)"', embed_html)
    if not m:
        return {"embeddable": False, "status": "UNKNOWN", "reason": "No embed player response"}
    try:
        raw_json = json.loads('"' + m.group(1) + '"')
        data = json.loads(raw_json)
        ps = data.get("previewPlayabilityStatus", {})
        status = ps.get("status", "UNKNOWN")
        reason = ps.get("reason")
        return {
            "embeddable": status == "OK",
            "status": status,
            "reason": reason,
        }
    except Exception as exc:
        return {"embeddable": False, "status": "PARSE_ERROR", "reason": str(exc)}


async def check_video_embeddability(
    video_id: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Verify whether a YouTube video exists (via oEmbed) AND actually allows external
    IFrame embedding (by inspecting /embed/{video_id} previewPlayabilityStatus).
    """
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    oembed_url = f"https://www.youtube.com/oembed?url={watch_url}&format=json"
    embed_url = f"https://www.youtube.com/embed/{video_id}"
    default_thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=5.5)

    try:
        headers_embed = {**DEFAULT_HEADERS, "Referer": "http://127.0.0.1:8000/"}
        o_resp, e_resp = await asyncio.gather(
            client.get(oembed_url, headers=DEFAULT_HEADERS),
            client.get(embed_url, headers=headers_embed),
            return_exceptions=True,
        )

        if isinstance(o_resp, Exception) or o_resp.status_code != 200:
            return {
                "video_id": video_id,
                "title": f"YouTube Track ({video_id})",
                "channel_title": "YouTube Artist",
                "thumbnail_url": default_thumb,
                "embeddable": False,
                "status": "NOT_FOUND",
                "reason": "Video unavailable or does not allow oEmbed",
            }

        o_data = o_resp.json()
        title = o_data.get("title") or f"YouTube Track ({video_id})"
        channel_title = o_data.get("author_name") or "YouTube Artist"
        thumbnail_url = o_data.get("thumbnail_url") or default_thumb

        if isinstance(e_resp, Exception) or e_resp.status_code != 200:
            return {
                "video_id": video_id,
                "title": title,
                "channel_title": channel_title,
                "thumbnail_url": thumbnail_url,
                "embeddable": True,
                "status": "OEMBED_OK",
                "reason": None,
            }

        playability = _parse_embed_playability(e_resp.text)
        return {
            "video_id": video_id,
            "title": title,
            "channel_title": channel_title,
            "thumbnail_url": thumbnail_url,
            "embeddable": playability["embeddable"],
            "status": playability["status"],
            "reason": playability["reason"],
        }
    finally:
        if owns_client:
            await client.aclose()


async def fetch_youtube_metadata(video_id: str, custom_title: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch YouTube video title, channel name, and embeddability status.
    Nothing proxies or touches media streams; only public page metadata is read.
    """
    info = await check_video_embeddability(video_id)
    if custom_title and custom_title.strip():
        info["title"] = custom_title.strip()
    return info


async def search_embeddable_youtube_songs(query: str, max_results: int = 8) -> List[Dict[str, Any]]:
    """
    Search YouTube for music videos and filter to those verified as embeddable
    (previewPlayabilityStatus == 'OK' in YouTube's embedded player).

    If YOUTUBE_API_KEY is set, uses YouTube Data API v3 (`search.list` with
    `type=video`, `videoEmbeddable=true`, `videoCategoryId=10`) to get candidate IDs,
    then verifies actual embed playback status. Otherwise searches YouTube directly
    and verifies candidate IDs in parallel.
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return []

    candidate_ids: List[str] = []
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()

    async with httpx.AsyncClient(timeout=6.5) as client:
        if api_key:
            try:
                yt_api_url = (
                    "https://www.googleapis.com/youtube/v3/search"
                    f"?part=snippet&type=video&videoEmbeddable=true&videoCategoryId=10"
                    f"&maxResults={min(max_results * 2, 20)}"
                    f"&q={urllib.parse.quote(clean_q)}&key={urllib.parse.quote(api_key)}"
                )
                api_resp = await client.get(yt_api_url, headers=DEFAULT_HEADERS)
                if api_resp.status_code == 200:
                    for item in api_resp.json().get("items", []):
                        vid = item.get("id", {}).get("videoId")
                        if vid and vid not in candidate_ids:
                            candidate_ids.append(vid)
            except Exception:
                pass

        if not candidate_ids:
            search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(clean_q)
            resp = await client.get(search_url, headers=DEFAULT_HEADERS)
            if resp.status_code == 200:
                for match in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', resp.text):
                    vid = match.group(1)
                    if vid not in candidate_ids:
                        candidate_ids.append(vid)
                    if len(candidate_ids) >= max(max_results * 2, 14):
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


def extract_playlist_id(text: str) -> Optional[str]:
    """Extract a YouTube playlist ID from a URL or raw playlist ID string."""
    if not text:
        return None
    s = text.strip()
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


def clean_migration_query(title: str, channel_title: str = "") -> str:
    """Build a clean search query to find embeddable alternatives for a restricted track."""
    cleaned = re.sub(
        r"(\(|\[)\s*(official\s*(music\s*)?video|official\s*audio|lyric\s*video|hd|4k|hq|remastered.*?|visualizer)\s*(\)|\])",
        "",
        title or "",
        flags=re.IGNORECASE,
    ).strip()
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
                if vid and RAW_VIDEO_ID_PATTERN.match(str(vid)) and (not ctype or "VIDEO" in ctype):
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
                                "title": title or f"YouTube Track ({vid})",
                                "channel_title": channel or "YouTube Artist",
                                "start_seconds": None,
                                "end_seconds": None,
                            }
                        )
            elif "playlistVideoRenderer" in node and isinstance(node["playlistVideoRenderer"], dict):
                pvr = node["playlistVideoRenderer"]
                vid = pvr.get("videoId")
                if vid and RAW_VIDEO_ID_PATTERN.match(str(vid)):
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
                                "title": title or f"YouTube Track ({vid})",
                                "channel_title": channel or "YouTube Artist",
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
    """
    raw = (source_input or "").strip()
    if not raw:
        raise ValueError("Please provide a YouTube playlist URL, playlist ID, or list of video links.")

    playlist_id = extract_playlist_id(raw)
    playlist_title = ""
    playlist_description = ""
    raw_tracks: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        if playlist_id:
            pl_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
            resp = await client.get(pl_url, headers=DEFAULT_HEADERS)
            if resp.status_code == 200:
                m_init = re.search(r"var ytInitialData = (\{.*?\});</script>", resp.text)
                if m_init:
                    try:
                        data = json.loads(m_init.group(1))
                        meta = data.get("metadata", {}).get("playlistMetadataRenderer", {})
                        playlist_title = meta.get("title") or ""
                        playlist_description = meta.get("description") or ""
                        raw_tracks = _extract_playlist_items_from_initial_data(data)
                    except Exception:
                        pass

                if not raw_tracks:
                    for match in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', resp.text):
                        vid = match.group(1)
                        if not any(t["video_id"] == vid for t in raw_tracks):
                            raw_tracks.append(
                                {
                                    "video_id": vid,
                                    "title": "",
                                    "channel_title": "",
                                    "start_seconds": None,
                                    "end_seconds": None,
                                }
                            )

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
                    "thumbnail_url": info.get("thumbnail_url")
                    or f"https://i.ytimg.com/vi/{item['video_id']}/hqdefault.jpg",
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


