# Environment of the last run

- Date: 2026-10-05T18:05:41
- OS: macOS-15.3.1-arm64-arm-64bit
- Python: 3.12.9
- uv: uv 0.9.26 (ee4f00362 2026-01-15)
- homr: 0.7.0
- pdftoppm: pdftoppm version 26.09.0
- MuseScore CLI: MuseScore4 4.7.5

## Commands to repeat the run

```bash
uv sync
uv run python scripts/run_batch.py --timeout 900
# per page, the script runs:  pdftoppm -r 300 -png <pdf> work/<name>/p   (PDFs only)
#                             homr <page image>
#                             postprocess.py fixes -> outputs/<name>.musicxml
```
