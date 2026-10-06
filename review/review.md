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
| merkurius-m01-10 | ok | yes | time signatures 10/10 and rhythm 18/20 staff-measures after postprocess; 92/93 checked pitches (m2: B6 read as Bb6); m4, m6 LH: 19 of 20 sixteenths (one note missing, padded with a rest); m6 LH: a spurious eighth rest | low–medium | phone photo, sharpest of the four (notehead ~19 px at HOMR's scale); RH is a treble 8va clef, added via inputs/postprocess.json |
| merkurius-m22-33 | ok | yes | rhythm 19/23; m22 misread on both staves (10 sixteenths + 2 eighths for 8 + 1, around the flats between chords), so its time signature comes out 7/8 instead of 5/8; m23 rhythms wrong on both staves; pitches not checked | medium | photo from further away (notehead ~12 px); same m22 misread on 16 px crops, so not a resolution problem |
| merkurius-m34-45 | ok | yes | time signatures 12/12, rhythm 20/23; m34 LH: 19 of 20 sixteenths; m36: LH sixteenths read into the RH; pitches not checked | medium | |
| merkurius-m46-51 | ok | yes | time signatures 6/6, rhythm 11/12; m47 RH: 19 of 20 sixteenths; pitches not checked | low | |

## Merkurius: HOMR + postprocess (iteration 2)

Four phone photos of one printed piano piece (m1–10, 22–33, 34–45, 46–51), scored against a
hand-checked transcription (`review/truth/merkurius.json`, see the README). Every measure has its
time signature and rhythm; pitches are checked on page 1 only (93 notes), where they could be
verified reliably against the photo.

| | HOMR alone | + postprocess.py |
|---|---|---|
| measures found | 40/40 | 40/40 |
| time signatures | 14/40 | 39/40 |
| rhythm (staff-measures) | 64/78 | 68/78 |
| pitches checked (page 1) | 92/93 | 92/93 |
| overfull measures | 24 | 0 |
| treble 8va clef | missing | yes |

- HOMR's model only reads the denominator of a time signature; HOMR then uses one numerator, the
  median measure length, for the whole page. This piece changes meter almost every bar, so most
  of it was wrong. postprocess.py gives each measure the time signature its content fills. On
  skanna0523 (no truth file) that also turned 7/8 everywhere into the printed 7/8, 8/8, 7/8, ….
- HOMR times both staves with one cursor in reading order, so a hand with long rests against
  sixteenths in the other hand gets overlapping onsets (m6, m34, m47). postprocess.py lays such a
  staff out again (3 staff-measures fixed) and fills short staves with rests (1 more, m4).
- What is left are recognition errors: one sixteenth missing from 20-note runs (m4, m6, m34,
  m47), m22–23 garbled, LH notes read into the RH (m36).
- Tried and dropped (worse or no change): flattening the lighting (rhythm 68 → 42 of 78) and
  cropping single systems/measures to enlarge the notes (same misreads; one crop lost a staff).

## Findings summary

_To write after review: overall success rate, which kinds of scores work or fail, the error types that come up most._

## Smallest useful next experiment

_To write after review._
