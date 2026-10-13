"""
Arcus Public Register Scraper
Scrapes the comments and documents of a planning application from councils that use the Arcus
Built Environment public register (a Salesforce site), e.g. Wiltshire:
https://development.wiltshire.gov.uk/pr3/s/planning-application/a0iQ300000FJY0XIAX/pl202505830

The register's pages are rendered by JavaScript, so instead of parsing HTML this calls the
same Salesforce "Aura" API the page itself uses.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from google_search_crawler import RetryableError, with_retries
from planning_spider import DOCUMENTS_DIR, METADATA_DIR, USER_AGENT, application_storage_key

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# https://<host>[/<site prefix>]/s/planning-application/<record id>[/<slug>]
APPLICATION_URL = re.compile(
    r'^(https?://[^/]+)((?:/[^/]+)*?)/s/planning-application/([A-Za-z0-9]{15,18})(?:[/?#]|$)'
)
DEFAULT_REGISTER = 'Arcus_BE_Public_Register'

# Apex classes behind the register's tabs
VIEW_SERVICE = 'PublicRegisterViewService'
COMMENT_SERVICE = 'PR_CommentService'
FILES_SERVICE = 'PR_FilesListCont'

# Document categories (lowercase) that are listed in the metadata but not downloaded
SKIP_CATEGORIES = {'plan', 'revised plans/documents', 'superseded'}


class ArcusError(Exception):
    """The register returned an error or something unexpected."""


def is_arcus_url(url: str) -> bool:
    return bool(APPLICATION_URL.match(url.strip()))


def _slug(text: str, max_len: int) -> str:
    # Same rule as the portal scraper's filenames
    return re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_')[:max_len].rstrip('_')


def council_from_host(host: str) -> str:
    """'development.wiltshire.gov.uk' -> 'wiltshire'."""
    parts = host.lower().split(':')[0].split('.')
    if 'gov' in parts and parts.index('gov') > 0:
        return parts[parts.index('gov') - 1]
    return parts[-2] if len(parts) > 1 else parts[0]


class ArcusScraper:
    """Scrapes the comments and documents tabs of an Arcus public register planning application."""

    def __init__(self, url: str, max_retries: int = 3, skip_categories: Optional[set[str]] = None):
        match = APPLICATION_URL.match(url.strip())
        if not match:
            raise ValueError(f"Not an Arcus planning application URL: {url}")

        self.base_url, self.site_prefix, self.record_id = match.groups()
        self.register = parse_qs(urlparse(url).query).get('c__r', [DEFAULT_REGISTER])[0]
        self.council = council_from_host(urlparse(self.base_url).netloc)
        self.page_url = f"{self.base_url}{self.site_prefix}/s/planning-application/{self.record_id}"
        self.max_retries = max_retries
        self.download_delay = 1.0  # seconds between document downloads
        self.skip_categories = {c.strip().lower() for c in (SKIP_CATEGORIES if skip_categories is None
                                                            else skip_categories)}

        # Set once the application's details are fetched
        self.reference = None
        self.application_key = None
        self.app_dir = None
        self.details = {}
        self.comments = []    # one row per comment
        self.files = []       # one row per document
        self.documents = []   # comments + documents (the documents table)
        self.downloaded = []

    # -----------------------------------------------------------------------
    # Aura API
    # -----------------------------------------------------------------------

    async def _aura_context(self, session: aiohttp.ClientSession) -> dict:
        """Read the framework/app version IDs the API needs from the page's HTML."""
        async with session.get(self.page_url) as response:
            response.raise_for_status()
            html = unquote(await response.text())

        fwuid = re.search(r'"fwuid":"([^"]+)"', html)
        loaded = re.search(r'"loaded":(\{[^}]*\})', html)
        if not fwuid or not loaded:
            raise ArcusError("Could not find the Salesforce app version in the page; the site may have changed")
        return {
            'mode': 'PROD',
            'fwuid': fwuid.group(1),
            'app': 'siteforce:communityApp',
            'loaded': json.loads(loaded.group(1)),
            'dn': [], 'globals': {}, 'uad': False,
        }

    async def _call(self, session: aiohttp.ClientSession, context: dict, calls: list[tuple]) -> list:
        """
        Run Apex methods through the Aura endpoint.

        Args:
            calls: (namespace, class name, method) tuples, all called with this record's id

        Returns:
            Each method's return value, in order
        """
        actions = [{
            'id': f'{i};a',
            'descriptor': 'aura://ApexActionController/ACTION$execute',
            'callingDescriptor': 'UNKNOWN',
            'params': {
                'namespace': namespace, 'classname': classname, 'method': method,
                'params': {'recordId': self.record_id, 'registerName': self.register},
                'cacheable': False, 'isContinuation': False,
            },
        } for i, (namespace, classname, method) in enumerate(calls)]
        data = {
            'message': json.dumps({'actions': actions}),
            'aura.context': json.dumps(context),
            'aura.pageURI': self.page_url.removeprefix(self.base_url),
            'aura.token': 'null',
        }
        endpoint = f"{self.base_url}{self.site_prefix}/s/sfsites/aura?r=1&aura.ApexAction.execute={len(actions)}"

        async def post():
            try:
                async with session.post(endpoint, data=data, timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status == 429 or response.status >= 500:
                        raise RetryableError(f"status {response.status}")
                    body = await response.text()
                    if response.status != 200:
                        raise ArcusError(f"Register API returned status {response.status}")
                    return body
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as e:
                raise RetryableError(f"network error: {e!r}")

        body = await with_retries(post, self.max_retries, 2.0, 'Register API')
        try:
            result = json.loads(body.removeprefix('while(1);').strip())
        except json.JSONDecodeError:
            raise ArcusError(f"Register API returned something other than JSON: {body[:200]!r}")

        values = []
        for (_, classname, method), action in zip(calls, result.get('actions', [])):
            if action.get('state') != 'SUCCESS':
                errors = '; '.join(e.get('message', str(e)) for e in action.get('error', [])) or action.get('state')
                raise ArcusError(f"{classname}.{method} failed: {errors}")
            values.append((action.get('returnValue') or {}).get('returnValue'))
        if len(values) != len(calls):
            raise ArcusError("Register API returned fewer results than requested")
        return values

    # -----------------------------------------------------------------------
    # Scraping
    # -----------------------------------------------------------------------

    @staticmethod
    def _flatten_details(header: dict, details: dict) -> dict:
        """Turn the header and detail sections into {label: value}."""
        flat = {'Application Reference': (header or {}).get('title')}
        for f in (header or {}).get('fields', []):
            flat[f['label']] = f.get('value')
        for section in (details or {}).get('sections', []):
            for f in section.get('fields', []):
                value = f.get('value')
                # Dates come as 'YYYY-MM-DD 00:00:00'
                if f.get('type') == 'Date' and value:
                    value = value.split(' ')[0]
                flat[f['label']] = value
        return flat

    @staticmethod
    def _dates(value: str, fmt_in: Optional[str] = None) -> tuple[str, str]:
        """('27 May 2026', '2026-05-27') from an ISO timestamp or a 'YYYY-MM-DD' date."""
        try:
            dt = (datetime.strptime(value, fmt_in) if fmt_in
                  else datetime.fromisoformat(value.replace('Z', '+00:00')))
            return dt.strftime('%d %b %Y'), dt.strftime('%Y-%m-%d')
        except (ValueError, AttributeError):
            return value or '', _slug(value, 20) or 'nodate'

    def _comment_row(self, comment: dict) -> dict:
        created = comment.get('CreatedDate') or ''
        date_published, date_slug = self._dates(created)
        name = (comment.get('arcshared__Name__c') or '').strip()
        stance = (comment.get('arcshared__Stance__c') or '').strip()
        return {
            'date_published': date_published,
            'document_type': 'Comment',
            'description': f"{stance or 'Comment'}: {name or 'Unnamed'}",
            'view_url': self.page_url,
            'filename': f"{date_slug}_comment_{_slug(stance, 30) or 'comment'}_{_slug(name, 40) or 'unnamed'}",
            'commenter': name,
            'stance': stance,
            'comment_id': comment.get('Id'),
            'created_at': created,
            'text': comment.get('arcshared__Text__c') or '',
        }

    def _file_row(self, file: dict) -> dict:
        document_date = file.get('arcshared__Document_Date__c') or ''
        date_published, date_slug = self._dates(document_date, '%Y-%m-%d')
        if date_slug == 'nodate' or not document_date:
            date_published, date_slug = self._dates(file.get('CreatedDate') or '')
        category = (file.get('arcshared__Category__c') or '').strip()
        title = (file.get('Title') or '').strip()
        # Descriptions are often just '.'; keep real ones (e.g. 'Figure 4.3 R0', 'V2.0')
        note = (file.get('Description') or '').strip()
        note = '' if note in ('', '.') else note
        return {
            'date_published': date_published,
            'document_type': category or 'Uncategorised',
            'description': f"{title} ({note})" if note else title,
            'view_url': self.page_url,
            'file_url': f"{self.base_url}{self.site_prefix}/sfc/servlet.shepherd/version/download/{file.get('Id')}",
            'filename': f"{date_slug}_{_slug(category, 30) or 'uncategorised'}_{_slug(title, 40) or 'untitled'}",
            'title': title,
            'category': category,
            'file_id': file.get('Id'),
            'file_extension': (file.get('FileExtension') or '').lower(),
            'size_bytes': file.get('ContentSize'),
            'uploaded_at': file.get('CreatedDate'),
        }

    @staticmethod
    def _make_unique(rows: list[dict]) -> list[dict]:
        # Several rows can share a date/type/name, so keep filenames unique
        seen = set()
        for row in rows:
            base, n = row['filename'], 2
            while row['filename'] in seen:
                row['filename'] = f"{base}_{n}"
                n += 1
            seen.add(row['filename'])
        return rows

    async def fetch_table_data(self) -> list[dict]:
        """Fetch the application's details, its comments and its list of documents."""
        logger.info(f"Fetching record {self.record_id} from {self.base_url}")

        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            context = await self._aura_context(session)
            header, details, comments, files = await self._call(session, context, [
                ('arcuscommunity', VIEW_SERVICE, 'getRecordHeaderDetails'),
                ('arcuscommunity', VIEW_SERVICE, 'getRecordDetails'),
                ('arcuscommunity', COMMENT_SERVICE, 'getComments'),
                ('arcuscommunity', FILES_SERVICE, 'getFiles'),
            ])

        self.details = self._flatten_details(header, details)
        self.reference = self.details.get('Application Reference') or self.record_id
        self.application_key = application_storage_key(self.council, self.reference)
        self.app_dir = DOCUMENTS_DIR / self.application_key

        self.comments = self._make_unique([self._comment_row(c) for c in comments or []])
        # The register lists every document in one response (the page's 20-per-page view is client-side)
        self.files = self._make_unique([self._file_row(f) for f in files or []])
        self.documents = self.comments + self.files
        logger.info(f"{self.reference}: {len(self.comments)} comments, {len(self.files)} documents")
        return self.documents

    def _comment_file_text(self, row: dict) -> str:
        return (
            f"Application: {self.reference}\n"
            f"Commenter: {row['commenter']}\n"
            f"Stance: {row['stance']}\n"
            f"Date: {row['date_published']}\n\n"
            f"{row['text'].strip()}\n"
        )

    def save_comments(self) -> list[dict]:
        """Write each comment to its own .txt file, replacing a previous run's comment files."""
        self.app_dir.mkdir(parents=True, exist_ok=True)
        for old in self.app_dir.glob('*_comment_*.txt'):
            old.unlink()

        saved = []
        for row in self.comments:
            file_path = self.app_dir / f"{row['filename']}.txt"
            file_path.write_text(self._comment_file_text(row), encoding='utf-8')
            saved.append({
                'filename': row['filename'],
                'file_path': str(file_path),
                'document_type': row['document_type'],
                'view_url': row['view_url'],
                'downloaded_at': datetime.now().isoformat(),
                'size_bytes': file_path.stat().st_size,
            })
        self.downloaded += saved
        logger.info(f"Saved {len(saved)} comments to {self.app_dir}")
        return saved

    def files_to_download(self, max_downloads: Optional[int] = None) -> list[dict]:
        """The documents that will be downloaded: all except the skipped categories."""
        wanted = [f for f in self.files if f['category'].strip().lower() not in self.skip_categories]
        return wanted[:max_downloads] if max_downloads else wanted

    async def _download_file(self, session: aiohttp.ClientSession, row: dict) -> Optional[Path]:
        file_path = self.app_dir / f"{row['filename']}.{row['file_extension'] or 'bin'}"
        # Already downloaded by an earlier run (same size): don't fetch it again
        if file_path.exists() and row.get('size_bytes') and file_path.stat().st_size == row['size_bytes']:
            return file_path

        async def fetch():
            try:
                async with session.get(row['file_url'], timeout=aiohttp.ClientTimeout(total=600)) as response:
                    if response.status == 429 or response.status >= 500:
                        raise RetryableError(f"status {response.status}")
                    if response.status != 200:
                        raise ArcusError(f"status {response.status}")
                    if 'json' in response.headers.get('content-type', ''):
                        raise ArcusError("register returned an error page instead of the file")
                    return await response.read()
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as e:
                raise RetryableError(f"network error: {e!r}")

        body = await with_retries(fetch, self.max_retries, 2.0, f"Download {row['title']}")
        file_path.write_bytes(body)
        return file_path

    async def download_files(self, max_downloads: Optional[int] = None) -> list[dict]:
        """Download the documents (except skipped categories) into the application's folder."""
        self.app_dir.mkdir(parents=True, exist_ok=True)
        to_download = self.files_to_download(max_downloads)
        skipped = len(self.files) - len(self.files_to_download())
        if skipped:
            logger.info(f"Skipping {skipped} documents in categories: {', '.join(sorted(self.skip_categories))}")
        logger.info(f"Downloading {len(to_download)} documents")

        saved = []
        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            for i, row in enumerate(to_download):
                try:
                    file_path = await self._download_file(session, row)
                except (ArcusError, RetryableError, aiohttp.ClientError) as e:
                    row['download_error'] = str(e)
                    logger.error(f"Failed to download {row['title']}: {e}")
                    continue
                saved.append({
                    'filename': row['filename'],
                    'file_path': str(file_path),
                    'document_type': row['document_type'],
                    'view_url': row['view_url'],
                    'file_url': row['file_url'],
                    'downloaded_at': datetime.now().isoformat(),
                    'size_bytes': file_path.stat().st_size,
                })
                logger.info(f"Downloaded ({i + 1}/{len(to_download)}): {file_path.name}")
                await asyncio.sleep(self.download_delay)  # Be polite to the register
        self.downloaded += saved
        return saved

    def _save_metadata(self) -> Path:
        METADATA_DIR.mkdir(parents=True, exist_ok=True)

        def count(rows, key):
            counts = {}
            for row in rows:
                counts[row[key] or 'Unknown'] = counts.get(row[key] or 'Unknown', 0) + 1
            return counts

        metadata = {
            'application_key': self.application_key,
            'council': self.council,
            'portal': 'arcus',
            'reference': self.reference,
            'record_id': self.record_id,
            'source_url': self.page_url,
            'scraped_at': datetime.now().isoformat(),
            'application_details': self.details,
            'skip_categories': sorted(self.skip_categories),
            # Comments and documents together, like the portal scraper's documents table
            'documents_table': self.documents,
            'downloaded': self.downloaded,
            'summary': {
                'total_comments': len(self.comments),
                'by_stance': count(self.comments, 'stance'),
                'total_documents': len(self.files),
                'by_category': count(self.files, 'category'),
                'documents_downloaded': sum(1 for d in self.downloaded if d['document_type'] != 'Comment'),
                'total_size_bytes': sum(d.get('size_bytes', 0) for d in self.downloaded),
            },
        }
        metadata_file = METADATA_DIR / f"{self.application_key}_metadata.json"
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved metadata to {metadata_file}")
        return metadata_file

    async def run(self, download: bool = True, max_downloads: Optional[int] = None,
                  comments: bool = True, documents: bool = True):
        """
        Fetch the comments and documents and save them.

        Args:
            download: write comment files and download documents (False = metadata only)
            max_downloads: maximum number of documents to download (comments are always all saved)
            comments / documents: which tabs to save
        """
        await self.fetch_table_data()
        if download and comments:
            self.save_comments()
        if download and documents:
            await self.download_files(max_downloads=max_downloads)
        self._save_metadata()
