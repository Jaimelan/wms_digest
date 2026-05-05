# Science Digest

This repository generates a rolling literature digest for whole metagenome sequencing and microbiome/metagenomics in medicine and human health, with a focus on female reproductive health.

The default workflow runs weekly, keeps roughly the last month of generated files, and publishes:

- Markdown digest pages in `docs/digests/`
- Podcast scripts in `docs/podcasts/`
- Free Edge TTS audio files in `docs/podcasts/`
- Machine-readable paper metadata in `docs/data/`
- Optional Notion digest pages when Notion secrets are configured
- A rolling index at `docs/index.md`

## GitHub Setup

1. Push this repository to GitHub.
2. Enable GitHub Actions.
3. Optional: enable GitHub Pages from the `docs/` directory to make the digest browsable.
4. Optional: add `NOTION_TOKEN` and `NOTION_DATABASE_ID` repository secrets to publish a digest page to Notion.

The workflow has `workflow_dispatch`, so you can run it manually from the Actions tab. It also runs every Monday morning by default.

## Local Run

Requires Python 3.10 or newer.

```bash
python3 -m pip install -r requirements.txt
python3 scripts/run_digest.py
```

Audio generation uses `edge-tts`, which calls Microsoft Edge's web speech service and does not require an OpenAI key.
Install `ffmpeg` locally if the script is long enough to be split into multiple audio chunks.

## Configuration

Edit `config/science_digest.ini`.

Important settings:

- `lookback_days`: how many days of new papers to include per run. Default is `7`.
- `retention_days`: how long generated outputs remain in the current tree. Default is `31`.
- `max_papers`: maximum papers included in a digest.
- `enable_arxiv`: optional arXiv source. It is disabled by default because GitHub-hosted runners are often rate-limited by arXiv and Europe PMC is the better primary source for this clinical topic.
- `enable_crossref_lookup`: tries to resolve missing DOIs through Crossref by title.
- `audio.enabled`: turns free Edge TTS audio generation on or off.
- `notion.enabled`: turns optional Notion publishing on or off. The workflow skips Notion unless `NOTION_TOKEN` and `NOTION_DATABASE_ID` are present.
- `podcast_target_minutes`: approximate target duration for the script.
- `schedule`: the GitHub cron is in `.github/workflows/science-digest.yml`.

To make this daily instead of weekly, change the cron schedule and set `lookback_days = 1`.

## Sources

The runner queries Europe PMC for life-sciences papers and preprints. arXiv can be enabled for computational preprints, but is off by default to avoid frequent rate limits on GitHub Actions. The runner deduplicates by DOI, title, and source ID, removes configured off-topic areas such as plant, agriculture, food, animal, and environmental metagenomics, ranks papers with female reproductive health and clinical keywords, and writes a concise digest plus a longer podcast script.

Each digest includes a general summary, a study summary for every selected record, and a DOI line for every article. DOI values come from source metadata first and Crossref title lookup second; the runner does not invent missing DOIs.

## Notion

To publish each digest to Notion:

1. Create a Notion integration and copy its token.
2. Share your target Notion database with that integration.
3. Add GitHub repository secrets:
   - `NOTION_TOKEN`
   - `NOTION_DATABASE_ID`

The database needs a title property. The default property name is `Name`; change `title_property` in `config/science_digest.ini` if your database uses a different title property.
