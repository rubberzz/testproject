# Golden corpus

Put real supplier files in `files/`. They are gitignored on purpose — supplier
price lists are client data and should not end up in a repo.

Aim for one file per layout family, because each family is a different way for the
extractor to break:

| Layout family | Example |
|---|---|
| Standard table, cost + retail columns | large supplier price list (e.g. Lasher) |
| Pre-exploded variants, wide columns, image URLs | Shopify product export |
| Side-by-side blocks with category headers | venue menu / bar price list (e.g. Ti Amo) |
| Cost and margin columns that must **not** become the selling price | internal stock pricing sheet |
| Generic retail table | plain product list |
| Yoco export re-import | a catalogue exported from the Yoco app |

## Usage

```bash
python tests/run_goldens.py --update   # snapshot current behaviour
python tests/run_goldens.py            # compare; non-zero exit on drift
```

Set `GEMINI_API_KEY` to exercise the AI layout-planning path. Without it the run
still covers the deterministic parsers, the audit and the export shape.

`--update` after an intentional improvement, and read the diff before you accept
it. The point of the snapshot is that a prompt change which fixes one supplier and
breaks another cannot pass silently.

`expected.json` is also gitignored, since it only means anything next to the exact
files that produced it. Keep both alongside each other wherever you run this.
