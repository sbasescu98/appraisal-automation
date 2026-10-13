"""
Command-line interface for Phase 2a document extraction.
"""

import asyncio
import sys

import click

from document_detector import DEFAULT_SUPPORTED_TYPES, TYPE_MAP, detect_file_type
from pdf_extractor import DocumentExtractor, _classify_pages, _extract_pdf_pages, ocr_available


@click.group()
def cli():
    """Planning document extraction toolkit."""
    pass


@cli.command()
@click.argument('application_key')
@click.option(
    '--types',
    '-t',
    default=','.join(DEFAULT_SUPPORTED_TYPES),
    show_default=True,
    help="Comma-separated file types to extract (add 'doc' for legacy Word files)",
)
def extract(application_key: str, types: str):
    """Extract and chunk all downloaded documents for an application."""
    supported_types = [t.strip().lower() for t in types.split(',') if t.strip()]
    unknown = set(supported_types) - set(TYPE_MAP.values())
    if unknown:
        click.echo(f"❌ Unknown file types: {', '.join(sorted(unknown))}", err=True)
        sys.exit(1)

    try:
        extractor = DocumentExtractor(application_key, supported_types=supported_types)
        summary = asyncio.run(extractor.extract_all_documents())
    except FileNotFoundError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)

    overall = summary['overall']
    click.echo(f"\n✅ Extracted {overall['successful']}/{summary['total_documents']} documents")
    for file_type, stats in summary['by_type'].items():
        line = f"  - {file_type.upper() + ':':<5} {stats['successful']} success"
        if stats['failed']:
            line += f", {stats['failed']} failed"
        click.echo(line)
    if overall['skipped']:
        click.echo(f"  - Skipped: {overall['skipped']}")
    click.echo(f"📊 Total tokens: {overall['total_tokens']:,}")
    click.echo(f"📁 Files saved to: {extractor.extract_dir}/")

    failed = [f for f in summary['files'] if f['status'] == 'error']
    if failed:
        click.echo("\n⚠️  Failed:")
        for f in failed:
            click.echo(f"  {f['filename']}: {f['error']}")


@cli.command()
@click.argument('application_key')
def compile(application_key: str):
    """Combine existing extractions into all_documents.json and all_documents.txt."""
    try:
        json_file, txt_file = DocumentExtractor(application_key).compile_extractions()
    except FileNotFoundError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)

    click.echo(f"✅ Compiled documents into:\n   {json_file}\n   {txt_file}")


@cli.command()
@click.argument('application_key')
def analyze(application_key: str):
    """Show the file type of each downloaded document (and whether PDFs are scanned)."""
    extractor = DocumentExtractor(application_key)

    try:
        files = extractor.list_documents()
    except FileNotFoundError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(1)

    if not files:
        click.echo(f"No documents found in {extractor.docs_dir}")
        return

    width = max(len(p.name) for p in files)
    click.echo("File Type Distribution:")
    for file_path in files:
        file_type = detect_file_type(file_path)
        label = file_type.upper()
        if file_type == 'pdf':
            try:
                pages = _extract_pdf_pages(file_path)
                label += f" ({_classify_pages(pages)}, {len(pages)} pages)"
            except Exception as e:
                label += f" (unreadable: {e})"
        click.echo(f"  {file_path.name:<{width}}  → {label}")

    if not ocr_available():
        click.echo("\nℹ️  OCR not available: scanned pages will be skipped (brew install tesseract poppler)")


if __name__ == '__main__':
    cli()
