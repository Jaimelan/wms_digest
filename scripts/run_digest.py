#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import configparser
import dataclasses
import datetime as dt
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "science_digest.ini"
USER_AGENT = "science-digest/0.1 (+https://github.com/)"


@dataclasses.dataclass(frozen=True)
class Paper:
    source: str
    source_id: str
    title: str
    authors: list[str]
    published: str
    venue: str
    abstract: str
    url: str
    doi: str = ""
    is_preprint: bool = False
    cited_by: int = 0
    categories: list[str] = dataclasses.field(default_factory=list)
    score: int = 0
    score_reasons: list[str] = dataclasses.field(default_factory=list)

    def key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower()
        normalized_title = normalize_text(self.title)
        if normalized_title:
            return "title:" + normalized_title
        return f"{self.source}:{self.source_id}"


def main() -> int:
    args = parse_args()
    config = load_config(args.config)

    today = parse_date(args.date) if args.date else dt.date.today()
    lookback_days = int(config["digest"]["lookback_days"])
    start_date = today - dt.timedelta(days=lookback_days)
    output_dir = resolve_output_dir(args.output_dir or config["digest"].get("output_dir", "docs"))
    max_papers = int(config["digest"]["max_papers"])
    min_score = int(config["digest"]["min_relevance_score"])

    print(f"Collecting papers from {start_date.isoformat()} through {today.isoformat()}")
    papers = collect_papers(config, start_date, today)
    ranked = rank_and_filter(papers, config, min_score=min_score, max_papers=max_papers)
    ranked = resolve_missing_dois(ranked, config)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_outputs(config, output_dir, today, start_date, ranked)
    cleanup_old_outputs(output_dir, today, int(config["digest"]["retention_days"]))
    write_index(config, output_dir, today)

    print(f"Wrote {len(ranked)} papers to {output_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a rolling science digest.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--date", help="Run date in YYYY-MM-DD form. Defaults to today.")
    parser.add_argument("--output-dir", type=Path, help="Override the configured output directory.")
    return parser.parse_args()


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def resolve_output_dir(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_config(path: Path) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None)
    read_files = parser.read(path)
    if not read_files:
        raise FileNotFoundError(path)

    return {
        "digest": {
            "title": parser.get("digest", "title"),
            "lookback_days": parser.getint("digest", "lookback_days"),
            "retention_days": parser.getint("digest", "retention_days"),
            "max_papers": parser.getint("digest", "max_papers"),
            "min_relevance_score": parser.getint("digest", "min_relevance_score"),
            "podcast_target_minutes": parser.getint("digest", "podcast_target_minutes"),
            "output_dir": parser.get("digest", "output_dir", fallback="docs"),
        },
        "sources": {
            "enable_europe_pmc": parser.getboolean("sources", "enable_europe_pmc", fallback=True),
            "enable_arxiv": parser.getboolean("sources", "enable_arxiv", fallback=False),
        },
        "audio": {
            "enabled": parser.getboolean("audio", "enabled", fallback=True),
            "provider": parser.get("audio", "provider", fallback="edge_tts"),
            "voice": parser.get("audio", "voice", fallback="en-GB-RyanNeural"),
            "rate": parser.get("audio", "rate", fallback="+0%"),
        },
        "doi": {
            "enable_crossref_lookup": parser.getboolean("doi", "enable_crossref_lookup", fallback=True),
            "crossref_mailto": parser.get("doi", "crossref_mailto", fallback="").strip(),
        },
        "notion": {
            "enabled": parser.getboolean("notion", "enabled", fallback=False),
            "title_property": parser.get("notion", "title_property", fallback="Name"),
        },
        "queries": {
            "europe_pmc": parser.get("queries", "europe_pmc"),
            "arxiv": parser.get("queries", "arxiv"),
        },
        "filters": {
            "required_human_health_terms": multiline_list(
                parser.get("filters", "required_human_health_terms", fallback="")
            ),
            "required_focus_terms": multiline_list(parser.get("filters", "required_focus_terms", fallback="")),
            "excluded_terms": multiline_list(parser.get("filters", "excluded_terms", fallback="")),
        },
        "ranking": {
            "core_terms": multiline_list(parser.get("ranking", "core_terms")),
            "human_health_terms": multiline_list(parser.get("ranking", "human_health_terms")),
            "female_reproductive_terms": multiline_list(
                parser.get("ranking", "female_reproductive_terms", fallback="")
            ),
            "methods_terms": multiline_list(parser.get("ranking", "methods_terms")),
        },
    }


def collect_papers(config: dict[str, Any], start_date: dt.date, end_date: dt.date) -> list[Paper]:
    enabled_sources: list[str] = []
    failed_sources: list[str] = []

    papers: list[Paper] = []
    if config["sources"].get("enable_europe_pmc", True):
        enabled_sources.append("Europe PMC")
        europe_pmc = fetch_europe_pmc(config["queries"]["europe_pmc"], start_date, end_date)
        if europe_pmc is None:
            failed_sources.append("Europe PMC")
        else:
            papers.extend(europe_pmc)

    if config["sources"].get("enable_arxiv", False):
        enabled_sources.append("arXiv")
        arxiv = fetch_arxiv(config["queries"]["arxiv"], start_date, end_date)
        if arxiv is None:
            failed_sources.append("arXiv")
        else:
            papers.extend(arxiv)
    else:
        print("arXiv source is disabled in config; skipping arXiv fetch.")

    if not enabled_sources:
        raise RuntimeError("No literature sources are enabled.")
    if len(failed_sources) == len(enabled_sources):
        raise RuntimeError("All enabled literature sources failed; refusing to write an empty digest.")

    return dedupe_papers(papers)


def fetch_json(url: str, params: dict[str, str], timeout: int = 30) -> dict[str, Any]:
    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str, params: dict[str, str], timeout: int = 30) -> str:
    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def fetch_text_with_retries(
    url: str,
    params: dict[str, str],
    *,
    timeout: int = 30,
    retries: int = 2,
    backoff_seconds: int = 15,
) -> str:
    for attempt in range(retries + 1):
        try:
            return fetch_text(url, params, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == retries:
                raise
            wait_seconds = backoff_seconds * (attempt + 1)
            print(f"arXiv rate-limited the request; retrying in {wait_seconds} seconds.", file=sys.stderr)
            time.sleep(wait_seconds)
    raise RuntimeError("unreachable arXiv retry state")


def fetch_europe_pmc(query: str, start_date: dt.date, end_date: dt.date) -> list[Paper] | None:
    compact_query = " ".join(query.split())
    dated_query = (
        f"({compact_query}) AND FIRST_PDATE:[{start_date.isoformat()} TO {end_date.isoformat()}] "
        "sort_date:y"
    )
    params = {
        "query": dated_query,
        "format": "json",
        "resultType": "core",
        "pageSize": "100",
        "synonym": "true",
    }

    try:
        payload = fetch_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Europe PMC fetch failed: {exc}", file=sys.stderr)
        return None

    results = payload.get("resultList", {}).get("result", [])
    papers: list[Paper] = []
    for item in results:
        title = clean_markup(item.get("title", ""))
        abstract = clean_markup(item.get("abstractText", ""))
        if not title:
            continue
        source = str(item.get("source", "Europe PMC"))
        source_id = str(item.get("id", item.get("pmid", "")))
        doi = str(item.get("doi", "") or "")
        url = f"https://europepmc.org/article/{source}/{source_id}"
        if doi:
            url = "https://doi.org/" + doi
        authors = split_authors(item.get("authorString", ""))
        published = first_nonempty(
            item.get("firstPublicationDate"),
            item.get("electronicPublicationDate"),
            item.get("printPublicationDate"),
            item.get("pubYear"),
        )
        venue = first_nonempty(item.get("journalTitle"), item.get("bookOrReportDetails"), source)
        pub_types = item.get("pubTypeList", {}).get("pubType", [])
        is_preprint = source.upper() == "PPR" or any("preprint" in str(kind).lower() for kind in pub_types)
        papers.append(
            Paper(
                source="Europe PMC",
                source_id=source_id,
                title=title,
                authors=authors,
                published=normalize_date_string(published),
                venue=venue,
                abstract=abstract,
                url=url,
                doi=doi,
                is_preprint=is_preprint,
                cited_by=safe_int(item.get("citedByCount", 0)),
                categories=[str(kind) for kind in pub_types],
            )
        )
    return papers


def fetch_arxiv(query: str, start_date: dt.date, end_date: dt.date) -> list[Paper] | None:
    compact_query = " ".join(query.split())
    start_stamp = start_date.strftime("%Y%m%d") + "000000"
    end_stamp = end_date.strftime("%Y%m%d") + "235959"
    dated_query = f"({compact_query}) AND submittedDate:[{start_stamp} TO {end_stamp}]"
    params = {
        "search_query": dated_query,
        "start": "0",
        "max_results": "25",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    try:
        text = fetch_text_with_retries("https://export.arxiv.org/api/query", params)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"arXiv fetch failed: {exc}", file=sys.stderr)
        return None

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        print(f"arXiv parse failed: {exc}", file=sys.stderr)
        return None

    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ns):
        title = clean_markup(find_text(entry, "atom:title", ns))
        abstract = clean_markup(find_text(entry, "atom:summary", ns))
        source_url = find_text(entry, "atom:id", ns)
        source_id = source_url.rstrip("/").split("/")[-1]
        published = normalize_date_string(find_text(entry, "atom:published", ns))
        authors = [
            clean_markup(find_text(author, "atom:name", ns))
            for author in entry.findall("atom:author", ns)
            if find_text(author, "atom:name", ns)
        ]
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", ns)
            if category.attrib.get("term")
        ]
        doi = find_text(entry, "arxiv:doi", ns)
        url = source_url or f"https://arxiv.org/abs/{source_id}"
        papers.append(
            Paper(
                source="arXiv",
                source_id=source_id,
                title=title,
                authors=authors,
                published=published,
                venue="arXiv",
                abstract=abstract,
                url=url,
                doi=doi,
                is_preprint=True,
                categories=categories,
            )
        )
    time.sleep(3)
    return papers


def find_text(node: ET.Element, path: str, ns: dict[str, str]) -> str:
    child = node.find(path, ns)
    return child.text.strip() if child is not None and child.text else ""


def dedupe_papers(papers: list[Paper]) -> list[Paper]:
    by_key: dict[str, Paper] = {}
    for paper in papers:
        key = paper.key()
        current = by_key.get(key)
        if current is None:
            by_key[key] = paper
            continue
        by_key[key] = prefer_richer_paper(current, paper)
    return list(by_key.values())


def prefer_richer_paper(left: Paper, right: Paper) -> Paper:
    left_score = len(left.abstract) + len(left.doi) * 25 + left.cited_by
    right_score = len(right.abstract) + len(right.doi) * 25 + right.cited_by
    return right if right_score > left_score else left


def rank_and_filter(
    papers: list[Paper], config: dict[str, Any], min_score: int, max_papers: int
) -> list[Paper]:
    scored = [
        score_paper(paper, config["ranking"])
        for paper in papers
        if passes_topic_filters(paper, config.get("filters", {}))
    ]
    filtered_count = len(papers) - len(scored)
    if filtered_count:
        print(f"Filtered {filtered_count} off-topic papers with the configured medical/female-health filters.")
    filtered = [paper for paper in scored if paper.score >= min_score]
    filtered.sort(key=lambda paper: (paper.score, paper.cited_by, paper.published), reverse=True)
    return filtered[:max_papers]


def passes_topic_filters(paper: Paper, filters: dict[str, list[str]]) -> bool:
    haystack = paper_haystack(paper)
    excluded_terms = filters.get("excluded_terms", [])
    if any(has_term(haystack, term) for term in excluded_terms):
        return False

    human_terms = filters.get("required_human_health_terms", [])
    if human_terms and not any(has_term(haystack, term) for term in human_terms):
        return False

    focus_terms = filters.get("required_focus_terms", [])
    if focus_terms and not any(has_term(haystack, term) for term in focus_terms):
        return False

    return True


def score_paper(paper: Paper, ranking: dict[str, list[str]]) -> Paper:
    haystack = paper_haystack(paper)
    score = 0
    reasons: list[str] = []

    weighted_groups = [
        ("core metagenomics", ranking.get("core_terms", []), 3),
        ("female reproductive health", ranking.get("female_reproductive_terms", []), 4),
        ("human health", ranking.get("human_health_terms", []), 2),
        ("methods and benchmarks", ranking.get("methods_terms", []), 2),
    ]
    for label, terms, weight in weighted_groups:
        hits = [term for term in terms if has_term(haystack, term)]
        if hits:
            score += min(len(hits), 4) * weight
            reasons.append(label)

    if paper.abstract:
        score += 1
    if paper.is_preprint:
        reasons.append("preprint")
    if paper.cited_by:
        score += min(3, paper.cited_by // 5)
        reasons.append(f"{paper.cited_by} Europe PMC citations")

    return dataclasses.replace(paper, score=score, score_reasons=unique_preserving_order(reasons))


def resolve_missing_dois(papers: list[Paper], config: dict[str, Any]) -> list[Paper]:
    if not config.get("doi", {}).get("enable_crossref_lookup", True):
        return papers

    resolved: list[Paper] = []
    mailto = config.get("doi", {}).get("crossref_mailto", "")
    for paper in papers:
        if paper.doi:
            resolved.append(paper)
            continue
        doi = lookup_crossref_doi(paper.title, mailto=mailto)
        if doi:
            resolved.append(dataclasses.replace(paper, doi=doi))
            print(f"Resolved DOI for '{paper.title}': {doi}")
        else:
            resolved.append(paper)
        time.sleep(1)
    return resolved


def lookup_crossref_doi(title: str, mailto: str = "") -> str:
    if not title:
        return ""
    params = {
        "query.title": title,
        "rows": "3",
        "select": "DOI,title",
    }
    if mailto:
        params["mailto"] = mailto
    try:
        payload = fetch_json("https://api.crossref.org/works", params, timeout=20)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Crossref DOI lookup failed for '{title}': {exc}", file=sys.stderr)
        return ""

    items = payload.get("message", {}).get("items", [])
    for item in items:
        candidate_titles = item.get("title") or []
        candidate_title = candidate_titles[0] if candidate_titles else ""
        if titles_match(title, candidate_title):
            return str(item.get("DOI", "")).strip()
    return ""


def titles_match(left: str, right: str) -> bool:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio() >= 0.88


def write_outputs(
    config: dict[str, Any],
    output_dir: Path,
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
) -> None:
    digest_dir = output_dir / "digests"
    podcast_dir = output_dir / "podcasts"
    data_dir = output_dir / "data"
    for directory in (digest_dir, podcast_dir, data_dir):
        directory.mkdir(parents=True, exist_ok=True)

    issue_id = today.isoformat()
    general_summary = render_general_summary(config, today, start_date, papers)
    digest_md = render_digest(config, today, start_date, papers, general_summary)
    script_md = render_podcast_script(config, today, start_date, papers, digest_md, general_summary)

    (digest_dir / f"{issue_id}.md").write_text(digest_md, encoding="utf-8")
    (podcast_dir / f"{issue_id}-script.md").write_text(script_md, encoding="utf-8")
    (data_dir / f"{issue_id}.json").write_text(
        json.dumps([dataclasses.asdict(paper) for paper in papers], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    maybe_generate_audio(config, podcast_dir, issue_id, script_md)
    maybe_publish_to_notion(config, data_dir, issue_id, today, start_date, papers, general_summary)


def render_digest(
    config: dict[str, Any],
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
    general_summary: str,
) -> str:
    title = config["digest"]["title"]
    if not papers:
        return textwrap.dedent(
            f"""\
            # {title} Digest: {today.isoformat()}

            Window: {start_date.isoformat()} to {today.isoformat()}

            No matching papers or preprints were found in the configured sources for this window.
            """
        )

    theme_counts = extract_theme_counts(papers)
    lines = [
        f"# {title} Digest: {today.isoformat()}",
        "",
        f"Window: {start_date.isoformat()} to {today.isoformat()}",
        "",
        "This digest is generated from free metadata sources. Audio narration, when present, is generated with Edge TTS.",
        "",
        "## General Summary",
        "",
        general_summary,
        "",
        "## Main Themes",
        "",
    ]
    for theme, count in theme_counts:
        lines.append(f"- **{theme}**: {count} paper{'s' if count != 1 else ''}")

    lines.extend(["", "## Papers", ""])
    for index, paper in enumerate(papers, start=1):
        authors = format_authors(paper.authors)
        venue = paper.venue or paper.source
        status = "preprint" if paper.is_preprint else "paper"
        reasons = ", ".join(paper.score_reasons) if paper.score_reasons else "topic match"
        lines.extend(
            [
                f"### {index}. {paper.title}",
                "",
                f"- **Source**: {venue} ({status}, {paper.published or 'date unavailable'})",
                f"- **Authors**: {authors}",
                f"- **Why it matters**: {make_takeaway(paper)}",
                f"- **Study summary**: {make_study_summary(paper)}",
                f"- **Topic signals**: {reasons}; relevance score {paper.score}",
                f"- **DOI**: {format_doi(paper.doi)}",
                f"- **Link**: {paper.url}",
            ]
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_podcast_script(
    config: dict[str, Any],
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
    digest_md: str,
    general_summary: str,
) -> str:
    return render_deterministic_script(config, today, start_date, papers, digest_md, general_summary)


def render_deterministic_script(
    config: dict[str, Any],
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
    digest_md: str,
    general_summary: str,
) -> str:
    title = config["digest"]["title"]
    if not papers:
        return textwrap.dedent(
            f"""\
            # Podcast Script: {today.isoformat()}

            Audio disclosure: if this script is converted to speech, the voice is synthetic and generated with Edge TTS.

            No matching papers or preprints were found for {start_date.isoformat()} through {today.isoformat()}.
            """
        )

    theme_counts = extract_theme_counts(papers)
    lines = [
        f"# Podcast Script: {title} - {today.isoformat()}",
        "",
        "Audio disclosure: this script is converted to speech with Edge TTS.",
        "",
        f"Welcome to this week in {title.lower()}, covering {start_date.isoformat()} through {today.isoformat()}.",
        f"This episode covers {len(papers)} papers and preprints selected from free literature metadata sources.",
        general_summary,
        "",
        "The main themes this week are "
        + natural_join([f"{theme.lower()} ({count})" for theme, count in theme_counts])
        + ".",
        "",
    ]
    for index, paper in enumerate(papers, start=1):
        preprint_note = " This is a preprint, so treat its conclusions as provisional." if paper.is_preprint else ""
        lines.extend(
            [
                f"Segment {index}: {paper.title}.",
                f"{format_authors(paper.authors)} report this in {paper.venue or paper.source}.{preprint_note}",
                f"DOI: {paper.doi or 'not available from metadata or Crossref lookup'}.",
                make_takeaway(paper),
                make_study_summary(paper),
                "",
            ]
        )
    lines.extend(
        [
            "Across the set, the practical thread is clear: whole metagenome sequencing keeps moving in two directions at once.",
            "One direction is clinical interpretation, where studies connect microbial community signals to female reproductive health, disease, pregnancy, fertility, and treatment contexts.",
            "The other is infrastructure: better benchmarks, software, databases, quality control, assembly, binning, and profiling methods that make human clinical results more reproducible.",
            "That is the digest for this window.",
            "",
            "<!-- Source digest used for fallback script generation. -->",
            "<details>",
            "<summary>Digest source</summary>",
            "",
            digest_md,
            "</details>",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def maybe_generate_audio(config: dict[str, Any], podcast_dir: Path, issue_id: str, script_md: str) -> None:
    audio_config = config.get("audio", {})
    if not audio_config.get("enabled", True):
        print("Audio generation is disabled in config.")
        return
    if audio_config.get("provider", "edge_tts") != "edge_tts":
        print(f"Unsupported audio provider '{audio_config.get('provider')}'; skipping audio generation.")
        return
    try:
        import edge_tts
    except ImportError:
        print("edge-tts is not installed; skipping audio generation.", file=sys.stderr)
        return

    voice = audio_config.get("voice", "en-GB-RyanNeural")
    rate = audio_config.get("rate", "+0%")
    narration_text = markdown_to_narration(script_md)
    chunks = chunk_text(narration_text, limit=3500)
    if not chunks:
        print("Podcast script is empty; skipping audio generation.", file=sys.stderr)
        return

    mp3_path = podcast_dir / f"{issue_id}.mp3"
    part_paths = [podcast_dir / f"{issue_id}-part{index:02d}.mp3" for index in range(1, len(chunks) + 1)]
    try:
        asyncio.run(write_edge_tts_parts(edge_tts, chunks, part_paths, voice=voice, rate=rate))
        if combine_mp3_files(part_paths, mp3_path):
            for part_path in part_paths:
                part_path.unlink(missing_ok=True)
            print(f"Wrote podcast audio to {mp3_path}")
        else:
            print("Could not combine podcast audio parts; leaving per-part MP3 files in place.", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - keep scheduled job resilient.
        print(f"Edge TTS audio generation failed: {exc}", file=sys.stderr)
        for part_path in part_paths:
            part_path.unlink(missing_ok=True)


async def write_edge_tts_parts(edge_tts_module: Any, chunks: list[str], paths: list[Path], voice: str, rate: str) -> None:
    for chunk, path in zip(chunks, paths):
        communicate = edge_tts_module.Communicate(chunk, voice=voice, rate=rate)
        await communicate.save(str(path))


def combine_mp3_files(parts: list[Path], destination: Path) -> bool:
    existing_parts = [part for part in parts if part.exists()]
    if not existing_parts:
        return False
    if len(existing_parts) == 1:
        shutil.move(str(existing_parts[0]), destination)
        return True

    if not shutil.which("ffmpeg"):
        print("ffmpeg is not installed; cannot combine MP3 chunks.", file=sys.stderr)
        return False

    concat_file = destination.with_suffix(".concat.txt")
    concat_file.write_text(
        "".join(f"file '{part.resolve()}'\n" for part in existing_parts),
        encoding="utf-8",
    )
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ffmpeg MP3 concatenation failed: {exc}", file=sys.stderr)
        return False
    finally:
        concat_file.unlink(missing_ok=True)
    return True


def cleanup_old_outputs(output_dir: Path, today: dt.date, retention_days: int) -> None:
    cutoff = today - dt.timedelta(days=retention_days)
    for directory in (output_dir / "digests", output_dir / "podcasts", output_dir / "data"):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            file_date = date_from_filename(path.name)
            if file_date and file_date < cutoff:
                path.unlink()


def write_index(config: dict[str, Any], output_dir: Path, today: dt.date) -> None:
    title = config["digest"]["title"]
    digest_dir = output_dir / "digests"
    podcast_dir = output_dir / "podcasts"
    issues = sorted(
        [path for path in digest_dir.glob("*.md") if date_from_filename(path.name)],
        key=lambda path: path.name,
        reverse=True,
    )
    lines = [
        f"# {title}",
        "",
        f"Last updated: {today.isoformat()}",
        "",
        "Generated digest pages and audio narration for recent human-health metagenomics literature, with a focus on female reproductive health.",
        "",
        "## Recent Digests",
        "",
    ]
    if not issues:
        lines.append("No digests have been generated yet.")
    for digest_path in issues:
        issue_id = digest_path.stem
        links = [f"[digest](digests/{digest_path.name})"]
        mp3 = podcast_dir / f"{issue_id}.mp3"
        wav = podcast_dir / f"{issue_id}.wav"
        script = podcast_dir / f"{issue_id}-script.md"
        if mp3.exists():
            links.append(f"[audio](podcasts/{mp3.name})")
        elif wav.exists():
            links.append(f"[audio](podcasts/{wav.name})")
        if script.exists():
            links.append(f"[script](podcasts/{script.name})")
        lines.append(f"- **{issue_id}**: " + " | ".join(links))
    (output_dir / "index.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def maybe_publish_to_notion(
    config: dict[str, Any],
    data_dir: Path,
    issue_id: str,
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
    general_summary: str,
) -> None:
    notion_config = config.get("notion", {})
    if not notion_config.get("enabled", False):
        return

    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not token or not database_id:
        print("Notion publishing is enabled, but NOTION_TOKEN or NOTION_DATABASE_ID is not set; skipping.")
        return

    state_path = data_dir / f"{issue_id}-notion.json"
    if state_path.exists():
        print(f"Notion page already recorded in {state_path}; skipping duplicate publish.")
        return

    title = config["digest"]["title"]
    page_title = f"{title}: {issue_id}"
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            notion_config.get("title_property", "Name"): {
                "title": [{"text": {"content": page_title}}],
            }
        },
        "children": notion_blocks_for_digest(config, today, start_date, papers, general_summary),
    }

    try:
        response = notion_request("POST", "https://api.notion.com/v1/pages", payload, token)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Notion publish failed: {exc}", file=sys.stderr)
        return

    state_path.write_text(
        json.dumps(
            {
                "page_id": response.get("id", ""),
                "url": response.get("url", ""),
                "published_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Published digest to Notion: {response.get('url', response.get('id', 'unknown page'))}")


def notion_blocks_for_digest(
    config: dict[str, Any],
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
    general_summary: str,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        notion_heading("General summary"),
        notion_paragraph(f"Window: {start_date.isoformat()} to {today.isoformat()}. {general_summary}"),
        notion_heading("Papers"),
    ]
    for index, paper in enumerate(papers, start=1):
        doi_text = paper.doi or "not available from source metadata or Crossref lookup"
        blocks.extend(
            [
                notion_heading(f"{index}. {paper.title}", level=3),
                notion_paragraph(
                    f"{paper.venue or paper.source}; {paper.published or 'date unavailable'}. "
                    f"DOI: {doi_text}. Source: {paper.url}"
                ),
                notion_paragraph(make_study_summary(paper)),
            ]
        )
    if not papers:
        blocks.append(notion_paragraph("No matching papers or preprints were found in this window."))
    return blocks[:100]


def notion_heading(text: str, level: int = 2) -> dict[str, Any]:
    block_type = "heading_3" if level == 3 else "heading_2"
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": notion_rich_text(text)},
    }


def notion_paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": notion_rich_text(text)},
    }


def notion_rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[:2000]}}]


def notion_request(method: str, url: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise urllib.error.URLError(f"HTTP {exc.code}: {body}") from exc


def render_general_summary(
    config: dict[str, Any],
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
) -> str:
    title = config["digest"]["title"].lower()
    if not papers:
        return f"No matching papers or preprints were found for {start_date.isoformat()} through {today.isoformat()}."

    theme_counts = extract_theme_counts(papers)[:3]
    preprints = sum(1 for paper in papers if paper.is_preprint)
    doi_count = sum(1 for paper in papers if paper.doi)
    record_word = "record" if len(papers) == 1 else "records"
    doi_verb = "includes" if len(papers) == 1 else "include"
    theme_text = natural_join([f"{theme.lower()} ({count})" for theme, count in theme_counts])
    preprint_text = f"{preprints} preprint{'s' if preprints != 1 else ''}" if preprints else "no preprints"
    doi_text = f"{doi_count} of {len(papers)} {record_word} {doi_verb} a DOI after source metadata and Crossref lookup"
    return (
        f"This issue of {title} covers {len(papers)} selected {record_word} from the current window, with {preprint_text}. "
        f"The strongest themes are {theme_text}. {doi_text}. "
        "The digest emphasizes human medicine and female reproductive health, while filtering plant, agriculture, food, animal, and environmental metagenomics."
    )


def extract_theme_counts(papers: list[Paper]) -> list[tuple[str, int]]:
    themes = {
        "Vaginal and cervicovaginal microbiome": [
            "vaginal",
            "vagina",
            "cervicovaginal",
            "vulvovaginal",
            "urogenital",
        ],
        "Pregnancy, maternal health, and preterm birth": [
            "pregnancy",
            "pregnant",
            "maternal",
            "placenta",
            "placental",
            "preterm",
            "miscarriage",
        ],
        "Gynecologic disease, cancer, and fertility": [
            "gynecologic",
            "gynaecologic",
            "cervical",
            "endometrial",
            "fertility",
            "infertility",
            "endometriosis",
            "PCOS",
            "HPV",
            "cancer",
        ],
        "Infection, STI, and antimicrobial resistance": [
            "pathogen",
            "antimicrobial resistance",
            "antibiotic resistance",
            "resistome",
            "infection",
            "bacterial vaginosis",
            "sexually transmitted",
        ],
        "Clinical metagenomic tools, workflows, and benchmarks": [
            "benchmark",
            "pipeline",
            "workflow",
            "software",
            "tool",
            "database",
        ],
        "Assembly, binning, and strain resolution": [
            "assembly",
            "binning",
            "metagenome-assembled genome",
            "strain",
            "MAG",
        ],
        "Taxonomic and functional profiling": [
            "taxonomic profiling",
            "functional profiling",
            "taxonomy",
            "function",
            "metabolic",
        ],
    }
    counts: dict[str, int] = {theme: 0 for theme in themes}
    for paper in papers:
        haystack = paper_haystack(paper)
        for theme, terms in themes.items():
            if any(has_term(haystack, term) for term in terms):
                counts[theme] += 1
    ranked = sorted(((theme, count) for theme, count in counts.items() if count), key=lambda item: item[1], reverse=True)
    return ranked or [("General metagenomics", len(papers))]


def make_takeaway(paper: Paper) -> str:
    haystack = paper_haystack(paper)
    if any(has_term(haystack, term) for term in ["vaginal", "cervicovaginal", "vulvovaginal", "urogenital"]):
        return "This is directly relevant to female reproductive tract microbiome research and clinical interpretation."
    if any(has_term(haystack, term) for term in ["pregnancy", "pregnant", "maternal", "placenta", "preterm"]):
        return "This connects metagenomics to pregnancy, maternal health, or birth-outcome questions."
    if any(has_term(haystack, term) for term in ["fertility", "infertility", "endometriosis", "PCOS", "HPV", "cervical", "endometrial"]):
        return "This links microbiome or metagenomic signals to gynecologic disease, fertility, or cancer-relevant contexts."
    if any(has_term(haystack, term) for term in ["benchmark", "benchmarking", "comparison"]):
        return "This looks useful for comparing methods, databases, or analytical choices in human clinical metagenomic workflows."
    if any(has_term(haystack, term) for term in ["pipeline", "workflow", "software", "tool"]):
        return "This is relevant to the practical processing layer that shapes reproducibility and interpretation in human-health metagenomics."
    if any(has_term(haystack, term) for term in ["patient", "clinical", "disease", "infection", "cancer"]):
        return "This connects metagenomic sequencing to human health questions where study design and interpretation matter."
    if any(has_term(haystack, term) for term in ["assembly", "binning", "strain"]):
        return "This is relevant to genome reconstruction or fine-resolution profiling from human-associated metagenomes."
    return "This paper matched the configured human medicine and female reproductive health metagenomics themes."


def make_study_summary(paper: Paper) -> str:
    if not paper.abstract:
        return (
            "No abstract was available from the metadata source. Based on the title and source metadata, "
            + make_takeaway(paper)
        )
    sentences = split_sentences(paper.abstract)
    if not sentences:
        return paper.abstract
    selected = sentences[:3]
    summary = " ".join(selected)
    return summary if summary.endswith((".", "!", "?")) else summary + "."


def format_doi(doi: str) -> str:
    if not doi:
        return "Not available from source metadata or Crossref lookup"
    return f"[{doi}](https://doi.org/{doi})"


def markdown_to_narration(markdown: str) -> str:
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
    text = re.sub(r"<details>.*?</details>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?details>|</?summary>", "", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`>]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, limit: int) -> list[str]:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > limit:
            sentences = split_sentences(paragraph)
            for sentence in sentences:
                if len(current) + len(sentence) + 1 > limit and current:
                    chunks.append(current.strip())
                    current = ""
                current += (" " if current else "") + sentence
            continue
        if len(current) + len(paragraph) + 2 > limit and current:
            chunks.append(current.strip())
            current = paragraph
        else:
            current += ("\n\n" if current else "") + paragraph
    if current.strip():
        chunks.append(current.strip())
    return chunks


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def clean_markup(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_text(value: str) -> str:
    value = html.unescape(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def paper_haystack(paper: Paper) -> str:
    return normalize_text(" ".join([paper.title, paper.abstract, paper.venue] + paper.categories))


def has_term(normalized_haystack: str, term: str) -> bool:
    normalized_term = normalize_text(term)
    if not normalized_term:
        return False
    return f" {normalized_term} " in f" {normalized_haystack} "


def normalize_date_string(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if "T" in value:
        value = value.split("T", 1)[0]
    return value


def split_authors(value: str) -> list[str]:
    if not value:
        return []
    return [author.strip() for author in re.split(r",|;|\sand\s", value) if author.strip()]


def format_authors(authors: list[str]) -> str:
    if not authors:
        return "Authors unavailable"
    if len(authors) <= 4:
        return ", ".join(authors)
    return ", ".join(authors[:4]) + " et al."


def natural_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return items[0] + " and " + items[1]
    return ", ".join(items[:-1]) + ", and " + items[-1]


def first_nonempty(*values: Any) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def date_from_filename(name: str) -> dt.date | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", name)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def multiline_list(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
