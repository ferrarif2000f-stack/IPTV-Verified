#!/usr/bin/env python3
"""Build M3U playlists containing every HLS stream with a downloadable segment."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


SOURCE_URL = "https://raw.githubusercontent.com/sacuar/MyIPTV/refs/heads/main/adult.m3u"
USER_AGENT = "Mozilla/5.0 (compatible; IPTV-Verified/1.0)"
PLAYLIST_LIMIT = 2 * 1024 * 1024
SEGMENTS_TO_TRY = 4


@dataclass(frozen=True)
class Entry:
    info: str
    url: str


def fetch(url: str, timeout: float, *, limit: int | None = None) -> tuple[int, bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        body = response.read(limit) if limit is not None else response.read()
        return response.status, body, response.geturl()


def parse_source(data: bytes) -> list[Entry]:
    text = data.decode("utf-8-sig", errors="replace")
    entries: list[Entry] = []
    seen_urls: set[str] = set()
    info: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXTINF:"):
            info = line
        elif line and not line.startswith("#"):
            if (
                info is not None
                and ".m3u8" in line.lower()
                and line not in seen_urls
            ):
                entries.append(Entry(info, line))
                seen_urls.add(line)
            info = None
    return entries


def playlist_kind(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0].lstrip("\ufeff") != "#EXTM3U":
        return None
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        return "master"
    if (
        any(line.startswith("#EXTINF:") for line in lines)
        and any(line.startswith("#EXT-X-TARGETDURATION:") for line in lines)
        and any(not line.startswith("#") for line in lines)
    ):
        return "media"
    return None


def referenced_uris(text: str, marker: str | None = None) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    result: list[str] = []
    if marker is None:
        return [line for line in lines if line and not line.startswith("#")]
    for index, line in enumerate(lines):
        if line.startswith(marker):
            for following in lines[index + 1 :]:
                if following and not following.startswith("#"):
                    result.append(following)
                    break
    return result


def media_has_segment(url: str, text: str, timeout: float) -> bool:
    for uri in referenced_uris(text)[:SEGMENTS_TO_TRY]:
        try:
            status, body, _ = fetch(urljoin(url, uri), timeout, limit=1)
            if status == 200 and body:
                return True
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
    return False


def verify(entry: Entry, timeout: float) -> bool:
    try:
        status, body, final_url = fetch(entry.url, timeout, limit=PLAYLIST_LIMIT)
        if status != 200:
            return False
        text = body.decode("utf-8-sig", errors="replace")
        kind = playlist_kind(text)
        if kind == "media":
            return media_has_segment(final_url, text, timeout)
        if kind != "master":
            return False

        for variant in referenced_uris(text, "#EXT-X-STREAM-INF:"):
            try:
                status, media_body, media_url = fetch(
                    urljoin(final_url, variant), timeout, limit=PLAYLIST_LIMIT
                )
                if status != 200:
                    continue
                media_text = media_body.decode("utf-8-sig", errors="replace")
                if playlist_kind(media_text) == "media" and media_has_segment(
                    media_url, media_text, timeout
                ):
                    return True
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                continue
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        pass
    return False


def write_outputs(
    entries: list[Entry],
    total_candidates: int,
    source: str,
    outputs: list[Path],
    report: Path,
) -> None:
    playlist = "#EXTM3U\n" + "".join(
        f"{entry.info}\n{entry.url}\n" for entry in entries
    )
    for output in outputs:
        output.write_text(playlist, encoding="utf-8")
    report.write_text(
        json.dumps(
            {
                "test_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": source,
                "total_candidates": total_candidates,
                "urls_tested": total_candidates,
                "urls_passed": len(entries),
                "urls_failed": total_candidates - len(entries),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_URL)
    parser.add_argument("--output", type=Path, default=Path("verified-all.m3u"))
    parser.add_argument("--legacy-output", type=Path, default=Path("verified-100.m3u"))
    parser.add_argument("--report", type=Path, default=Path("verification-report.json"))
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--workers", type=int, default=64)
    args = parser.parse_args()

    status, source_data, _ = fetch(args.source, args.timeout)
    if status != 200:
        raise RuntimeError(f"source returned HTTP {status}")
    candidates = parse_source(source_data)
    print(f"Testing all {len(candidates)} unique HLS candidates...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(lambda item: verify(item, args.timeout), candidates))
    passed = [entry for entry, ok in zip(candidates, results) if ok]

    write_outputs(
        passed,
        len(candidates),
        args.source,
        [args.output, args.legacy_output],
        args.report,
    )
    print(f"Wrote {len(passed)} verified entries after testing {len(candidates)} URLs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
