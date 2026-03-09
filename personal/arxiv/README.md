# ArXiv cs.CV Daily Digest

A personal arxiv reading tool deployed on GitHub Pages. Every weekday it automatically fetches all cs.CV papers, classifies them by interest, and serves a filterable reading interface with per-paper Gemini analysis on demand.

---

## How it works

```
GitHub Actions (Mon–Fri 09:00 UTC)
  └─ scripts/fetch_arxiv.py
       ├─ Scrapes https://arxiv.org/list/cs.CV/recent (all pages)
       ├─ Batch-fetches abstracts via arxiv XML API
       ├─ Classifies each paper → interested / neutral / not_for_me
       ├─ Bakes committed user_annotations.json into output
       └─ Writes personal/arxiv/YYYY-MM-DD.json + latest.json

personal/arxiv.html  (static frontend, served by GitHub Pages)
  ├─ Loads latest.json (or a past date's JSON)
  ├─ Reads localStorage for your annotation overrides
  ├─ Syncs with committed user_annotations.json on load
  └─ Calls Gemini API directly from browser (on demand)
```

---

## Classification

### Title blacklist (`TITLE_BLACKLIST` in `fetch_arxiv.py`)
Papers whose **title** contains any blacklisted keyword are immediately labelled **not_for_me**, before any other rule runs. Edit this list freely — it's at the very top of the script.

Current blacklisted topics: 3D Gaussian / NeRF / point cloud / depth estimation / mesh / medical / clinical / autonomous driving / LiDAR / satellite / remote sensing / weather / document / scene text.

### Keyword rules
| Label | Categories |
|-------|-----------|
| **interested** | image/video generation, multimodal LLM, human generation |
| **not_for_me** | medical, 3D, downstream tasks, pure visual understanding |
| **neutral** | everything else |

Rules check title + abstract. An `interested` match with a `medical` tag flips to **not_for_me**.

---

## Frontend features

| Feature | How |
|---------|-----|
| Filter tabs | All / Interested / Neutral / Not for me / Saved |
| Live counts | Badge numbers update immediately on every button click |
| Manual override | neutral → Interested · interested → Not for me · Undo |
| Save | Independent bookmark; works on any paper regardless of label |
| Abstract toggle | Expand/collapse inline |
| Gemini analysis | Per-paper on-demand button; result cached in localStorage |
| Saved sidebar | Fixed panel listing all saved papers grouped by date |
| Date picker | Load any past date's JSON from the header |
| Export annotations | Downloads `user_annotations.json` for cross-device sync |

---

## Persistence

Annotations live in two places:

1. **`localStorage`** — instant, device-local. Keys: `arxiv_overrides`, `arxiv_saved`, `arxiv_analyses`, `arxiv_gemini_key`.
2. **`personal/arxiv/user_annotations.json`** — committed to the repo for cross-device sync and as a backup for the Python script.

### Cross-device sync workflow
```bash
# After reading on one machine, export annotations from the sidebar
# then commit the downloaded file:
mv ~/Downloads/user_annotations.json personal/arxiv/user_annotations.json
git add personal/arxiv/user_annotations.json
git commit -m "Update arxiv annotations"
git push
```
On load, the page fetches this file and merges it into localStorage (local entries always win). The Python script also reads it and bakes `user_override` fields into new paper JSONs.

---

## Backfilling past dates

```bash
python3 scripts/fetch_arxiv.py --date 2026-03-03
```

Uses the monthly listing (`https://arxiv.org/list/cs.CV/YYMM`), scans pages until it finds the target date's section, and saves to `personal/arxiv/2026-03-03.json`. The frontend date picker will pick it up automatically.

> **Note:** arxiv does not publish on weekends or holidays.

---

## Running locally

```bash
# From repo root:
python3 -m http.server 8765
# Open: http://localhost:8765/personal/arxiv.html
```

`file://` won't work because the page uses `fetch()` to load JSON files.

---

## Gemini analysis

Click **✦ Analyze** on any paper card. On first use you'll be prompted for a Gemini API key (stored in localStorage). The analysis follows the structured reading framework in [`paper_instruction.txt`](paper_instruction.txt) and renders with LaTeX (KaTeX) and Markdown support.

---

## Files

```
scripts/fetch_arxiv.py          # Scraper + classifier (run by GitHub Actions)
.github/workflows/fetch_arxiv.yml
personal/arxiv.html             # Frontend SPA
personal/arxiv/
  YYYY-MM-DD.json               # Daily paper data (generated)
  latest.json                   # Symlink to most recent day (generated)
  user_annotations.json         # Your saved overrides (commit this manually)
  paper_instruction.txt         # Gemini analysis prompt template
  README.md                     # This file
```
