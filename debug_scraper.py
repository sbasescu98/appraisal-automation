"""
Debug script to inspect the planning portal structure.
Useful for understanding HTML layout and debugging scraping issues.
"""

import asyncio
import logging
from urllib.parse import urljoin

from crawlee import Request
from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = "https://newplanningaccess.eastriding.gov.uk"
APPLICATION_DETAILS_PATH = "/newplanningaccess/applicationDetails.do"


async def inspect_page(application_key: str = "P7SRNYBJ0GJ00"):
    """
    Fetch and inspect the application details page.
    Prints HTML structure to help debug scraping.
    """
    url = f"{BASE_URL}{APPLICATION_DETAILS_PATH}?activeTab=documents&keyVal={application_key}"
    
    logger.info(f"Inspecting: {url}")
    
    crawler = BeautifulSoupCrawler(parser='html.parser', max_requests_per_crawl=1)
    
    @crawler.router.default_handler
    async def handle_page(context: BeautifulSoupCrawlingContext) -> None:
        """Inspect the page."""
        soup = context.soup
        html = str(soup)
        
        # Print page title
        title = soup.find('title')
        if title:
            print(f"\n📄 Page Title: {title.string}\n")
        
        # Find all tables
        tables = soup.find_all('table')
        print(f"Found {len(tables)} tables on page\n")
        
        for table_idx, table in enumerate(tables):
            print(f"\n{'='*80}")
            print(f"TABLE {table_idx}")
            print(f"{'='*80}")
            
            # Check for ID or class
            table_id = table.get('id')
            table_class = table.get('class')
            print(f"ID: {table_id}")
            print(f"Class: {table_class}\n")
            
            # Show first few rows
            rows = table.find_all('tr')
            print(f"Total rows: {len(rows)}\n")
            
            # Show header
            header_row = rows[0] if rows else None
            if header_row:
                headers = header_row.find_all(['th', 'td'])
                print("HEADER:")
                for col_idx, header in enumerate(headers):
                    print(f"  Column {col_idx}: {header.get_text(strip=True)}")
            
            # Show first data row
            if len(rows) > 1:
                print("\nFIRST DATA ROW:")
                data_row = rows[1]
                cells = data_row.find_all(['td', 'th'])
                for cell_idx, cell in enumerate(cells):
                    cell_text = cell.get_text(strip=True)[:100]  # First 100 chars
                    
                    # Check for links
                    links = cell.find_all('a')
                    link_info = ""
                    if links:
                        for link in links:
                            href = link.get('href')
                            link_info += f"\n    → Link: {href}"
                    
                    print(f"  Cell {cell_idx}: {cell_text}{link_info}")
        
        # Look for forms or other interactive elements
        print(f"\n{'='*80}")
        print("FORMS & INTERACTIVE ELEMENTS")
        print(f"{'='*80}")
        
        forms = soup.find_all('form')
        print(f"Forms found: {len(forms)}")
        for form in forms:
            print(f"  - Method: {form.get('method')}, Action: {form.get('action')}")
        
        # Look for AJAX/JavaScript hints
        scripts = soup.find_all('script')
        print(f"\nScripts found: {len(scripts)}")
        
        # Check for any data attributes that might indicate AJAX loading
        ajax_elements = soup.find_all(attrs={'data-url': True})
        if ajax_elements:
            print(f"Elements with data-url attribute: {len(ajax_elements)}")
            for elem in ajax_elements[:3]:
                print(f"  - {elem.get('data-url')}")
        
        # Save HTML to file for manual inspection
        debug_html = f"debug_{application_key}.html"
        with open(debug_html, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"\n✅ Full HTML saved to: {debug_html}")
    
    await crawler.run([Request.from_url(url, always_enqueue=True)])


async def test_download_link(application_key: str = "P7SRNYBJ0GJ00"):
    """
    Test what happens when we follow a document view link.
    """
    # First, get the documents table
    url = f"{BASE_URL}{APPLICATION_DETAILS_PATH}?activeTab=documents&keyVal={application_key}"
    
    crawler = BeautifulSoupCrawler(parser='html.parser', max_requests_per_crawl=1)
    
    view_links = []
    
    @crawler.router.default_handler
    async def collect_links(context: BeautifulSoupCrawlingContext) -> None:
        """Collect view links from the table."""
        soup = context.soup
        table = soup.find('table')
        
        if not table:
            return
        
        rows = table.find_all('tr')[1:]  # Skip header
        
        for row in rows[:3]:  # First 3 rows
            cells = row.find_all('td')
            if len(cells) >= 7:
                view_link = cells[6].find('a')
                if view_link and view_link.get('href'):
                    view_links.append({
                        'description': cells[5].get_text(strip=True),
                        'href': view_link.get('href')
                    })
    
    await crawler.run([Request.from_url(url, always_enqueue=True)])
    
    if not view_links:
        print("No view links found!")
        return
    
    print(f"Found {len(view_links)} view links, testing first one...\n")
    
    # Now test following the first link
    test_link = view_links[0]
    test_url = urljoin(BASE_URL, test_link['href'])
    
    print(f"📄 Document: {test_link['description']}")
    print(f"🔗 URL: {test_url}\n")
    
    # Files only download when the session cookie from the details page is sent
    import aiohttp
    async with aiohttp.ClientSession(headers={'User-Agent': 'Mozilla/5.0'}) as session:
        async with session.get(url) as response:
            await response.read()
        async with session.get(test_url, allow_redirects=False) as response:
            print(f"Status: {response.status}")
            print(f"Content-Type: {response.headers.get('content-type')}\n")
            
            if response.status in [301, 302, 303, 307, 308]:
                print(f"→ Redirect to: {response.headers.get('location')}")
            elif 'text' in response.headers.get('content-type', ''):
                print("RESPONSE PREVIEW:")
                print((await response.text())[:2000])
                print("\n... (truncated)")
            else:
                print(f"Binary file, {len(await response.read())} bytes")


async def main():
    """Run debug commands."""
    import sys
    
    if len(sys.argv) > 1:
        application_key = sys.argv[1]
    else:
        application_key = "P7SRNYBJ0GJ00"
    
    print("="*80)
    print("PLANNING PORTAL DEBUG TOOL")
    print("="*80)
    print(f"\nApplication Key: {application_key}\n")
    
    print("1️⃣  Inspecting page structure...\n")
    await inspect_page(application_key)
    
    print("\n\n2️⃣  Testing document view links...\n")
    await test_download_link(application_key)


if __name__ == "__main__":
    asyncio.run(main())
