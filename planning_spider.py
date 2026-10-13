"""
Planning Portal Document Scraper
Scrapes planning application documents from East Riding planning portal
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
BASE_URL = "https://newplanningaccess.eastriding.gov.uk"
APPLICATION_DETAILS_PATH = "/newplanningaccess/applicationDetails.do"

# Storage paths
DATA_DIR = Path("data")
DOCUMENTS_DIR = DATA_DIR / "documents"
METADATA_DIR = DATA_DIR / "metadata"

COUNCIL = "eastriding"

# Document types (lowercase) that are listed in the metadata but not downloaded
SKIP_DOCUMENT_TYPES = {"plans", "amended plans"}

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


def application_storage_key(council: str, reference: str) -> str:
    """
    Folder/file key for an application: '<council>_<reference>', e.g. 'eastriding_25_01104_plf'.
    Used by every portal scraper (and, with '_search' added, by the web search crawler).
    """
    reference_slug = re.sub(r'[^a-z0-9]+', '_', reference.lower()).strip('_')
    return f"{council}_{reference_slug}"


class PlanningDocumentScraper:
    """Scrapes planning application documents from East Riding portal."""
    
    def __init__(self, key_val: str, max_retries: int = 3):
        """
        Initialize the scraper.
        
        Args:
            key_val: The portal's internal application key (e.g., 'P7SRNYBJ0GJ00')
            max_retries: Maximum number of retries for failed requests
        """
        self.key_val = key_val
        self.max_retries = max_retries
        self.documents = []
        
        # Set once the page is read: storage is named after the reference, e.g. eastriding_25_01104_plf
        self.reference = None
        self.summary = None
        self.application_key = None
        self.app_dir = None
        
        METADATA_DIR.mkdir(parents=True, exist_ok=True)
    
    def _set_reference(self, reference: Optional[str]) -> None:
        # Fall back to the portal key if the page has no reference
        self.reference = reference or self.key_val
        self.application_key = application_storage_key(COUNCIL, self.reference)
        self.app_dir = DOCUMENTS_DIR / self.application_key
        self.app_dir.mkdir(parents=True, exist_ok=True)
        
    
    async def fetch_table_data(self) -> list[dict]:
        """
        Fetch and parse the documents table.
        
        Returns:
            List of document metadata dictionaries
        """
        logger.info(f"Fetching documents table for application {self.key_val}")
        
        # Build the application details URL
        url = f"{BASE_URL}{APPLICATION_DETAILS_PATH}?activeTab=documents&keyVal={self.key_val}"
        
        crawler = BeautifulSoupCrawler(
            parser='html.parser',
            max_requests_per_crawl=1,  # Only fetch the one page
            max_request_retries=self.max_retries,
        )
        
        @crawler.router.default_handler
        async def handle_page(context: BeautifulSoupCrawlingContext) -> None:
            """Parse the documents table from the response."""
            soup = context.soup
            
            # Reference (e.g. 25/01104/PLF) and the "reference | proposal | address" line in the header
            case_number = soup.select_one('span.caseNumber')
            self.reference = case_number.get_text(strip=True) if case_number else None
            crumb = soup.select_one('#pa .addressCrumb')
            self.summary = re.sub(r'\s+', ' ', crumb.get_text(' ', strip=True)) if crumb else None
            
            # Find the documents table
            # Look for table with data rows
            table = soup.find('table')
            if not table:
                logger.error("Could not find documents table")
                return
            
            # Parse table rows (skip header)
            rows = table.find_all('tr')[1:]  # Skip header row
            documents = []
            
            for row_idx, row in enumerate(rows):
                try:
                    cells = row.find_all('td')
                    if len(cells) < 7:  # Need at least 7 columns
                        logger.debug(f"Row {row_idx} has insufficient cells, skipping")
                        continue
                    
                    # Extract cell contents
                    # Based on your table structure:
                    # 0: Checkbox (skip)
                    # 1: Date Published
                    # 2: Document Type
                    # 3: Measure
                    # 4: Drawing Number
                    # 5: Description
                    # 6: View (link)
                    
                    date_published = cells[1].get_text(strip=True)
                    document_type = cells[2].get_text(strip=True)
                    measure = cells[3].get_text(strip=True)
                    drawing_number = cells[4].get_text(strip=True)
                    description = cells[5].get_text(strip=True)
                    
                    # Extract view link
                    view_link_elem = cells[6].find('a')
                    if not view_link_elem:
                        logger.warning(f"No view link found for row {row_idx}")
                        continue
                    
                    view_href = view_link_elem.get('href')
                    if not view_href:
                        logger.warning(f"No href found for row {row_idx}")
                        continue
                    
                    # Convert relative URL to absolute
                    view_url = urljoin(BASE_URL, view_href)
                    
                    doc = {
                        'date_published': date_published,
                        'document_type': document_type,
                        'measure': measure,
                        'drawing_number': drawing_number,
                        'description': description,
                        'view_url': view_url,
                        'filename': self._generate_filename(
                            document_type, 
                            drawing_number or description, 
                            date_published
                        )
                    }
                    
                    # Several documents can share a type/date/drawing, so keep names unique
                    base, n = doc['filename'], 2
                    while any(d['filename'] == doc['filename'] for d in documents):
                        doc['filename'] = f"{base}_{n}"
                        n += 1
                    
                    documents.append(doc)
                    logger.info(f"Extracted: {doc['document_type']} - {doc['description']}")
                    
                except Exception as e:
                    logger.error(f"Error parsing row {row_idx}: {e}")
                    continue
            
            logger.info(f"Successfully extracted {len(documents)} documents")
            self.documents = documents
        
        # Run the crawler
        await crawler.run([url])
        self._set_reference(self.reference)
        
        return self.documents
    
    async def download_documents(self, max_downloads: Optional[int] = None) -> list[dict]:
        """
        Download all documents or a subset.
        
        Args:
            max_downloads: Maximum number of documents to download (None = all)
        
        Returns:
            List of successfully downloaded document records
        """
        if not self.documents:
            await self.fetch_table_data()
        
        if not self.documents:
            logger.error("No documents to download")
            return []
        
        # Skip document types we don't want (e.g. drawings), but keep them in the metadata table
        wanted = [d for d in self.documents if d['document_type'].strip().lower() not in SKIP_DOCUMENT_TYPES]
        skipped = len(self.documents) - len(wanted)
        if skipped:
            logger.info(f"Skipping {skipped} documents of type: {', '.join(sorted(SKIP_DOCUMENT_TYPES))}")
        
        to_download = wanted[:max_downloads] if max_downloads else wanted
        logger.info(f"Starting download of {len(to_download)} documents")
        
        downloaded = []
        
        # The "View" links point directly at the files, but the portal returns 404
        # unless the request carries the session cookie set by the details page.
        details_url = f"{BASE_URL}{APPLICATION_DETAILS_PATH}?activeTab=documents&keyVal={self.key_val}"
        
        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            async with session.get(details_url) as response:
                response.raise_for_status()
            
            for doc in to_download:
                file_path = await self._download_file(session, doc['view_url'], doc['filename'])
            
                if file_path:
                    downloaded.append({
                        'filename': doc['filename'],
                        'file_path': str(file_path),
                        'document_type': doc['document_type'],
                        'view_url': doc['view_url'],
                        'file_url': doc['view_url'],
                        'downloaded_at': datetime.now().isoformat(),
                        'size_bytes': file_path.stat().st_size
                    })
                    logger.info(f"Downloaded: {file_path.name}")
                else:
                    logger.error(f"Failed to download file for {doc['filename']}")
            
                await asyncio.sleep(1)  # Be polite to the portal
        
        # Save metadata
        self._save_metadata(downloaded)
        
        logger.info(f"Successfully downloaded {len(downloaded)} documents")
        return downloaded
    
    def _generate_filename(self, doc_type: str, label: str, date: str) -> str:
        """
        Generate a consistent filename for a document.
        
        Args:
            doc_type: Document type
            label: Drawing number, or the description when there is none
            date: Date published (e.g. '03 Oct 2018')
        
        Returns:
            Filename without extension
        """
        def slug(text: str, max_len: int) -> str:
            # Lowercase, and replace anything that isn't safe in a filename (incl. '/') with '_'
            return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')[:max_len].rstrip('_')
        
        try:
            date_clean = datetime.strptime(date, '%d %b %Y').strftime('%Y-%m-%d')
        except ValueError:
            date_clean = slug(date, 20) or 'nodate'
        
        return f"{date_clean}_{slug(doc_type, 30) or 'document'}_{slug(label, 40) or 'untitled'}"
    
    async def _download_file(self, session, file_url: str, filename: str) -> Optional[Path]:
        """
        Download a file from URL and save it.
        
        Args:
            session: aiohttp session carrying the portal's session cookie
            file_url: URL of the file to download
            filename: Base filename (without extension)
        
        Returns:
            Path to saved file, or None if failed
        """
        try:
            async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=120)) as response:
                if response.status != 200:
                    logger.error(f"Failed to download {file_url}: status {response.status}")
                    return None
                
                # Determine file extension from content-type
                content_type = response.headers.get('content-type', '')
                ext = self._get_extension_from_content_type(content_type, file_url)
                
                # Save file
                file_path = self.app_dir / f"{filename}{ext}"
                
                content = await response.read()
                with open(file_path, 'wb') as f:
                    f.write(content)
                
                return file_path
                
        except asyncio.TimeoutError:
            logger.error(f"Timeout downloading {file_url}")
            return None
        except Exception as e:
            logger.error(f"Error downloading {file_url}: {e}")
            return None
    
    def _get_extension_from_content_type(self, content_type: str, url: str) -> str:
        """
        Determine file extension from content-type header or URL.
        
        Args:
            content_type: Content-Type header value
            url: URL of the file
        
        Returns:
            File extension including dot (e.g., '.pdf')
        """
        # Map content types to extensions
        type_map = {
            'application/pdf': '.pdf',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/rtf': '.rtf',
            'text/rtf': '.rtf',
            'text/plain': '.txt',
            'application/vnd.oasis.opendocument.text': '.odt',
            'application/vnd.ms-excel.sheet.macroenabled.12': '.xlsm',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-excel': '.xls',
            'image/png': '.png',
            'image/jpeg': '.jpg',
            'image/tiff': '.tiff',
        }

        # Try content-type first
        if content_type:
            content_type = content_type.split(';')[0].strip().lower()
            if content_type in type_map:
                return type_map[content_type]

        # Otherwise use the URL's own extension (e.g. '.rtf', '.xlsm')
        suffix = Path(urlparse(url).path).suffix.lower()
        if re.fullmatch(r'\.[a-z0-9]{2,5}', suffix):
            return suffix

        # Default to .pdf (common for planning documents)
        return '.pdf'
    
    def _save_metadata(self, downloaded: list[dict]) -> None:
        """Save metadata about downloads."""
        metadata = {
            'application_key': self.application_key,
            'council': COUNCIL,
            'portal': 'idox',
            'reference': self.reference,
            'portal_key': self.key_val,
            'source_url': f"{BASE_URL}{APPLICATION_DETAILS_PATH}?activeTab=documents&keyVal={self.key_val}",
            'application_summary': self.summary,
            'scraped_at': datetime.now().isoformat(),
            'documents_table': self.documents,
            'downloaded': downloaded,
            'summary': {
                'total_documents_found': len(self.documents),
                'documents_downloaded': len(downloaded),
                'total_size_bytes': sum(d.get('size_bytes', 0) for d in downloaded)
            }
        }
        
        metadata_file = METADATA_DIR / f"{self.application_key}_metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        logger.info(f"Saved metadata to {metadata_file}")
    
    async def run(self, download: bool = True, max_downloads: Optional[int] = None):
        """
        Run the full scraping workflow.
        
        Args:
            download: Whether to download documents (default: True)
            max_downloads: Maximum documents to download
        """
        logger.info(f"Starting scrape for application {self.key_val}")
        
        try:
            # Fetch table
            await self.fetch_table_data()
            
            if not self.documents:
                logger.error("No documents found in table")
                return
            
            # Download documents if requested
            if download:
                await self.download_documents(max_downloads=max_downloads)
            else:
                self._save_metadata([])
            
            logger.info(f"Completed scrape for application {self.reference} (saved as {self.application_key})")
            
        except Exception as e:
            logger.error(f"Scraping failed: {e}", exc_info=True)
            raise


async def main():
    """Example usage."""
    # Example application key from your HTML
    application_key = "P7SRNYBJ0GJ00"
    
    scraper = PlanningDocumentScraper(application_key)
    
    # Run the full scrape with download
    # max_downloads=2 means only download first 2 documents (for testing)
    await scraper.run(download=True, max_downloads=None)


if __name__ == "__main__":
    asyncio.run(main())
