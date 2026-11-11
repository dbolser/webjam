"""Security regressions for app/youtube.py (Phase 0 items #5, #6, #8, #19).

All outbound HTTP is mocked with httpx.MockTransport; nothing touches the network.
"""
import asyncio
import json
import time

import httpx
import pytest

from app import youtube

VID = "dQw4w9WgXcQ"
VID2 = "0PsOJ_fRckg"
VID3 = "TNsSBhl_2LI"
HONEST_UA = "Webjam/0.1 (+https://github.com/dbolser/webjam)"


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    youtube.clear_youtube_caches()
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    yield
    youtube.clear_youtube_caches()


def _embed_html(status="OK", reason=None):
    inner = json.dumps({"previewPlayabilityStatus": {"status": status, "reason": reason}})
    escaped = json.dumps(inner)[1:-1]
    return f'<html><script>var x = {{"embedded_player_response":"{escaped}"}};</script></html>'


class FakeYouTube:
    """Records every request and answers like oEmbed / embed / Data API would."""

    def __init__(self, oembed_status=200, embed_status="OK", delay=0.0, api_status=200, videos=None):
        self.requests = []
        self.oembed_status = oembed_status
        self.embed_status = embed_status
        self.delay = delay
        self.api_status = api_status
        self.videos = videos or {}
        self.in_flight_oembed = 0
        self.max_in_flight_oembed = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "www.youtube.com" and path == "/oembed":
            self.in_flight_oembed += 1
            self.max_in_flight_oembed = max(self.max_in_flight_oembed, self.in_flight_oembed)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.oembed_status != 200:
                    return httpx.Response(self.oembed_status, text="nope")
                return httpx.Response(
                    200,
                    json={
                        "title": "Real Title",
                        "author_name": "Real Channel",
                        "thumbnail_url": "https://i.ytimg.com/vi/x/hqdefault.jpg",
                    },
                )
            finally:
                self.in_flight_oembed -= 1
        if host == "www.youtube.com" and path.startswith("/embed/"):
            return httpx.Response(200, text=_embed_html(self.embed_status))
        if host == "www.youtube.com" and path == "/results":
            body = "".join(f'"videoId":"{v}",' for v in (VID, VID2))
            return httpx.Response(200, text=body)
        if host == "www.googleapis.com" and path == "/youtube/v3/search":
            if self.api_status != 200:
                return httpx.Response(self.api_status, json={"error": {"message": "quotaExceeded"}})
            items = [{"id": {"kind": "youtube#video", "videoId": v}} for v in self.videos]
            items.append({"id": {"videoId": "bad id with spaces"}})
            return httpx.Response(200, json={"items": items})
        if host == "www.googleapis.com" and path == "/youtube/v3/videos":
            ids = request.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [self.videos[i] for i in ids if i in self.videos]})
        return httpx.Response(599, text="unexpected request in test")


@pytest.fixture
def fake(monkeypatch):
    fy = FakeYouTube()
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fy.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return fy


def _api_video(vid, embeddable=True, privacy="public", age=False, title="T"):
    details = {"duration": "PT3M"}
    if age:
        details["contentRating"] = {"ytRating": "ytAgeRestricted"}
    return {
        "id": vid,
        "snippet": {
            "title": title,
            "channelTitle": "Chan",
            "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}},
        },
        "status": {"embeddable": embeddable, "privacyStatus": privacy, "uploadStatus": "processed"},
        "contentDetails": details,
    }


# ---------------------------------------------------------------------------
# #5 ReDoS: linear URL parsing with input caps
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        "youtube.com/watch?&" * 55_000,  # one ~1 MB token (old pattern was O(n^2))
        "https://www.youtube.com/watch?" + "&" * 1_000_000 + "v=" + VID,
        ("youtube.com/watch?&" * 50 + " ") * 1_100,  # many medium tokens
        "https://youtu.be/" + "a" * 1_000_000,
        "x" * 1_000_000,
    ],
)
def test_adversarial_megabyte_input_parses_fast(payload):
    assert len(payload) >= 1_000_000
    t0 = time.perf_counter()
    youtube.extract_video_entries(payload)
    youtube.extract_video_ids(payload)
    youtube.extract_playlist_id(payload)
    assert time.perf_counter() - t0 < 0.5


def test_embed_player_regex_has_no_catastrophic_backtracking():
    html = '"embedded_player_response":"' + "\\" * 200_001
    t0 = time.perf_counter()
    result = youtube._parse_embed_playability(html)
    assert time.perf_counter() - t0 < 0.5
    assert result["embeddable"] is False


def test_old_quadratic_patterns_are_gone():
    assert not hasattr(youtube, "YOUTUBE_URL_PATTERN")
    assert not hasattr(youtube, "YOUTUBE_FULL_URL_PATTERN")


@pytest.mark.parametrize(
    "text,expected",
    [
        (f"https://www.youtube.com/watch?v={VID}", (VID, None, None)),
        (f"https://www.youtube.com/watch?feature=share&v={VID}&t=1m15s&end=4m05s", (VID, 75, 245)),
        (f"youtube.com/watch?v={VID}&start=30", (VID, 30, None)),
        (f"https://youtu.be/{VID}?t=90", (VID, 90, None)),
        (f"https://m.youtube.com/watch?v={VID}", (VID, None, None)),
        (f"https://music.youtube.com/watch?v={VID}&list=RDAMVM", (VID, None, None)),
        (f"https://www.youtube.com/shorts/{VID}", (VID, None, None)),
        (f"https://www.youtube.com/embed/{VID}?start=5", (VID, 5, None)),
        (f"https://www.youtube.com/v/{VID}", (VID, None, None)),
        (f"Check this out: https://youtu.be/{VID}.", (VID, None, None)),
        (VID, (VID, None, None)),
    ],
)
def test_url_parsing_still_supports_known_forms(text, expected):
    entries = youtube.extract_video_entries(text)
    assert [(e["video_id"], e["start_seconds"], e["end_seconds"]) for e in entries] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        f"https://evil.example/watch?v={VID}",
        f"https://evil.example/?u=youtube.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}EXTRA",  # longer than an id
        "https://www.youtube.com/watch?v=short",
    ],
)
def test_url_parsing_rejects_non_youtube_or_bad_ids(text):
    assert youtube.extract_video_ids(text) == []


def test_entry_count_and_input_size_are_capped():
    ids = [f"{i:011d}" for i in range(300)]
    text = "\n".join(f"https://youtu.be/{v}" for v in ids)
    assert len(youtube.extract_video_entries(text)) == youtube.MAX_PARSED_ENTRIES
    raw_ids = " ".join(ids)
    assert len(youtube.extract_video_ids(raw_ids)) == youtube.MAX_PARSED_ENTRIES

    late = "x " * (youtube.MAX_PARSE_CHARS // 2) + f"https://youtu.be/{VID}"
    assert youtube.extract_video_ids(late) == []


# ---------------------------------------------------------------------------
# #19 video id validation, encoded URLs, generic error reasons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["abc?x=1&y=2", "../../etc/pass", "dQw4w9WgXcQ\n", "", "a" * 12, "dQw4w9WgX%3F"])
def test_invalid_video_id_never_reaches_network(fake, bad):
    result = asyncio.run(youtube.check_video_embeddability(bad))
    assert result["status"] == "INVALID_ID"
    assert result["embeddable"] is False
    assert result["video_id"] == ""
    assert fake.requests == []

    meta = asyncio.run(youtube.fetch_youtube_metadata(bad, custom_title="x"))
    assert meta["embeddable"] is False
    assert fake.requests == []


def test_valid_video_id_builds_encoded_urls_with_honest_api_headers(fake):
    result = asyncio.run(youtube.check_video_embeddability(VID))
    assert result["embeddable"] is True and result["status"] == "OK"
    oembed = [r for r in fake.requests if r.url.path == "/oembed"]
    embed = [r for r in fake.requests if r.url.path.startswith("/embed/")]
    assert len(oembed) == 1 and len(embed) == 1
    assert oembed[0].url.params["url"] == f"https://www.youtube.com/watch?v={VID}"
    assert oembed[0].url.params["format"] == "json"
    assert embed[0].url.path == f"/embed/{VID}"
    # oEmbed is an API call: honest UA, no forged consent cookie or Referer
    assert oembed[0].headers["user-agent"] == HONEST_UA
    assert "cookie" not in oembed[0].headers
    assert "referer" not in oembed[0].headers


def test_parse_error_reason_does_not_leak_exception_text():
    html = '"embedded_player_response":"{not valid json'  # truncated
    result = youtube._parse_embed_playability(html + '"')
    assert result["status"] == "PARSE_ERROR"
    assert result["reason"] == "Could not read embed status"
    assert "Expecting" not in (result["reason"] or "")


def test_untrusted_oembed_thumbnail_host_is_replaced(fake, monkeypatch):
    async def handler(request):
        fake.requests.append(request)
        if request.url.path == "/oembed":
            return httpx.Response(200, json={"title": "t", "author_name": "a", "thumbnail_url": "https://evil.example/x.png"})
        return httpx.Response(200, text=_embed_html("OK"))

    monkeypatch.setattr(fake, "handler", handler)
    result = asyncio.run(youtube.check_video_embeddability(VID))
    assert result["thumbnail_url"] == f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg"


# ---------------------------------------------------------------------------
# #8 search uses the Data API (only) when a key is configured
# ---------------------------------------------------------------------------
def test_search_with_api_key_uses_data_api_and_never_scrapes(fake, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    fake.videos = {
        VID: _api_video(VID, title="Good &amp; plain"),
        VID2: _api_video(VID2, embeddable=False),
        VID3: _api_video(VID3, age=True),
    }
    results = asyncio.run(youtube.search_embeddable_youtube_songs("never gonna", max_results=8))

    assert [r["video_id"] for r in results] == [VID]
    assert results[0]["embeddable"] is True
    assert results[0]["title"] == "Good &amp; plain"  # passed through as data, not re-parsed
    hosts_paths = [(r.url.host, r.url.path) for r in fake.requests]
    assert hosts_paths == [
        ("www.googleapis.com", "/youtube/v3/search"),
        ("www.googleapis.com", "/youtube/v3/videos"),
    ]
    search_req, videos_req = fake.requests
    assert search_req.url.params["q"] == "never gonna"
    assert search_req.url.params["videoEmbeddable"] == "true"
    assert set(videos_req.url.params["part"].split(",")) >= {"status", "contentDetails"}
    assert videos_req.url.params["id"].split(",") == [VID, VID2, VID3]  # bad id dropped
    for req in fake.requests:
        assert req.headers["user-agent"] == HONEST_UA
        assert "cookie" not in req.headers
        assert "referer" not in req.headers


def test_search_api_failure_does_not_fall_back_to_scraping(fake, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    fake.api_status = 403
    results = asyncio.run(youtube.search_embeddable_youtube_songs("anything"))
    assert results == []
    assert all(r.url.host == "www.googleapis.com" for r in fake.requests)


def test_search_without_api_key_uses_dev_only_fallback(fake):
    results = asyncio.run(youtube.search_embeddable_youtube_songs("anything", max_results=2))
    assert {r["video_id"] for r in results} == {VID, VID2}
    assert any(r.url.path == "/results" for r in fake.requests)
    assert not any(r.url.host == "www.googleapis.com" for r in fake.requests)


def test_search_query_is_capped_at_200_chars(fake, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    fake.videos = {VID: _api_video(VID)}
    asyncio.run(youtube.search_embeddable_youtube_songs("q" * 5000))
    assert len(fake.requests[0].url.params["q"]) == youtube.MAX_SEARCH_QUERY_CHARS == 200


# ---------------------------------------------------------------------------
# #6 TTL cache and bounded outbound concurrency
# ---------------------------------------------------------------------------
def test_embeddability_result_is_cached(fake):
    first = asyncio.run(youtube.check_video_embeddability(VID))
    n = len(fake.requests)
    second = asyncio.run(youtube.check_video_embeddability(VID))
    assert second == first
    assert len(fake.requests) == n


def test_custom_title_does_not_poison_cache(fake):
    meta = asyncio.run(youtube.fetch_youtube_metadata(VID, custom_title="Attacker title"))
    assert meta["title"] == "Attacker title"
    again = asyncio.run(youtube.check_video_embeddability(VID))
    assert again["title"] == "Real Title"


def test_transient_failures_are_not_cached(fake):
    fake.oembed_status = 503
    assert asyncio.run(youtube.check_video_embeddability(VID))["embeddable"] is False
    fake.oembed_status = 200
    assert asyncio.run(youtube.check_video_embeddability(VID))["embeddable"] is True


def test_search_results_are_cached(fake, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    fake.videos = {VID: _api_video(VID)}
    asyncio.run(youtube.search_embeddable_youtube_songs("cached query"))
    n = len(fake.requests)
    again = asyncio.run(youtube.search_embeddable_youtube_songs("cached query"))
    assert [r["video_id"] for r in again] == [VID]
    assert len(fake.requests) == n


def test_outbound_checks_are_bounded_by_semaphore(fake):
    fake.delay = 0.02
    ids = [f"{i:011d}" for i in range(12)]

    async def run():
        async with httpx.AsyncClient() as client:  # shared client, like audit/inspect
            return await asyncio.gather(*(youtube.check_video_embeddability(v, client=client) for v in ids))

    results = asyncio.run(run())
    assert len(results) == 12
    assert 1 <= fake.max_in_flight_oembed <= youtube.MAX_CONCURRENT_OUTBOUND == 4


def test_semaphore_works_across_event_loops(fake):
    # TestClient and asyncio.run create fresh loops; a module-level Semaphore bound to
    # an old loop would raise. Run contended work on two separate loops.
    fake.delay = 0.01
    for _ in range(2):
        youtube.clear_youtube_caches()

        async def run():
            return await asyncio.gather(*(youtube.check_video_embeddability(f"{i:011d}") for i in range(6)))

        assert len(asyncio.run(run())) == 6


def test_ttl_cache_expiry_and_size_bound():
    cache = youtube._TTLCache(ttl_seconds=60, max_entries=3)
    cache.set("gone", {"v": 1}, ttl_seconds=0)
    assert cache.get("gone") is None
    for i in range(5):
        cache.set(i, {"v": i})
    assert len(cache) == 3
    assert cache.get(0) is None and cache.get(4) == {"v": 4}
    got = cache.get(4)
    got["v"] = "mutated"
    assert cache.get(4) == {"v": 4}
