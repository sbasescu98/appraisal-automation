# Planning Application Document Pipeline

Downloads the documents for an East Riding or Wiltshire planning application (plus the public comments for Wiltshire), and finds web coverage of a project through Google search. It turns both into text ready for LLM processing.

| Phase | What it does | Command |
|-------|--------------|---------|
| **1. Scrape** | Reads the application's document table on the planning portal and downloads the files. For Wiltshire (and other Arcus registers), also saves every comment | `python cli.py -k KEY` or `python cli.py --url URL` |
| **2a. Extract** | Extracts text from each file (OCR for scanned pages), splits it into chunks, and compiles everything into one file | `python extractor_cli.py extract KEY` |
| **1b. Web search** | Runs Google searches (via SerpAPI) exactly as you type them, downloads the result pages, and extracts their text into the same format | `python search_cli.py "your google search"` |
| 2b+. *Planned* | LLM summarisation, flag extraction, appraisal generation | — |

For step-by-step instructions, see **[QUICKSTART.md](QUICKSTART.md)**.

---

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# OCR for scanned PDFs (macOS)
brew install tesseract poppler
```

Without tesseract/poppler, text PDFs, DOCX, RTF and TXT still work; scanned pages are skipped (and fully scanned PDFs fail) with a message saying what to install.

For the web search crawler, add a SerpAPI key:

```bash
cp .env.example .env    # then set SERPAPI_API_KEY=... (from https://serpapi.com/manage-api-key)
```

| Variable | Needed for | |
|----------|------------|---|
| `SERPAPI_API_KEY` | `search_cli.py` (except `--dry-run`) | Read from `.env` or the environment. Keep `.env` out of version control. |

Run the tests with `pytest` (no network or API key needed).

---

## How applications are named

Everything for one application is stored under a key made of the council and the application reference, so related folders sit side by side:

```
data/documents/
├── eastriding_25_01104_plf/            ← council documents (Phase 1)
├── eastriding_25_01104_plf_search/     ← every web search for that application (Phase 1b, with -a)
├── wiltshire_pl_2025_05830/            ← council comments (Phase 1)
└── wiltshire_pl_2025_05830_search/
```

The same keys are used under `data/metadata/` and `data/extracted/`. The scrapers print the key at the end of a run; use it for `extractor_cli.py` and as `--application` for searches.

## Finding the East Riding application key

To scrape East Riding, give either the application page URL (`--url`) or the portal's internal **key** (`keyVal`), which is not the application reference. Results are still saved under the reference, e.g. `eastriding_18_30161_condet`. Search for the reference on the [portal](https://newplanningaccess.eastriding.gov.uk/newplanningaccess/search.do?action=simple&searchType=Application), open the application, and copy the `keyVal=` value from the URL.

| Reference | Portal key | Saved as |
|-----------|-----|-----|
| `18/30161/CONDET` | `P7SRNYBJ0GJ00` | `eastriding_18_30161_condet` |
| `25/01104/PLF` | `SUPVDJBJKAW00` | `eastriding_25_01104_plf` |

---

## Phase 1: Scraping (`cli.py`, `planning_spider.py`)

```bash
python cli.py -k P7SRNYBJ0GJ00                    # download all documents (except Plans and Amended Plans)
python cli.py -k P7SRNYBJ0GJ00 --max-downloads 2  # test with the first 2
python cli.py -k P7SRNYBJ0GJ00 --fetch-only       # read the table only, no downloads
python cli.py --url "https://newplanningaccess.eastriding.gov.uk/newplanningaccess/applicationDetails.do?activeTab=documents&keyVal=P7SRNYBJ0GJ00"   # same, from the page URL
python cli.py list-documents eastriding_18_30161_condet   # list documents from saved metadata
python cli.py show-metadata eastriding_18_30161_condet    # print the full metadata JSON
```

### How it works

1. Loads `applicationDetails.do?activeTab=documents&keyVal=KEY` with Crawlee. It reads the application reference from the page header and parses the documents table (date published, document type, measure, drawing number, description, view link).
2. Opens the same page with aiohttp to get a session cookie. **The portal returns 404 for document files without this cookie.**
3. Downloads each file directly from its "View" link, waiting 1 second between files.
4. Saves the reference, portal key, the table and the download results to `data/metadata/eastriding_<reference>_metadata.json` (also with `--fetch-only`).

### Skipping document types

Document types listed in `SKIP_DOCUMENT_TYPES` at the top of `planning_spider.py` stay in the metadata but aren't downloaded. By default this skips drawings (Plans and Amended Plans):

```python
SKIP_DOCUMENT_TYPES = {"plans", "amended plans"}   # lowercase; e.g. add "consultee comment"
```

`--max-downloads` counts only the documents that will be downloaded.

### File names

Downloaded files are named `{date}_{document type}_{drawing number or description}.{ext}`, e.g. `2018-10-03_decision_notice_of_decision.pdf`. Names are lowercased, unsafe characters become `_`, and duplicates get `_2`, `_3`, …

### Wiltshire and other Arcus registers: comments and documents (`arcus_scraper.py`)

Wiltshire's register (`development.wiltshire.gov.uk/pr3/...`) runs on Arcus Built Environment, a Salesforce product used by several councils. For these, the scraper saves the **Comments** and **Documents** tabs.

```bash
python cli.py --url "https://development.wiltshire.gov.uk/pr3/s/planning-application/a0iQ300000FJY0XIAX/pl202505830"
python extractor_cli.py extract wiltshire_pl_2025_05830

python cli.py --url "..." --only comments          # just the comments (fast)
python cli.py --url "..." --only documents         # just the documents
python cli.py --url "..." --max-downloads 5        # test with the first 5 documents
python cli.py --url "..." --all-categories         # also download plans, revised and superseded documents
python cli.py --url "..." --fetch-only             # list everything in the metadata, download nothing
```

Paste the application's page URL from your browser (any tab; extra `?...` parameters are fine). Other Arcus councils work the same way, as long as their URLs contain `/s/planning-application/<record id>`.

The scraper then:

1. Calls the register's own API, the same one the page uses, rather than parsing the page. These pages are drawn by JavaScript, so their HTML contains no data. No browser is needed.
2. Fetches the application details (reference, address, proposal, type, officer, dates, decision, wards, parishes), every comment (name, stance, date, text) and the full document list. The register sends the whole list at once; the 20-per-page view on the website is only how the page displays it.
3. Saves each comment as `{date}_comment_{stance}_{name}.txt` in `data/documents/<council>_<reference>/`, e.g. `wiltshire_pl_2025_05830/2025-08-11_comment_object_andre_kayani.txt`. The file starts with a short header (application, commenter, stance, date), followed by the comment text.
4. Downloads each document, one at a time with a 1-second pause, into the same folder as `{document date}_{category}_{title}.{ext}`, e.g. `2026-09-14_decision_decision_notice.pdf`. Documents already downloaded by an earlier run (same name and size) aren't downloaded again.
5. Saves `data/metadata/<council>_<reference>_metadata.json`. It holds the application details and one row per comment and per document. Comments have `commenter`, `stance`, `comment_id`, `created_at` and `text`. Documents have `title`, `category`, `file_id`, `file_url`, `uploaded_at` and `size_bytes`. It also includes counts by stance and by category.

The key (`wiltshire_pl_2025_05830`) is printed at the end of the run; use it with `extractor_cli.py` as usual. Besides `all_documents.txt`, extraction writes `comments.txt` (every comment, with a count by stance at the top) and `documents.txt` (everything else), each with a matching `.json`. `commenter`, `stance`, `category` and the IDs are carried through to `all_documents.json`.

### Skipped document categories

Documents in these categories are listed in the metadata but not downloaded (`SKIP_CATEGORIES` at the top of `arcus_scraper.py`; `--all-categories` downloads them too):

| Category | What's in it |
|----------|--------------|
| Plan | Drawings and elevations. On Wiltshire it also holds the Framework Construction Traffic Management Plan. |
| Revised Plans/Documents | Revised submissions. Despite the name, mostly revised reports: on PL/2025/05830, the final ecology and biodiversity net gain documents. |
| Superseded | Older versions of documents that were later replaced |

Spreadsheets (`.xlsx`, `.xlsm`) and images are downloaded but skipped by the extractor.

### "Download all" on the website

The Documents tab has a "Download all" button. The scraper doesn't use it, because:
- it can't skip categories;
- it produces one large zip, so a single failure means starting over;
- the files in a zip lose the per-document details (category, date, ID) that link each file to its metadata.

Downloading files one by one also means a re-run only fetches documents that are new.

### Notes

The register API doesn't say whether a comment is from a consultee (e.g. "Natural England", "Ecology") or a member of the public. Tell them apart by name. Comments from the public include people's names. They're published on the council's register, but handle them as personal data in anything you produce.

---

## Phase 1b: Web search (`search_cli.py`, `google_search_crawler.py`)

Finds news articles, council pages, objection letters and other web pages about a project. You type each Google search exactly as you would in the browser; the tool runs it, downloads the results and extracts their text the same way as the council documents.

```bash
# Preview the searches and API requests; nothing is sent (no API key needed)
python search_cli.py "round river solar miami county indiana" --country us --dry-run

# Search, download every result, extract the text
python search_cli.py "round river solar miami county indiana" --country us -k miamicounty_round_river_solar_search

# Several searches in one run (each quoted string is one Google search)
python search_cli.py '"Round River Solar"' '"Round River Solar" zoning OR lawsuit OR moratorium' --country us -k miamicounty_round_river_solar_search

# Label each result by whether its text mentions the project (-p), or keep only those that do
python search_cli.py '"Round River Solar"' -p "Round River Solar" --country us
python search_cli.py '"Round River Solar"' -p "Round River Solar" --require-project-name --country us

# Searches for an application you've scraped: results go to <application key>_search,
# and every search with the same -a is added to that one folder
python search_cli.py -a wiltshire_pl_2025_05830 "flaxlands solar farm" "PL/2025/05830" -p "Flaxlands"

# Run the searches and save the results table, but don't download anything
python search_cli.py "flaxlands solar farm" --search-only

# Settings from a file (command-line options override it)
python search_cli.py -c search_config.example.json

# List what was found: title, URL, search, rank, whether it mentions the project
python search_cli.py list-results wiltshire_pl_2025_05830_search --skipped
```

Each search is sent to Google exactly as typed, so Google's own syntax works: `"exact phrase"`, `OR`, `-exclude`, `site:bbc.co.uk`. In the terminal, wrap a search that contains double quotes in single quotes: `'"Round River Solar" zoning'`.

### Options

| Option | Default | |
|--------|---------|---|
| `SEARCH ...` | (required) | One or more Google searches, each sent exactly as written |
| `--project`, `-p` | none | Project name to check each result's text for. Results are labelled `mentions_project` true/false; without `-p` they aren't labelled. |
| `--require-project-name` | off | Drops pages whose extracted text doesn't contain the `--project` name (case-insensitive) |
| `--max-results`, `-m` | 10 | Per search. Above 10, extra pages are requested (one API call, i.e. one credit, each). |
| `--delay` | 2 s | Between every request (searches and page downloads); also the backoff base |
| `--max-retries` | 3 | Retries on 429, 5xx, timeouts and CAPTCHA pages, with exponential backoff (2 s, 4 s, 8 s + jitter, or `Retry-After` if longer) |
| `--country` / `--language` | `uk` / `en` | Google `gl` / `hl`. Use `--country us` for US projects. |
| `--application`, `-a` | | Key of the scraped application the search is for (e.g. `wiltshire_pl_2025_05830`). Results are saved under `<key>_search`. |
| `--key`, `-k` | `search_<project or first search>` | Storage key, for searches not tied to a scraped application |
| `--fresh` | | Discard the key's earlier results and start over |
| `--dry-run` | | Prints the searches and the SerpAPI requests (key masked) and exits |
| `--search-only` / `--no-extract` | | Stop after searching / after downloading |

A config file is JSON with the same settings in snake_case: `queries` (a list of searches), `project`, `max_results`, `delay`, `max_retries`, `country`, `language` and `require_project_name`. It also accepts `extra_blocked_domains`, which adds sites to skip. See `search_config.example.json`.

### Search provider: SerpAPI

Scraping google.com directly is against Google's terms and is quickly blocked by CAPTCHAs. Google's Custom Search JSON API is closed to new customers and being retired. The crawler therefore uses [SerpAPI](https://serpapi.com), which returns real Google results (UK region by default). Each query uses one SerpAPI search credit per page of 10 results, so check your plan's monthly allowance. Use `--dry-run` to see how many requests a run will make.

The provider is a small class (`SerpApiProvider`) with a `search()` method, so another provider can be added alongside it.

### How it works

1. Runs each search through SerpAPI. Only organic results are used: Google's ads, maps and "people also ask" boxes are never included.
2. Skips noise before downloading:
   - Google's own pages
   - ad and tracking links
   - social media and video sites (Facebook, Instagram, X/Twitter, LinkedIn, TikTok, Pinterest, YouTube, Reddit, Threads), which are mostly login walls. The list is in `BLOCKED_DOMAINS`.
   - council planning-register pages: application pages, comments and document files on Idox, Arcus/Salesforce, Northgate, Agile and Ocella registers. The portal scrapers (Phase 1) collect these. Council news pages and press releases are kept.

   Skipped results keep Google's title and snippet in the metadata. For social media posts that is the only text collected; `list-results KEY --skipped` shows it.
3. Skips duplicate URLs across all queries. URLs are compared without `#fragments`, `utm_*`/`gclid`/`fbclid` tracking parameters, `www.` or trailing slashes. A duplicate's extra queries are recorded on the first copy (`queries`).
4. Downloads each result with a pause between requests. It saves HTML, PDF, DOCX, RTF and TXT; other types (images, spreadsheets) are skipped. Pages that turn out to be a CAPTCHA/bot check (after retries), a login wall, or only a JavaScript loader with no content (e.g. Salesforce sites) are skipped.
5. Extracts the text with the same extractor as the council documents. PDFs get the same text layer and OCR handling. Web pages use [trafilatura](https://trafilatura.readthedocs.io), which keeps the article text and drops navigation, footers and cookie banners.
6. Marks each page with `mentions_project`. With `--require-project-name`, pages that don't mention the project are left out of `all_documents.*` but kept on disk.

Every result that was skipped, and why, is listed under `skipped` in the metadata file.

Results build up under a key. Each run adds the results it finds that aren't there yet, and downloads only those. A URL that turns up again is recorded against the new search, not added twice. Each result keeps the `--project` name given with the search that found it (`project_name`), and the `mentions_project` check uses that name. Use `--fresh` to start a key over.

### Retracing a result

`data/metadata/<key>_metadata.json` keeps the search log and one row per unique result:

```json
{
  "application_key": "wiltshire_pl_2025_05830_search",
  "source": "web_search",
  "linked_application": "wiltshire_pl_2025_05830",
  "projects": ["Flaxlands"],
  "provider": "serpapi",
  "searches": [
    {"query": "\"Example Solar Farm\" objection", "project_name": "Example Solar Farm", "results_returned": 10,
     "pages": [{"search_id": "6710f...", "google_url": "https://www.google.co.uk/search?q=...", "searched_at": "..."}]}
  ],
  "documents_table": [
    {
      "filename": "01_bbc_co_uk_council_approves_example_solar_farm",
      "source_url": "https://www.bbc.co.uk/news/...",
      "title": "Council approves Example Solar Farm",
      "snippet": "The planning committee approved ...",
      "displayed_link": "bbc.co.uk › news",
      "result_date": "3 days ago",
      "query": "\"Example Solar Farm\" objection",
      "queries": ["\"Example Solar Farm\" objection", "\"Example Solar Farm\" approved"],
      "search_rank": 1,
      "project_name": "Example Solar Farm",
      "provider": "serpapi",
      "search_id": "6710f...",
      "searched_at": "2026-10-08T15:02:11",
      "scraped_at": "2026-10-08T15:02:30",
      "mentions_project": true
    }
  ],
  "downloaded": [{"filename": "...", "file_path": "...", "final_url": "...", "content_type": "text/html", "size_bytes": 81234}],
  "skipped": [{"source_url": "https://www.facebook.com/...", "query": "...", "search_rank": 4, "reason": "blocked site"}]
}
```

- `search_rank` is the position in Google's results for `query`.
- `search_id` looks up the original SerpAPI response in your [SerpAPI searches archive](https://serpapi.com/searches).
- `google_url` is the Google search page that was used.
- `final_url` is the address after redirects.

The same fields are copied into `raw_texts/*.json` (under `source`) and into each document in `all_documents.json`, so web results and council documents can be merged or compared.

---

## Phase 2a: Extraction (`extractor_cli.py`, `pdf_extractor.py`, `document_detector.py`)

```bash
python extractor_cli.py analyze eastriding_18_30161_condet   # file type of each document; text/scanned/mixed for PDFs
python extractor_cli.py extract eastriding_18_30161_condet   # extract, chunk and compile everything
python extractor_cli.py compile eastriding_18_30161_condet   # rebuild all_documents.* from existing extractions (fast)
python extractor_cli.py extract eastriding_18_30161_condet --types pdf,docx,rtf,txt,doc   # also extract legacy .doc
```

### Supported formats

| Format | How | Default |
|--------|-----|---------|
| PDF | pdfplumber for the text layer; tesseract OCR (300 dpi) for pages without usable text | ✅ |
| DOCX | python-docx (paragraphs, then tables as `cell \| cell` rows) | ✅ |
| RTF | striprtf | ✅ |
| TXT | read directly (UTF-8, then cp1252, then latin-1) | ✅ |
| HTML | trafilatura (main text only; for web search results) | ✅ |
| DOC | macOS `textutil` | opt-in via `--types` |

### How PDFs are handled

Each page is checked separately:

- **Text page:** the text layer is used.
- **Page needing OCR:** either an image with fewer than 25 characters of text, or a page where more than 20% of the "text" is `(cid:NN)` placeholders (fonts with no character mapping, common in lab reports).

A PDF is reported as `text`, `scanned` or `mixed` accordingly. Page boundaries are kept in the text as `[Page N]` markers.

### Chunking

Text is split into chunks of up to ~4,000 tokens (estimated as characters ÷ 4), breaking at paragraphs first, then sentences, then words.

---

## Output

```
data/
├── documents/KEY/                   # downloaded files (KEY = <council>_<reference>, or …_search for web searches)
├── metadata/KEY_metadata.json       # portal table (or search log + results) + download log
└── extracted/KEY/
    ├── all_documents.txt            # every document's text in one readable file
    ├── comments.txt / .json         # Arcus registers only: just the public and consultee comments
    ├── documents.txt / .json        # Arcus registers only: just the documents
    ├── all_documents.json           # same, structured
    ├── extraction_summary.json      # status, tokens and errors per document and per type
    └── raw_texts/{file}.json        # one per document: text, chunks, metadata, source
```

### `all_documents.txt`

```
================================================================================
DOCUMENT 17/17: 2018-10-03_decision_notice_of_decision.pdf
Type: Decision | Description: NOTICE OF DECISION | Published: 03 Oct 2018
Source: https://newplanningaccess.eastriding.gov.uk/.../18_30161_CONDET-NOTICE_OF_DECISION-3172530.pdf
================================================================================

[Page 1]
EAST RIDING OF YORKSHIRE COUNCIL
...
```

### `raw_texts/{file}.json`

```json
{
  "filename": "2018-10-03_decision_notice_of_decision.pdf",
  "file_type": "pdf",
  "pdf_subtype": "scanned",
  "status": "success",
  "text": "[Page 1]\n...",
  "chunks": [{"chunk_id": 0, "text": "...", "estimated_tokens": 3466}],
  "metadata": {
    "extraction_method": "pytesseract",
    "total_pages": 4,
    "ocr_used": true,
    "ocr_pages": [1, 2, 3, 4],
    "ocr_pages_skipped": [],
    "estimated_tokens": 3466
  },
  "source": {
    "document_type": "Decision",
    "description": "NOTICE OF DECISION",
    "date_published": "03 Oct 2018",
    "drawing_number": "",
    "view_url": "https://..."
  }
}
```

---

## Python API

```python
import asyncio
from planning_spider import PlanningDocumentScraper
from pdf_extractor import DocumentExtractor

async def process(key_val: str):
    scraper = PlanningDocumentScraper(key_val)
    await scraper.run(download=True)               # saves under scraper.application_key
    summary = await DocumentExtractor(scraper.application_key).extract_all_documents()  # also writes all_documents.*
    print(scraper.application_key, summary['overall'])

for key_val in ["P7SRNYBJ0GJ00"]:
    asyncio.run(process(key_val))
```

---

## Performance

Measured on `18/30161/CONDET` (37 documents listed, 17 downloaded once Plans are skipped):

| Step | Time | Notes |
|------|------|-------|
| Download | ~1.5 min | 17 PDFs, ~26 MB |
| Extract | ~7 min | mostly OCR: 227 pages across 7 documents |
| Compile | ~2 s | |

Result: ~172,000 tokens of text.

---

## Troubleshooting

| Problem | Cause / fix |
|---------|-------------|
| "No documents found in table" | Wrong key, or the page layout changed. Run `python debug_scraper.py KEY`: it prints the table structure, saves the page HTML, and test-downloads the first document. |
| Downloads fail with status 404 | The session cookie isn't being sent; see *How it works* above. |
| `Scanned PDF needs OCR, but tesseract/poppler are not installed` | `brew install tesseract poppler` |
| Text full of `(cid:NN)` | Shouldn't happen: these pages are OCR'd. If the ratio is borderline, lower `MAX_CID_RATIO` in `pdf_extractor.py`. |
| Poor OCR on non-English text | `brew install tesseract-lang` |
| `ModuleNotFoundError` | Activate the venv: `source venv/bin/activate` |
| `SERPAPI_API_KEY is not set` | Copy `.env.example` to `.env` and add your key |
| `SerpAPI rejected the API key` / `run out of searches` | Check the key and remaining credits at serpapi.com |
| A web result is `No main text found on page` | The page is built by JavaScript or is mostly images; trafilatura gets no text from the raw HTML |
| Many results don't mention the project | Normal for broad searches; put the project name in quotes in the search, or use `-p NAME --require-project-name` |

## Known limitations

- Supported portals: East Riding (documents) and Arcus registers such as Wiltshire (comments and documents).
- Web search: pages behind paywalls, logins or JavaScript-only rendering yield little or no text. The project-name check is an exact phrase match, so abbreviations and variant names count as "not mentioned".
- The application key must be looked up by hand (see above).
- OCR quality depends on the scan; tables and drawings in scanned pages come out as loose text.
- Token counts are estimates (characters ÷ 4), not a real tokenizer.
