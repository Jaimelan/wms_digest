#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import dataclasses
import datetime as dt
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
import wave
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
    parser = configparser.ConfigParser()
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
        "openai": {
            "summary_model": parser.get("openai", "summary_model"),
            "tts_model": parser.get("openai", "tts_model"),
            "voice": parser.get("openai", "voice"),
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
    europe_pmc = fetch_europe_pmc(config["queries"]["europe_pmc"], start_date, end_date)
    arxiv = fetch_arxiv(config["queries"]["arxiv"], start_date, end_date)
    if europe_pmc is None and arxiv is None:
        raise RuntimeError("All literature sources failed; refusing to write an empty digest.")

    papers: list[Paper] = []
    papers.extend(europe_pmc or [])
    papers.extend(arxiv or [])
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
        text = fetch_text("https://export.arxiv.org/api/query", params)
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
    digest_md = render_digest(config, today, start_date, papers)
    script_md = render_or_generate_podcast_script(config, today, start_date, papers, digest_md)

    (digest_dir / f"{issue_id}.md").write_text(digest_md, encoding="utf-8")
    (podcast_dir / f"{issue_id}-script.md").write_text(script_md, encoding="utf-8")
    (data_dir / f"{issue_id}.json").write_text(
        json.dumps([dataclasses.asdict(paper) for paper in papers], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    maybe_generate_audio(config, podcast_dir, issue_id, script_md)


def render_digest(config: dict[str, Any], today: dt.date, start_date: dt.date, papers: list[Paper]) -> str:
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
        "This digest is generated from Europe PMC and arXiv metadata. Audio narration, when present, is AI-generated.",
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
                f"- **Topic signals**: {reasons}; relevance score {paper.score}",
                f"- **Link**: {paper.url}",
            ]
        )
        if paper.doi:
            lines.append(f"- **DOI**: {paper.doi}")
        lines.append("")
        lines.append(shorten_abstract(paper.abstract, max_words=110))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_or_generate_podcast_script(
    config: dict[str, Any],
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
    digest_md: str,
) -> str:
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key and papers:
        generated = generate_script_with_openai(config, today, start_date, papers)
        if generated:
            return generated
    return render_deterministic_script(config, today, start_date, papers, digest_md)


def generate_script_with_openai(
    config: dict[str, Any], today: dt.date, start_date: dt.date, papers: list[Paper]
) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        print("OpenAI package is not installed; writing deterministic podcast script.", file=sys.stderr)
        return ""

    client = OpenAI()
    model = first_nonempty(os.environ.get("OPENAI_SUMMARY_MODEL"), config["openai"]["summary_model"])
    target_minutes = int(config["digest"]["podcast_target_minutes"])
    target_words = max(1200, target_minutes * 145)
    payload = {
        "date": today.isoformat(),
        "window": {"start": start_date.isoformat(), "end": today.isoformat()},
        "target_minutes": target_minutes,
        "papers": [paper_for_prompt(paper) for paper in papers],
    }
    instructions = (
        "Write a polished solo-host science podcast script for a technically literate biomedical audience. "
        "Focus on whole metagenome sequencing and microbiome/metagenomics in medicine, human health, "
        "and especially female reproductive health. Keep plant, agriculture, food, animal, and environmental "
        "metagenomics out of the framing unless a paper is explicitly about human clinical relevance. "
        "Synthesize papers into themes instead of reading the list mechanically. Be accurate to the provided "
        "metadata, do not invent results beyond titles and abstracts, and explicitly say when a paper is a preprint. "
        "Include a brief AI-generated audio disclosure. Use plain Markdown. "
        f"Target about {target_words} words, suitable for roughly {target_minutes} minutes of narration."
    )
    try:
        response = client.responses.create(
            model=model,
            instructions=instructions,
            input=json.dumps(payload, ensure_ascii=False),
            max_output_tokens=7000,
        )
    except Exception as exc:  # noqa: BLE001 - keep scheduled job resilient.
        print(f"OpenAI script generation failed: {exc}", file=sys.stderr)
        return ""

    text = getattr(response, "output_text", "") or ""
    if not text.strip():
        print("OpenAI script generation returned no text; writing deterministic script.", file=sys.stderr)
        return ""
    return text.rstrip() + "\n"


def render_deterministic_script(
    config: dict[str, Any],
    today: dt.date,
    start_date: dt.date,
    papers: list[Paper],
    digest_md: str,
) -> str:
    title = config["digest"]["title"]
    if not papers:
        return textwrap.dedent(
            f"""\
            # Podcast Script: {today.isoformat()}

            This is an AI-generated audio disclosure: if this script is converted to speech, the voice is synthetic.

            No matching papers or preprints were found for {start_date.isoformat()} through {today.isoformat()}.
            """
        )

    theme_counts = extract_theme_counts(papers)
    lines = [
        f"# Podcast Script: {title} - {today.isoformat()}",
        "",
        "This is an AI-generated audio disclosure: if this script is converted to speech, the voice is synthetic.",
        "",
        f"Welcome to this week in {title.lower()}, covering {start_date.isoformat()} through {today.isoformat()}.",
        f"This episode covers {len(papers)} papers and preprints selected from Europe PMC and arXiv.",
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
                make_takeaway(paper),
                shorten_abstract(paper.abstract, max_words=145),
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
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set; skipping audio generation.")
        return
    try:
        from openai import OpenAI
    except ImportError:
        print("OpenAI package is not installed; skipping audio generation.", file=sys.stderr)
        return

    client = OpenAI()
    tts_model = first_nonempty(os.environ.get("OPENAI_TTS_MODEL"), config["openai"]["tts_model"])
    voice = first_nonempty(os.environ.get("OPENAI_TTS_VOICE"), config["openai"]["voice"])
    narration_text = markdown_to_narration(script_md)
    chunks = chunk_text(narration_text, limit=3200)
    if not chunks:
        print("Podcast script is empty; skipping audio generation.", file=sys.stderr)
        return

    wav_parts: list[Path] = []
    for index, chunk in enumerate(chunks, start=1):
        part_path = podcast_dir / f"{issue_id}-part{index:02d}.wav"
        try:
            with client.audio.speech.with_streaming_response.create(
                model=tts_model,
                voice=voice,
                input=chunk,
                instructions=(
                    "Narrate as a calm, precise science podcast host. Use clear pacing, "
                    "natural transitions, and avoid exaggerated enthusiasm."
                ),
                response_format="wav",
            ) as response:
                response.stream_to_file(part_path)
        except Exception as exc:  # noqa: BLE001 - keep scheduled job resilient.
            print(f"OpenAI audio generation failed: {exc}", file=sys.stderr)
            for existing in wav_parts:
                existing.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)
            return
        wav_parts.append(part_path)

    combined_wav = podcast_dir / f"{issue_id}.wav"
    combine_wav_files(wav_parts, combined_wav)
    for part in wav_parts:
        part.unlink(missing_ok=True)

    mp3_path = podcast_dir / f"{issue_id}.mp3"
    if convert_wav_to_mp3(combined_wav, mp3_path):
        combined_wav.unlink(missing_ok=True)
        print(f"Wrote podcast audio to {mp3_path}")
    else:
        print(f"Wrote podcast audio to {combined_wav}; install ffmpeg to convert MP3.")


def combine_wav_files(parts: list[Path], destination: Path) -> None:
    if len(parts) == 1:
        shutil.move(str(parts[0]), destination)
        return

    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
        frames = [first.readframes(first.getnframes())]
    for part in parts[1:]:
        with wave.open(str(part), "rb") as current:
            if current.getparams()[:3] != params[:3]:
                raise ValueError("WAV chunk parameters differ; cannot concatenate safely.")
            frames.append(current.readframes(current.getnframes()))
    with wave.open(str(destination), "wb") as output:
        output.setparams(params)
        for frame_blob in frames:
            output.writeframes(frame_blob)


def convert_wav_to_mp3(wav_path: Path, mp3_path: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "96k",
        str(mp3_path),
    ]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ffmpeg MP3 conversion failed: {exc}", file=sys.stderr)
        return False
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


def paper_for_prompt(paper: Paper) -> dict[str, Any]:
    return {
        "title": paper.title,
        "authors": paper.authors[:8],
        "published": paper.published,
        "venue": paper.venue,
        "source": paper.source,
        "url": paper.url,
        "doi": paper.doi,
        "is_preprint": paper.is_preprint,
        "abstract": shorten_abstract(paper.abstract, max_words=190),
        "score_reasons": paper.score_reasons,
    }


def markdown_to_narration(markdown: str) -> str:
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
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


def shorten_abstract(abstract: str, max_words: int) -> str:
    if not abstract:
        return "No abstract was available from the metadata source."
    words = abstract.split()
    if len(words) <= max_words:
        return abstract
    return " ".join(words[:max_words]).rstrip(".,;:") + "..."


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
