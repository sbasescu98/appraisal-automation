# Quick Start Guide

From a planning application reference (e.g. `18/30161/CONDET`) to one combined text file of everything in it.

```
Application reference  →  1. find key  →  2. download  →  3. extract  →  all_documents.txt
   18/30161/CONDET          P7SRNYBJ0GJ00     PDFs           text + OCR
                                    saved as eastriding_18_30161_condet
```

---

## 0. One-time setup

```bash
cd "Appraisal Automation"

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# OCR tools for scanned documents (macOS)
brew install tesseract poppler
```

After this, every new terminal session only needs:

```bash
cd "Appraisal Automation"
source venv/bin/activate
```

---

## 1. Find the application key

The portal identifies applications by an internal **key** (e.g. `P7SRNYBJ0GJ00`), not the reference number.

1. Open https://newplanningaccess.eastriding.gov.uk/newplanningaccess/search.do?action=simple&searchType=Application
2. Search for the reference, e.g. `18/30161/CONDET`
3. Click the matching application
4. Copy the value after `keyVal=` in the browser's address bar:

```
...applicationDetails.do?activeTab=summary&keyVal=P7SRNYBJ0GJ00
                                                  ^^^^^^^^^^^^^
```

---

## 2. Download the documents (~1–2 min)

```bash
python cli.py -k P7SRNYBJ0GJ00
```

- Downloads every document **except "Plans" and "Amended Plans"** (drawings) into `data/documents/eastriding_18_30161_condet/` (named after the council and the application reference)
- Saves the portal's document table to `data/metadata/eastriding_18_30161_condet_metadata.json`
- Prints that key at the end; use it for the next steps

---

## 3. Extract the text (~1–10 min, depending on scanned pages)

```bash
python extractor_cli.py extract eastriding_18_30161_condet
```

- Reads the text from every downloaded document; scanned pages are OCR'd
- Writes one JSON per document, plus the combined files below

---

## 4. Read the results

```
data/extracted/eastriding_18_30161_condet/
├── all_documents.txt        ← everything in one readable file (start here)
├── all_documents.json       ← same, structured, for later processing
├── extraction_summary.json  ← per-document status, token counts, errors
└── raw_texts/               ← one JSON per document, with text split into chunks
```

Open `all_documents.txt` in VS Code. Each document starts with a header (type, description, date published, source link) and its text is marked with `[Page N]`.

---

## The whole thing, copy-paste

```bash
cd "Appraisal Automation"
source venv/bin/activate

python cli.py -k P7SRNYBJ0GJ00                          # download (portal key from step 1)
KEY=eastriding_18_30161_condet                          # the key it prints at the end
python extractor_cli.py extract $KEY                    # extract + compile
open data/extracted/$KEY/all_documents.txt
```

---

## Other useful commands

| Goal | Command |
|------|---------|
| Test with just 2 documents | `python cli.py -k PORTAL_KEY --max-downloads 2` |
| List documents without downloading | `python cli.py -k PORTAL_KEY --fetch-only` then `python cli.py list-documents KEY` |
| See which PDFs are scanned before extracting | `python extractor_cli.py analyze KEY` |
| Rebuild the combined files without re-running OCR | `python extractor_cli.py compile KEY` |
| Inspect the portal page if scraping breaks | `python debug_scraper.py PORTAL_KEY` |
| Get the comments and documents of a Wiltshire application | `python cli.py --url "<application page URL>"` then `python extractor_cli.py extract <key it prints>` (e.g. `wiltshire_pl_2025_05830`) |
| Find web pages about an application (needs `SERPAPI_API_KEY` in `.env`) | `python search_cli.py -a KEY "your google search" -p "Project Name"`: saved under `KEY_search` |
| Preview those searches without using credits | add `--dry-run` |
| List the web results with their URLs | `python search_cli.py list-results KEY_search` |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `command not found: python` / `ModuleNotFoundError` | Run `source venv/bin/activate` first |
| "No documents found in table" | Check the portal key; run `python debug_scraper.py PORTAL_KEY` |
| Extraction error mentions tesseract/poppler | Run `brew install tesseract poppler` |
| "No documents folder" when extracting | Run step 2 (download) first |

See [README.md](README.md) for details on configuration, output formats and how each step works.
