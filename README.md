# Science Digest

This repository generates a rolling literature digest for whole metagenome sequencing in human health and metagenomic processing, tools, and benchmarks.

The default workflow runs weekly, keeps roughly the last month of generated files, and publishes:

- Markdown digest pages in `docs/digests/`
- Podcast scripts in `docs/podcasts/`
- Audio files in `docs/podcasts/` when `OPENAI_API_KEY` is configured
- Machine-readable paper metadata in `docs/data/`
- A rolling index at `docs/index.md`

## GitHub Setup

1. Push this repository to GitHub.
2. Add a repository secret named `OPENAI_API_KEY` if you want LLM-written scripts and generated audio.
3. Enable GitHub Actions.
4. Optional: enable GitHub Pages from the `docs/` directory to make the digest browsable.

The workflow has `workflow_dispatch`, so you can run it manually from the Actions tab. It also runs every Monday morning by default.

## Local Run

Requires Python 3.10 or newer.

```bash
python3 -m pip install -r requirements.txt
python3 scripts/run_digest.py
```

With OpenAI audio generation:

```bash
export OPENAI_API_KEY="..."
python3 scripts/run_digest.py
```

## Configuration

Edit `config/science_digest.ini`.

Important settings:

- `lookback_days`: how many days of new papers to include per run. Default is `7`.
- `retention_days`: how long generated outputs remain in the current tree. Default is `31`.
- `max_papers`: maximum papers included in a digest.
- `podcast_target_minutes`: approximate target duration for the script.
- `schedule`: the GitHub cron is in `.github/workflows/science-digest.yml`.

To make this daily instead of weekly, change the cron schedule and set `lookback_days = 1`.

## Sources

The runner queries Europe PMC for life-sciences papers and preprints, then arXiv for computational preprints that may not be indexed in Europe PMC yet. It deduplicates by DOI, title, and source ID, ranks papers with topic-aware keywords, and writes a concise digest plus a longer podcast script.

OpenAI is optional for the paper collection step. Without `OPENAI_API_KEY`, the repository still produces a digest and a deterministic podcast script, but no audio.
