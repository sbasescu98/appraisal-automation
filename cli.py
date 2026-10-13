"""
Command-line interface for the planning document scraper.
"""

import asyncio
import re
import sys
from pathlib import Path

import aiohttp
import click

from arcus_scraper import SKIP_CATEGORIES, ArcusError, ArcusScraper, is_arcus_url
from planning_spider import BASE_URL, PlanningDocumentScraper


class DefaultScrapeGroup(click.Group):
    """Run `scrape` when options are passed without a subcommand (e.g. cli.py -k KEY)."""

    def parse_args(self, ctx, args):
        if args and args[0].startswith('-') and args[0] not in ('--help', '-h'):
            args = ['scrape'] + args
        return super().parse_args(ctx, args)


@click.group(cls=DefaultScrapeGroup)
def cli():
    """Planning document scraping toolkit."""
    pass


@cli.command()
@click.option(
    '--application-key',
    '-k',
    help='East Riding application key (e.g., P7SRNYBJ0GJ00)',
)
@click.option(
    '--url',
    '-u',
    help='Application page URL (East Riding, or an Arcus register such as Wiltshire)',
)
@click.option(
    '--fetch-only',
    is_flag=True,
    default=False,
    help='Only fetch table, do not download documents',
)
@click.option(
    '--max-downloads',
    '-m',
    type=int,
    default=None,
    help='Maximum number of documents to download (for testing)',
)
@click.option(
    '--only',
    type=click.Choice(['comments', 'documents']),
    help='Arcus registers: save only the comments or only the documents  [default: both]',
)
@click.option(
    '--all-categories',
    is_flag=True,
    default=False,
    help=f"Arcus registers: also download the categories skipped by default ({', '.join(sorted(SKIP_CATEGORIES))})",
)
@click.option(
    '--output-dir',
    '-o',
    type=Path,
    default=Path('data/documents'),
    help='Output directory for downloaded documents',
)
def scrape(application_key: str, url: str, fetch_only: bool, max_downloads: int, only: str,
           all_categories: bool, output_dir: Path):
    """
    Scrape planning documents from East Riding, or comments from an Arcus register (e.g. Wiltshire).
    
    Example:
        python cli.py --application-key P7SRNYBJ0GJ00
        python cli.py -k P7SRNYBJ0GJ00 --max-downloads 2 --fetch-only
        python cli.py --url "https://development.wiltshire.gov.uk/pr3/s/planning-application/a0iQ300000FJY0XIAX/pl202505830"
    """
    if bool(application_key) == bool(url):
        raise click.UsageError('Give either --application-key or --url.')
    
    if url and is_arcus_url(url):
        _scrape_arcus(url, fetch_only, max_downloads, only, all_categories)
        return
    if url:
        # East Riding (Idox) URLs carry the key as keyVal=...
        key_match = re.search(r'[?&]keyVal=([A-Za-z0-9]+)', url)
        if not url.startswith(BASE_URL) or not key_match:
            raise click.UsageError(
                'Unsupported portal URL. Supported: East Riding (…applicationDetails.do?…keyVal=KEY) '
                'and Arcus registers (…/s/planning-application/RECORD_ID/…).'
            )
        application_key = key_match.group(1)
    
    click.echo(f"🚀 Starting scrape for application {application_key}")
    
    scraper = PlanningDocumentScraper(application_key)
    
    try:
        asyncio.run(scraper.run(
            download=not fetch_only,
            max_downloads=max_downloads
        ))
        
        click.echo(f"\n✅ Scrape complete: {scraper.reference}")
        click.echo(f"📁 Documents saved to: {scraper.app_dir}")
        
        # Summary
        if scraper.documents:
            click.echo(f"\n📊 Summary:")
            click.echo(f"   Total documents found: {len(scraper.documents)}")
            if not fetch_only:
                click.echo(f"   (See metadata file for download details)")
        if scraper.application_key:
            click.echo(f"\nNext: python extractor_cli.py extract {scraper.application_key}")
        
    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        sys.exit(1)


def _scrape_arcus(url: str, fetch_only: bool, max_downloads: int, only: str, all_categories: bool):
    """Scrape the comments and documents of an Arcus public register application."""
    scraper = ArcusScraper(url, skip_categories=set() if all_categories else None)
    click.echo(f"🚀 Fetching application from {scraper.council.title()} (record {scraper.record_id})")
    
    try:
        asyncio.run(scraper.run(download=not fetch_only, max_downloads=max_downloads,
                                comments=only != 'documents', documents=only != 'comments'))
    except (ArcusError, aiohttp.ClientError) as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        sys.exit(1)
    
    details = scraper.details
    click.echo(f"\n✅ {scraper.reference}: {details.get('Site Address', '')}")
    if details.get('Issued Decision'):
        click.echo(f"   Decision: {details['Issued Decision']} ({details.get('Decision Date') or 'no date'})")
    
    def summary(rows, key):
        counts = {}
        for row in rows:
            counts[row[key] or 'Uncategorised'] = counts.get(row[key] or 'Uncategorised', 0) + 1
        return ', '.join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda x: -x[1]))
    
    click.echo(f"\n📊 {len(scraper.comments)} comments: {summary(scraper.comments, 'stance')}")
    click.echo(f"📊 {len(scraper.files)} documents: {summary(scraper.files, 'category')}")
    if not fetch_only:
        documents = [d for d in scraper.downloaded if d['document_type'] != 'Comment']
        failed = [f for f in scraper.files if f.get('download_error')]
        if only != 'documents':
            click.echo(f"📁 {len(scraper.comments)} comments saved to: {scraper.app_dir}")
        if only != 'comments':
            skipped = len(scraper.files) - len(scraper.files_to_download())
            click.echo(f"📁 {len(documents)} documents downloaded ({sum(d['size_bytes'] for d in documents) / 1e6:.0f} MB)"
                       + (f", {skipped} skipped by category" if skipped else '')
                       + (f", {len(failed)} failed" if failed else ''))
            for f in failed:
                click.echo(f"   ⚠️  {f['title']}: {f['download_error']}")
    click.echo(f"\nNext: python extractor_cli.py extract {scraper.application_key}")


@cli.command()
@click.argument('application_key')
def list_documents(application_key: str):
    """List documents for an application (from cached metadata)."""
    from planning_spider import METADATA_DIR
    
    metadata_file = METADATA_DIR / f"{application_key}_metadata.json"
    
    if not metadata_file.exists():
        click.echo(f"No metadata found for {application_key}", err=True)
        sys.exit(1)
    
    import json
    with open(metadata_file) as f:
        metadata = json.load(f)
    
    docs = metadata.get('documents_table', [])
    click.echo(f"\nDocuments for {application_key}:\n")
    
    for i, doc in enumerate(docs, 1):
        click.echo(f"{i}. {doc['document_type']} - {doc['description']}")
        click.echo(f"   Date: {doc['date_published']}")
        if doc.get('drawing_number'):
            click.echo(f"   Drawing: {doc['drawing_number']}")
        click.echo(f"   File: {doc['filename']}\n")


@cli.command()
@click.argument('application_key')
def show_metadata(application_key: str):
    """Show full metadata for an application."""
    from planning_spider import METADATA_DIR
    import json
    
    metadata_file = METADATA_DIR / f"{application_key}_metadata.json"
    
    if not metadata_file.exists():
        click.echo(f"No metadata found for {application_key}", err=True)
        sys.exit(1)
    
    with open(metadata_file) as f:
        metadata = json.load(f)
    
    click.echo(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    cli()
