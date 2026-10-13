"""
Web Search Crawler
Finds web pages that mention a project (via Google results from SerpAPI), downloads them,
and stores them in the same layout as the planning portal scraper so the extractor can process them.
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
from dotenv import load_dotenv

from planning_spider import DOCUMENTS_DIR, METADATA_DIR, USER_AGENT

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

API_KEY_ENV = "SERPAPI_API_KEY"

# Defaults (all overridable from the CLI or a config file)
DEFAULT_MAX_RESULTS = 10      # per query
DEFAULT_DELAY = 2.0           # seconds between requests (searches and page downloads)
DEFAULT_MAX_RETRIES = 3
DEFAULT_COUNTRY = "uk"        # Google 'gl'
DEFAULT_LANGUAGE = "en"       # Google 'hl'

# Results from these sites are skipped: social media (login walls) and video
BLOCKED_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "tiktok.com", "pinterest.com", "youtube.com", "reddit.com", "threads.net",
}
# Ad and tracking hosts
AD_DOMAINS = {"googleadservices.com", "doubleclick.net", "googlesyndication.com"}
# Google's own pages (google.com, google.co.uk, maps.google.com, ...)
GOOGLE_HOST = re.compile(r'(^|\.)google\.[a-z.]+$|(^|\.)(googleusercontent|gstatic)\.com$')

# Council planning register pages (application details, comments, document files). These are
# collected by the portal scrapers (Phase 1), so search results pointing at them are skipped.
# Council news pages and press releases don't match and are kept.
PLANNING_REGISTER = re.compile(
    r'/online-applications/'                                  # Idox Public Access
    r'|/applicationdetails\.do|/simplesearchresults\.do|/pagedsearchresults\.do'
    r'|/newplanningaccess/'                                   # Idox (East Riding)
    r'|/s/(planning-application|web-comment|sfsites)/'        # Arcus / Salesforce registers
    r'|/s/[a-z-]+/a0[a-z0-9]{13,16}(/|$)'                     # other Salesforce register records
    r'|planningexplorer|/northgate/|/aniteim\.websearch/'      # Northgate
    r'|/ocellaweb/'                                           # Ocella
    r'|agileapplications\.co\.uk|/planningapps?/'             # Agile and similar
    r'|//(publicaccess|planningaccess|planning-register|planningregister)\.',
    re.I,
)

# Query parameters that only track clicks; dropped when comparing URLs
TRACKING_PARAMS = re.compile(r'^(utm_.*|gclid|fbclid|msclkid|mc_cid|mc_eid|ref|ref_src)$', re.I)

# Text that means we got a bot check or a login page instead of the content
CAPTCHA_MARKERS = (
    "verify you are human", "unusual traffic from your computer", "cf-chl-",
    "challenge-platform", "are you a robot", "checking your browser before accessing",
)
LOGIN_WALL_MARKERS = (
    "log in to continue", "sign in to continue", "you must be logged in",
    "login to view", "sign in to view",
)
# Salesforce Experience sites (e.g. council Arcus registers) send only a JavaScript loader;
# the content never appears in the HTML. Comments on Arcus registers come from arcus_scraper.py.
JS_SHELL_MARKERS = ("/s/sfsites/", "siteforce")

# Downloadable content types -> extension (anything else is skipped)
CONTENT_TYPES = {
    'application/pdf': '.pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/rtf': '.rtf',
    'text/rtf': '.rtf',
    'text/html': '.html',
    'application/xhtml+xml': '.html',
    'text/plain': '.txt',
}

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SearchError(Exception):
    """A failure that retrying won't fix (bad API key, out of credits, ...)."""


class SkipResult(Exception):
    """A result page that can't be used (404, login wall, too large, ...)."""


class RetryableError(Exception):
    """A temporary failure (429, 5xx, timeout, CAPTCHA) worth retrying after a pause."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


async def with_retries(func, max_retries: int, base_delay: float, description: str):
    """
    Call `func()` (an async function), retrying RetryableErrors with exponential backoff.

    Waits base_delay * 2^attempt (plus jitter), or the server's Retry-After if longer.
    """
    for attempt in range(max_retries + 1):
        try:
            return await func()
        except RetryableError as e:
            if attempt == max_retries:
                raise
            wait = max(base_delay * 2 ** attempt + random.uniform(0, 1), e.retry_after or 0)
            logger.warning(f"{description}: {e}; retrying in {wait:.1f}s ({attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)


def _retry_after(response) -> Optional[float]:
    try:
        return float(response.headers.get('Retry-After', ''))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Queries & URLs
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Canonical form of a URL for spotting duplicates (no fragment, tracking params or trailing slash)."""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix('www.')
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if not TRACKING_PARAMS.match(k)])
    path = parsed.path.rstrip('/') or '/'
    return urlunparse((parsed.scheme.lower() or 'https', host, path, '', query, ''))


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().split(':')[0].removeprefix('www.')


def _matches_domain(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith('.' + d) for d in domains)


def noise_reason(url: str, blocked_domains: set[str] = BLOCKED_DOMAINS) -> Optional[str]:
    """Why a result URL should be skipped, or None if it looks like real content."""
    if not url.lower().startswith(('http://', 'https://')):
        return 'not a web URL'
    host = _host(url)
    if _matches_domain(host, AD_DOMAINS):
        return 'ad'
    if GOOGLE_HOST.search(host):
        return 'Google page'
    if _matches_domain(host, blocked_domains):
        return 'blocked site'
    if PLANNING_REGISTER.search(url):
        return 'council planning register'
    return None


def page_block_reason(html: str) -> Optional[str]:
    """Detect a CAPTCHA/bot check, a login wall, or a JavaScript-only page in a downloaded page."""
    head = html[:50_000].lower()
    if any(m in head for m in CAPTCHA_MARKERS):
        return 'CAPTCHA / bot check'
    if any(m in head for m in LOGIN_WALL_MARKERS):
        return 'login wall'
    if all(m in html.lower() for m in JS_SHELL_MARKERS):
        return 'JavaScript-only page'
    return None


def mentions_project(text: str, project: str) -> bool:
    """Case- and whitespace-insensitive check that the project name appears in the text."""
    def norm(s: str) -> str:
        return re.sub(r'\s+', ' ', s).strip().lower()
    return bool(text) and norm(project) in norm(text)


def _slug(text: str, max_len: int) -> str:
    # Same rule as the portal scraper's filenames
    return re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_')[:max_len].rstrip('_')


def project_key(text: str) -> str:
    """Default storage key for search results, e.g. 'search_round_river_solar' (from the project or query)."""
    return f"search_{_slug(text, 60) or 'results'}"


# ---------------------------------------------------------------------------
# Provider: SerpAPI (Google results)
# ---------------------------------------------------------------------------

class SerpApiProvider:
    """
    Google search results via SerpAPI (https://serpapi.com/search-api).

    Only organic results are used, so Google's ads, maps and "people also ask" boxes
    are never included.
    """

    name = 'serpapi'
    ENDPOINT = 'https://serpapi.com/search.json'
    PAGE_SIZE = 10  # Google returns at most 10 organic results per page

    def __init__(self, api_key: Optional[str], country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE):
        self.api_key = api_key
        self.country = country
        self.language = language

    def build_params(self, query: str, start: int = 0) -> dict:
        params = {
            'engine': 'google',
            'q': query,
            'gl': self.country,
            'hl': self.language,
            'num': self.PAGE_SIZE,
            'api_key': self.api_key or f'<{API_KEY_ENV}>',
        }
        if start:
            params['start'] = start
        return params

    def request_url(self, query: str, start: int = 0, mask_key: bool = True) -> str:
        params = self.build_params(query, start)
        if mask_key:
            params['api_key'] = '***'
        return f"{self.ENDPOINT}?{urlencode(params)}"

    @staticmethod
    def parse_results(data: dict) -> list[dict]:
        """Turn a SerpAPI response into result dicts (rank, url, title, snippet, ...)."""
        results = []
        for item in data.get('organic_results', []):
            if not item.get('link'):
                continue
            results.append({
                'search_rank': item.get('position'),
                'source_url': item['link'],
                'title': item.get('title', ''),
                'snippet': item.get('snippet', ''),
                'displayed_link': item.get('displayed_link', ''),
                'result_date': item.get('date', ''),
            })
        return results

    async def _fetch_page(self, session: aiohttp.ClientSession, query: str, start: int) -> dict:
        try:
            async with session.get(self.ENDPOINT, params=self.build_params(query, start),
                                   timeout=aiohttp.ClientTimeout(total=60)) as response:
                try:
                    data = await response.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError):
                    data = {}
                error = data.get('error', '')

                if response.status in (401, 403):
                    raise SearchError(f"SerpAPI rejected the API key ({response.status}): {error}")
                if 'run out of searches' in error.lower():
                    raise SearchError(f"SerpAPI: {error}")
                if response.status == 429 or response.status >= 500:
                    raise RetryableError(f"SerpAPI status {response.status} {error}".strip(), _retry_after(response))
                if error:
                    # "Google hasn't returned any results for this query." is not a failure
                    if "hasn't returned any results" in error:
                        return {**data, 'organic_results': []}
                    raise SearchError(f"SerpAPI: {error}")
                if response.status != 200:
                    raise SearchError(f"SerpAPI status {response.status}")
                return data
        except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as e:
            raise RetryableError(f"network error: {e!r}")

    async def search(self, session: aiohttp.ClientSession, query: str, max_results: int,
                     max_retries: int, delay: float) -> tuple[list[dict], list[dict]]:
        """
        Run one query, paging until max_results are collected.

        Returns:
            (results, pages) where pages records each API call for retracing
            (SerpAPI search id, the Google URL it fetched, when).
        """
        results, pages = [], []
        start = 0
        while len(results) < max_results:
            if start:
                await asyncio.sleep(delay)
            data = await with_retries(
                lambda: self._fetch_page(session, query, start),
                max_retries, delay, f"Search '{query}'",
            )
            meta = data.get('search_metadata', {})
            page_results = self.parse_results(data)
            pages.append({
                'search_id': meta.get('id'),
                'google_url': meta.get('google_url'),
                'json_endpoint': meta.get('json_endpoint'),
                'start': start,
                'results_returned': len(page_results),
                'searched_at': datetime.now().isoformat(),
            })
            for r in page_results:
                r['search_id'] = meta.get('id')
            results += page_results
            if len(page_results) < self.PAGE_SIZE or not data.get('serpapi_pagination', {}).get('next'):
                break
            start += self.PAGE_SIZE
        return results[:max_results], pages


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

@dataclass
class SearchConfig:
    queries: list[str]                   # sent to Google exactly as written, one search each
    project: Optional[str] = None        # name to check result text for ('mentions_project')
    max_results: int = DEFAULT_MAX_RESULTS
    delay: float = DEFAULT_DELAY
    max_retries: int = DEFAULT_MAX_RETRIES
    country: str = DEFAULT_COUNTRY
    language: str = DEFAULT_LANGUAGE
    require_project_name: bool = False
    extra_blocked_domains: list[str] = field(default_factory=list)

    @property
    def blocked_domains(self) -> set[str]:
        return BLOCKED_DOMAINS | {d.lower().removeprefix('www.') for d in self.extra_blocked_domains}


class WebSearchCrawler:
    """
    Runs Google searches (exactly as written), downloads the result pages, and extracts their text.

    Results build up under one key across runs, so every search for an application can share
    one folder ('<application key>_search'). Each run adds its new results; URLs already found
    are not downloaded again. Pass fresh=True to start the key over.
    """

    def __init__(self, config: SearchConfig, key: Optional[str] = None, provider=None, fresh: bool = False):
        self.config = config
        self.queries = [q.strip() for q in config.queries if q and q.strip()]
        if not self.queries:
            raise ValueError("At least one search query is required")
        self.key = key or project_key(config.project or self.queries[0])
        self.fresh = fresh
        self.docs_dir = DOCUMENTS_DIR / self.key
        self.metadata_file = METADATA_DIR / f"{self.key}_metadata.json"
        self.provider = provider or SerpApiProvider(
            os.getenv(API_KEY_ENV), country=config.country, language=config.language
        )

        self.searches = []    # one entry per query run, with the API calls made
        self.results = []     # unique result rows across all runs (the documents_table)
        self.skipped = []     # results dropped as noise, duplicates or failed downloads
        self.downloaded = []
        self.new_results = []      # results first found in this run
        self.new_downloads = []    # files downloaded in this run
        if not fresh:
            self._load_previous()

    def _load_previous(self) -> None:
        """Continue from earlier runs saved under this key."""
        if not self.metadata_file.exists():
            return
        with open(self.metadata_file, encoding='utf-8') as f:
            previous = json.load(f)
        self.searches = previous.get('searches', [])
        self.results = previous.get('documents_table', [])
        self.skipped = previous.get('skipped', [])
        self.downloaded = previous.get('downloaded', [])

        # Metadata from before results were kept across runs stored the project once, not per result
        old_project = previous.get('project_name')
        old_require = (previous.get('settings') or {}).get('require_project_name', False)
        for row in self.results:
            row.setdefault('project_name', old_project)
            row.setdefault('require_project_name', old_require)
        for search in self.searches:
            search.setdefault('project_name', old_project)
            search.setdefault('terms', previous.get('terms', []))
            search.setdefault('settings', previous.get('settings', {}))

    def plan(self) -> list[dict]:
        """The queries and API requests a run would make (for --dry-run)."""
        return [{'query': q, 'request_url': self.provider.request_url(q)} for q in self.queries]

    def _add_skipped(self, entry: dict) -> None:
        # Re-running a search finds the same noise again; record it once
        if not any(s['source_url'] == entry['source_url'] and s['query'] == entry['query']
                   and s['reason'] == entry['reason'] for s in self.skipped):
            self.skipped.append(entry)

    async def search(self, session: aiohttp.ClientSession) -> list[dict]:
        """Run every query and collect unique, non-noise results (new ones are added to self.results)."""
        seen = {normalize_url(r['source_url']): r for r in self.results}
        for i, query in enumerate(self.queries):
            if i:
                await asyncio.sleep(self.config.delay)
            logger.info(f"Searching: {query}")
            results, pages = await self.provider.search(
                session, query, self.config.max_results, self.config.max_retries, self.config.delay
            )
            self.searches.append({
                'query': query,
                'project_name': self.config.project,
                'provider': self.provider.name,
                'settings': {
                    'max_results': self.config.max_results,
                    'country': self.config.country,
                    'language': self.config.language,
                    'require_project_name': self.config.require_project_name,
                },
                'searched_at': datetime.now().isoformat(),
                'pages': pages,
                'results_returned': len(results),
            })
            logger.info(f"  {len(results)} results")

            for r in results:
                record = {'query': query, 'search_rank': r['search_rank'], 'source_url': r['source_url']}
                reason = noise_reason(r['source_url'], self.config.blocked_domains)
                if reason:
                    # Keep Google's title and snippet: for social media posts that's the only text we get
                    self._add_skipped({**record, 'reason': reason, 'title': r['title'],
                                       'snippet': r['snippet'], 'result_date': r['result_date']})
                    continue
                norm = normalize_url(r['source_url'])
                if norm in seen:
                    if query not in seen[norm]['queries']:
                        seen[norm]['queries'].append(query)
                    self._add_skipped({**record, 'reason': 'duplicate'})
                    continue
                row = {
                    **r,
                    'query': query,
                    'queries': [query],
                    'project_name': self.config.project,
                    'require_project_name': self.config.require_project_name,
                    'provider': self.provider.name,
                    'searched_at': datetime.now().isoformat(),
                    # Same keys as the portal scraper's documents table
                    'document_type': 'Web Search Result',
                    'description': r['title'],
                    'date_published': r['result_date'],
                    'view_url': r['source_url'],
                }
                seen[norm] = row
                self.results.append(row)
                self.new_results.append(row)

        # Filenames (without extension), like the portal scraper's; numbering continues across runs
        for n, row in enumerate(self.results, 1):
            row.setdefault(
                'filename', f"{n:02d}_{_slug(_host(row['source_url']), 30)}_{_slug(row['title'], 40) or 'untitled'}"
            )

        logger.info(f"{len(self.new_results)} new results ({len(self.results)} in total for {self.key})")
        return self.new_results

    async def _download_one(self, session: aiohttp.ClientSession, row: dict) -> tuple[Optional[dict], Optional[str]]:
        """Download one result. Returns (downloaded record, None) or (None, reason skipped)."""
        url = row['source_url']

        async def fetch():
            """Returns (content_type, body, final_url); raises SkipResult for pages to skip."""
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=90),
                                       allow_redirects=True) as response:
                    if response.status == 429 or response.status >= 500:
                        raise RetryableError(f"status {response.status}", _retry_after(response))
                    if response.status != 200:
                        raise SkipResult(f"status {response.status}")
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if int(response.headers.get('content-length') or 0) > MAX_DOWNLOAD_BYTES:
                        raise SkipResult('file too large')
                    body = await response.read()
                    if content_type in ('text/html', 'application/xhtml+xml'):
                        block = page_block_reason(body.decode('utf-8', errors='ignore'))
                        if block == 'CAPTCHA / bot check':
                            raise RetryableError(block)
                        if block:
                            raise SkipResult(block)
                    return content_type, body, str(response.url)
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as e:
                raise RetryableError(f"network error: {e!r}")

        try:
            content_type, body, final_url = await with_retries(
                fetch, self.config.max_retries, self.config.delay, f"Download {url}"
            )
        except SkipResult as e:
            return None, str(e)
        except RetryableError as e:
            return None, f"download failed: {e}"
        except aiohttp.ClientError as e:
            return None, f"download failed: {e!r}"

        ext = CONTENT_TYPES.get(content_type) or (
            '.pdf' if urlparse(final_url).path.lower().endswith('.pdf') else None
        )
        if not ext:
            return None, f"unsupported content type: {content_type or 'unknown'}"

        file_path = self.docs_dir / f"{row['filename']}{ext}"
        file_path.write_bytes(body)
        now = datetime.now().isoformat()
        row['scraped_at'] = now
        return {
            'filename': row['filename'],
            'file_path': str(file_path),
            'document_type': row['document_type'],
            'view_url': row['source_url'],
            'file_url': row['source_url'],
            'final_url': final_url,
            'content_type': content_type,
            'downloaded_at': now,
            'size_bytes': len(body),
        }, None

    async def download(self, session: aiohttp.ClientSession) -> list[dict]:
        """Download this run's new results into data/documents/KEY/."""
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        if self.fresh:
            for old in self.docs_dir.iterdir():
                if old.is_file():
                    old.unlink()

        for i, row in enumerate(self.new_results):
            if i:
                await asyncio.sleep(self.config.delay)
            record, reason = await self._download_one(session, row)
            if record:
                self.downloaded.append(record)
                self.new_downloads.append(record)
                logger.info(f"Downloaded: {Path(record['file_path']).name}")
            else:
                row['download_error'] = reason
                self._add_skipped({'query': row['query'], 'search_rank': row['search_rank'],
                                   'source_url': row['source_url'], 'reason': reason})
                logger.warning(f"Skipped {row['source_url']}: {reason}")
        return self.new_downloads

    def save_metadata(self) -> Path:
        """Save the search log, results table and download log (same layout as the portal scraper)."""
        METADATA_DIR.mkdir(parents=True, exist_ok=True)
        metadata = {
            'application_key': self.key,
            'source': 'web_search',
            # e.g. 'wiltshire_pl_2025_05830' for the key 'wiltshire_pl_2025_05830_search'
            'linked_application': self.key.removesuffix('_search') if self.key.endswith('_search') else None,
            'projects': sorted({s['project_name'] for s in self.searches if s.get('project_name')}),
            'provider': self.provider.name,
            'scraped_at': datetime.now().isoformat(),
            'searches': self.searches,
            'documents_table': self.results,
            'downloaded': self.downloaded,
            'skipped': self.skipped,
            'summary': {
                'queries_run': len(self.searches),
                'unique_results': len(self.results),
                'documents_downloaded': len(self.downloaded),
                'skipped': len(self.skipped),
                'total_size_bytes': sum(d['size_bytes'] for d in self.downloaded),
            },
        }
        with open(self.metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved metadata to {self.metadata_file}")
        return self.metadata_file

    def apply_project_filter(self, extractor) -> dict:
        """
        Mark each extracted result with whether its text mentions the project. With
        require_project_name, drop the ones that don't from all_documents.*.
        """
        summary_file = extractor.extract_dir / "extraction_summary.json"
        with open(summary_file, encoding='utf-8') as f:
            summary = json.load(f)

        rows = {r['filename']: r for r in self.results}
        counts = {'mention': 0, 'no_mention': 0, 'filtered': 0}
        for entry in summary['files']:
            if entry['status'] != 'success':
                continue
            output_file = extractor.extract_dir / entry['output_file']
            with open(output_file, encoding='utf-8') as f:
                result = json.load(f)

            # Each result is checked against the project name of the search that found it (if one was given)
            row = rows.get(Path(result['filename']).stem) or {}
            project = row.get('project_name') or self.config.project
            if not project:
                continue
            mentioned = mentions_project(result['text'], project)
            counts['mention' if mentioned else 'no_mention'] += 1
            result['source'] = {**(result.get('source') or {}), 'mentions_project': mentioned}
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            if row:
                row['mentions_project'] = mentioned

            if row.get('require_project_name', self.config.require_project_name) and not mentioned:
                entry['status'] = 'filtered'
                entry['error'] = 'project name not found in text'
                counts['filtered'] += 1

        summary['overall']['filtered'] = counts['filtered']
        summary['overall']['successful'] -= counts['filtered']
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        self.save_metadata()
        extractor.compile_extractions()
        return counts

    async def run(self, fetch: bool = True, extract: bool = True) -> dict:
        """
        Search, download and extract.

        Args:
            fetch: download the result pages (False = search only, save the results table)
            extract: run the document extractor on the downloads
        """
        if not self.provider.api_key:
            raise SearchError(f"{API_KEY_ENV} is not set. Add it to .env (see .env.example).")

        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            await self.search(session)
            if fetch and self.new_results:
                await self.download(session)
        self.save_metadata()

        outcome = {'extraction': None, 'project_filter': None}
        # Re-extract the whole folder when this run added files, so all_documents.* covers every run
        if fetch and extract and self.new_downloads:
            from pdf_extractor import DocumentExtractor
            extractor = DocumentExtractor(self.key)
            outcome['extraction'] = await extractor.extract_all_documents()
            outcome['project_filter'] = self.apply_project_filter(extractor)
            outcome['extract_dir'] = extractor.extract_dir
        return outcome
