# simple-yamba-music-pipeline

First iteration of our music OMR experiment: run [HOMR](https://github.com/liebharc/homr)
locally on about 10 of our scores and find out how usable its MusicXML is. It is deliberately
minimal: one script, no editor, no training, no orchestration.

```
inputs/    score images (png/jpg; homr only reads these) or PDFs: put yours here; also MusicXML
           files you already have (.musicxml/.mxl), which are imported as they are, without HOMR
outputs/   <name>.musicxml, or <name>-pNN.musicxml for each PDF page (HOMR + postprocess.py)
logs/      HOMR output per page
work/      page images as fed to HOMR, HOMR's own <name>.homr.musicxml and *_teaser.png (the staves it detected)
review/    run-results.csv, environment.md, review.md (notes and findings), renders/ (MuseScore PDFs),
           truth/ (hand-checked transcriptions for score_check.py)
exports/   scores saved from MuseScore after review; PDF/MIDI exports from the UI or export.py
scores/    full scores concatenated from results in the UI (<name>.musicxml + MuseScore PDF)
scripts/   run_batch.py, postprocess.py, score_check.py, combine.py, samples.py, export.py, ui.py
tests/     uv run pytest
```

Inputs, outputs, logs and review files are committed on purpose: they are the results of the
experiment.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and poppler (`brew install poppler`, for PDF → PNG).

```bash
uv sync                      # installs homr[cpu]==0.7.0 (and pytest) into .venv
```

The first run downloads HOMR's models, which takes a while. Everything runs locally.

## Run

```bash
uv run python scripts/run_batch.py            # optional: --timeout 900 (seconds per page)
```

For each input, the script converts PDFs to 300-dpi PNG pages, runs `homr <image>` on each page,
keeps HOMR's file as `work/<name>/<name>.homr.musicxml` and writes a fixed copy to `outputs/`
(see [Post-processing](#post-processing)). It then writes `review/run-results.csv` and
`review/environment.md` (tool versions and commands) and adds a row per new sample to
`review/review.md`. Failures are recorded as they are, without retries.

A `.musicxml` or compressed `.mxl` in `inputs/` (say a section you transcribed or fixed in
MuseScore) skips HOMR: it is copied uncompressed to `outputs/<name>.musicxml` and rendered, and
its row has status `imported`. It can then be concatenated with the HOMR results like any other.

### Or use the local UI

```bash
uv run python scripts/ui.py                   # then open http://127.0.0.1:8765
```

A single page where you drop images/PDFs into `inputs/` (or remove them), run the same batch
script, and see the results with links to each MusicXML, MuseScore render and log. It only
listens on 127.0.0.1. Dropped `.musicxml`/`.mxl` files are imported at once and show up in the
results without a batch run. In the results you can also:

- **Reorder** them: drag a row by ⠿, or use ↑ ↓. The order is kept in `review/full-score.json`.
- **Concatenate** the ticked results, in that order, into a full score:
  `scores/<name>.musicxml` plus its MuseScore PDF. Measures are numbered on from 1, each result
  starts a new system, and clefs or time signatures that only repeat what is in force are left
  out at the joins, as is a final barline before a join. The same from the command line:
  `uv run python scripts/combine.py <title> scores/<title>.musicxml outputs/a.musicxml outputs/b.musicxml`
- **Rename** a sample or a full score by clicking its name. A sample is renamed everywhere it
  is named (input, `work/`, outputs, log, render, its rows in `run-results.csv` and `review.md`,
  its pages in `review/truth/`, its options in `inputs/postprocess.json`), so the next run keeps
  the new name. A full score's title changes with its name.
- **Export** a result or a full score to PDF or MIDI with its PDF / MIDI buttons: MuseScore
  writes `exports/<name>.pdf` or `.mid` and the browser downloads it. The same from the command
  line: `uv run python scripts/export.py pdf|midi <file.musicxml> ...`

## Post-processing

`scripts/postprocess.py` fixes what HOMR gets systematically wrong, measure by measure. The batch
runs it on every page and lists what it changed in the `fixes` column of `review/run-results.csv`.

- **Time signatures.** HOMR's model only reads the denominator, and HOMR uses one numerator (the
  median measure length) for the whole page, which breaks every score that changes meter. Each
  measure gets the time signature its content fills.
- **Staff timing.** HOMR times both staves with one cursor in reading order, so a staff whose
  rhythm doesn't line up with the other one (long rests against sixteenths) gets overlapping
  onsets. Such a staff is laid out again, unless that would overflow the measure (real polyphony).
- **Short staves** are filled with rests, so MuseScore doesn't reject the file as incomplete.
- **Octave clefs**, which HOMR can't see, are set per input in `inputs/postprocess.json`, keyed
  by a glob on the output name: `{"merkurius-*": {"treble_8va": 1}}` marks staff 1's treble clef 8va.

It can also be run by hand: `uv run python scripts/postprocess.py <in.musicxml> -o <out.musicxml> [--treble-8va 1]`.

## Checking against a ground truth

`scripts/score_check.py` compares a MusicXML file with a hand-checked transcription, measure by
measure: time signature, rhythm per staff, and pitches where the transcription gives them.

```bash
uv run python scripts/score_check.py outputs/merkurius-m01-10.musicxml review/truth/merkurius.json merkurius-m01-10
uv run python scripts/score_check.py outputs/<name>.musicxml    # print it in the truth's token form
```

A truth file (see `review/truth/merkurius.json`) lists, per page, each measure's time signature
and one string per staff: `C4:8` is an eighth-note C4, `Bb5+D6:4.` a dotted-quarter chord, `r:2` a
half rest, `?:16` a sixteenth whose pitch wasn't checked, `R` an empty staff; `null` skips a staff.
Pitches are as printed. `tests/test_merkurius.py` uses it to check that post-processing keeps
its scores on the Merkurius photos.

## Tests

```bash
uv run pytest
```

The unit tests build small MusicXML files by hand; `test_merkurius.py` runs the current
`postprocess.py` on HOMR's committed output in `work/`, so none of them run HOMR.

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
