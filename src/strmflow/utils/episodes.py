from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from urllib.parse import unquote


def source_season_episode(source: str) -> tuple[int | None, int | None]:
    """Extract a season/episode identity without mistaking quality numbers for episodes."""
    normalized = unquote(source.replace("\\", "/").strip("/"))
    *directories, filename = normalized.split("/")
    directory_season: int | None = None
    for segment in reversed(directories):
        match = re.match(
            r"(?i)^\s*(?:season[ ._-]*0*(\d{1,2})|s0*(\d{1,2})(?:\b|[ ._-])|第\s*0*(\d{1,2})\s*季)",
            segment,
        )
        if match:
            directory_season = int(next(group for group in match.groups() if group is not None))
            break

    base = filename[: -len(PurePosixPath(filename).suffix)] if "." in filename else filename
    season_episode = re.search(
        r"(?i)(?:Season[ ._-]*0*(\d{1,2})[ ._-]+Episode[ ._-]*0*(\d{1,4})|"
        r"(?:^|[ ._\-()[\]【】])S0*(\d{1,2})[ ._-]*E(?:P)?0*(\d{1,4})"
        r"(?=$|[ ._\-()[\]【】])|第\s*0*(\d{1,2})\s*季.*?第\s*0*(\d{1,4})\s*[集话話]|"
        r"(?<!\d)(\d{1,2})x0*(\d{1,4})(?!\d))",
        base,
    )
    filename_season: int | None = None
    episode: int | None = None
    if season_episode:
        groups = season_episode.groups()
        for index in range(0, len(groups), 2):
            if groups[index] is not None:
                filename_season = int(groups[index])
                episode = int(groups[index + 1])
                break
    if episode is None:
        episode_match = re.search(
            r"(?i)(?:第\s*0*(\d{1,4})\s*[集话話]|"
            r"(?:^|[ ._\-()[\]【】])(?:EP|E)0*(\d{1,4})(?=$|[ ._\-()[\]【】])|"
            r"^\s*0*(\d{1,4})(?=$|[ ._\-()[\]【】]))",
            base,
        )
        if episode_match:
            episode = int(next(group for group in episode_match.groups() if group is not None))
    return directory_season if directory_season is not None else filename_season, episode


def media_quality_rank(source: str, size: int = 0) -> tuple[int, ...]:
    """Rank alternate encodes deterministically, preferring quality then the canonical name."""
    name = unquote(PurePosixPath(source).stem).casefold()
    resolution = _highest_match(
        name,
        (
            (4320, (r"(?<![a-z0-9])(?:8k|4320p?)(?![a-z])",)),
            (2160, (r"(?<![a-z0-9])(?:4k|uhd|2160p?)(?![a-z])",)),
            (1440, (r"(?<![a-z0-9])(?:2k|1440p?)(?![a-z])",)),
            (1080, (r"\b1080[pi]?\b",)),
            (720, (r"\b720p?\b",)),
            (576, (r"\b576p?\b",)),
            (480, (r"\b480p?\b",)),
        ),
    )
    source_quality = _highest_match(
        name,
        (
            (6, (r"\b(?:remux)\b",)),
            (5, (r"\b(?:blu[ ._-]?ray|bdrip|bdremux)\b",)),
            (4, (r"\b(?:web[ ._-]?dl|webdl)\b",)),
            (3, (r"\b(?:web[ ._-]?rip|web)\b",)),
            (2, (r"\b(?:hdtv)\b",)),
            (1, (r"\b(?:dvdrip|dvd)\b",)),
        ),
    )
    dynamic_range = _highest_match(
        name,
        (
            (4, (r"\b(?:dolby[ ._-]?vision|dovi|dv)\b",)),
            (3, (r"\b(?:hdr10\+|hdr10plus)\b",)),
            (2, (r"\b(?:hdr10|hdr)\b",)),
            (1, (r"\b(?:sdr)\b",)),
        ),
    )
    codec = _highest_match(
        name,
        (
            (4, (r"\b(?:av1)\b",)),
            (3, (r"\b(?:h[ ._-]?265|x265|hevc)\b",)),
            (2, (r"\b(?:h[ ._-]?264|x264|avc)\b",)),
        ),
    )
    frame_rate_match = re.search(r"(?<!\d)(120|60|50|30|25|24)\s*fps\b", name)
    frame_rate = int(frame_rate_match.group(1)) if frame_rate_match else 0
    canonical_name = 0 if re.search(r"(?:\(\d+\)|[ ._-](?:copy|副本)|-\d+)$", name) else 1
    return (
        resolution,
        source_quality,
        dynamic_range,
        codec,
        frame_rate,
        canonical_name,
        max(0, int(size or 0)),
    )


def select_preferred_episodes[T](
    entries: Sequence[T],
    *,
    path: Callable[[T], str],
    size: Callable[[T], int] | None = None,
    default_season: int = 1,
) -> tuple[list[T], list[T]]:
    """Keep one preferred file per detected SxxExx identity.

    Entries without a reliable episode identity are retained so movies, specials,
    subtitles and artwork are never discarded by an uncertain filename guess.
    """
    size_of = size or (lambda _entry: 0)
    winners: dict[tuple[int, int], tuple[int, tuple[int, ...]]] = {}
    duplicate_indexes: set[int] = set()
    for index, entry in enumerate(entries):
        source = path(entry)
        season, episode = source_season_episode(source)
        if episode is None:
            continue
        identity = (season or max(1, int(default_season or 1)), episode)
        rank = media_quality_rank(source, size_of(entry))
        current = winners.get(identity)
        if current is None:
            winners[identity] = (index, rank)
            continue
        current_index, current_rank = current
        if rank > current_rank:
            duplicate_indexes.add(current_index)
            duplicate_indexes.discard(index)
            winners[identity] = (index, rank)
        else:
            duplicate_indexes.add(index)
    preferred = [entry for index, entry in enumerate(entries) if index not in duplicate_indexes]
    duplicates = [entry for index, entry in enumerate(entries) if index in duplicate_indexes]
    return preferred, duplicates


def _highest_match(name: str, rules: tuple[tuple[int, tuple[str, ...]], ...]) -> int:
    return next(
        (
            score
            for score, patterns in rules
            if any(re.search(pattern, name) for pattern in patterns)
        ),
        0,
    )
