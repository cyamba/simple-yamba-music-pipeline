# HOMR review — iteration 1

Compare each file in `outputs/` (opened in MuseScore Studio) with its source in `inputs/`.
`scripts/run_batch.py` adds a row per new sample; fill in the empty columns by hand.

- **converted**: status from the run (`ok`, `failed (exit N)`, `no-output`, `timeout`).
- **opens in MuseScore**: yes / no (auto-filled if the MuseScore CLI was found).
- **major errors**: obvious problems with notes, rhythms, voices, measures, layout.
- **correction effort**: low (minutes) / medium (under an hour) / high (faster to re-enter by hand).

## Per-sample results

| sample | converted | opens in MuseScore | major errors (notes / rhythms / voices / measures / layout) | correction effort | notes |
|---|---|---|---|---|---|
| källvatten | ok (with many warnings) | no: CLI refuses (voice too long, m. 13 and 15); app shows 'corrupted', Open anyway also failed | staves mostly missed or misplaced (1 system not found at all, others offset onto noteheads/ledger lines); many notes without pitch; overfull measures | high (faster to re-enter by hand) | handwritten on printed staff paper; scan 500x724 px, staff spacing ~9 px (low but probably not the main cause); dense ledger lines, 8va, smudges, handwritten text |
| källvatten | ok | no (exit 40: Voice too long: Full score, measure 13, staff 1, voice 2. Found: 2/1. Expected: 3/4.; Voice too long: Full score, measure 15, staff 1, voice 2. Found: 2/1. Expected: 3/2.) |  |  |  |
| källvatten-clean | ok (fewer warnings: 1 invalid symbol vs 13) | no: CLI refuses (measure 1 overfull on both staves) | staff detection still wrong on most systems (lines through noteheads, 'appassionato' system still missed); 125 notes read for a page with several hundred | high | same page, grayscale + autocontrast + 3x upscale; cleanup helps only slightly, so handwriting is the limit |
| skanna0523 | ok | yes |  |  |  |

## Findings summary

_To write after review: overall success rate, which kinds of scores work or fail, the error types that come up most._

## Smallest useful next experiment

_To write after review._
