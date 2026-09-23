# simple-yamba-music-pipeline

First iteration of our music OMR experiment: run [HOMR](https://github.com/liebharc/homr)
locally on about 10 of our scores and find out how usable its MusicXML is. It is deliberately
minimal: one script, no editor, no training, no orchestration.

```
inputs/    score images (png/jpg; homr only reads these) or PDFs: put yours here
outputs/   <name>.musicxml, or <name>-pNN.musicxml for each PDF page
logs/      HOMR output per page
work/      page images as fed to HOMR, plus HOMR's *_teaser.png (the staves it detected)
review/    run-results.csv, environment.md, review.md (notes and findings), renders/ (MuseScore PDFs)
exports/   scores saved from MuseScore after review
scripts/   run_batch.py, ui.py
```

Inputs, outputs, logs and review files are committed on purpose: they are the results of the
experiment.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and poppler (`brew install poppler`, for PDF → PNG).

```bash
uv sync                      # installs homr[cpu]==0.7.0 into .venv
```

The first run downloads HOMR's models, which takes a while. Everything runs locally.

## Run

```bash
uv run python scripts/run_batch.py            # optional: --timeout 900 (seconds per page)
```

For each input, the script converts PDFs to 300-dpi PNG pages, runs `homr <image>` on each page,
and moves the result to `outputs/`. It then writes `review/run-results.csv` and
`review/environment.md` (tool versions and commands) and adds a row per new sample to
`review/review.md`. Failures are recorded as they are, without retries.

### Or use the local UI

```bash
uv run python scripts/ui.py                   # then open http://127.0.0.1:8765
```

A single page where you drop images/PDFs into `inputs/` (or remove them), run the same batch
script, and see the results with links to each MusicXML, MuseScore render and log. It only
listens on 127.0.0.1.

## Review in MuseScore Studio

1. Install MuseScore Studio: `brew install --cask musescore` or <https://musescore.org>.
   If it is installed, the script also exports each result to `review/renders/<name>.pdf`
   and records whether MuseScore could open it. If it can't, the reason MuseScore gives is
   recorded too (e.g. `Incomplete measure: … measure 7, staff 1`).
2. In MuseScore, go to **File → Open…** and pick `outputs/<name>.musicxml`. Put the original from
   `inputs/` next to it. If MuseScore says the score is corrupted, choose **Open anyway**.
   The command line refuses these files; the app can sometimes still show them, but a badly
   recognized score may fail to load even then.
3. Look for obvious errors in notes, rhythms, voices, measures and layout, then fill in that
   sample's row in `review/review.md`.
4. When all samples are done, write the findings summary and the next experiment at the bottom
   of `review/review.md`.

## License

The code in this repository (`scripts/`, config and docs) is released under the [MIT License](LICENSE).

[HOMR](https://github.com/liebharc/homr) is not included here. It is a separate dependency that
`uv sync` installs from PyPI, licensed under AGPL-3.0, and the scripts only call it as a
command-line tool.
