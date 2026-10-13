"""
Command-line interface for the web search crawler.
"""

import asyncio
import json
import sys
from pathlib import Path

import click

from google_search_crawler import (
    API_KEY_ENV, DEFAULT_COUNTRY, DEFAULT_DELAY, DEFAULT_LANGUAGE, DEFAULT_MAX_RESULTS,
    DEFAULT_MAX_RETRIES, METADATA_DIR, SearchConfig, SearchError, WebSearchCrawler,
)


class DefaultSearchGroup(click.Group):
    """Run `search` unless a subcommand is named (e.g. search_cli.py "round river solar" ...)."""

    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and args[0] not in ('--help', '-h'):
            args = ['search'] + args
        return super().parse_args(ctx, args)


@click.group(cls=DefaultSearchGroup)
def cli():
    """Web search toolkit: run Google searches and extract the text of the results."""
    pass


def load_config(config_file, **cli_values) -> SearchConfig:
    """
    Merge a JSON config file with command-line options (the command line wins).

    Config keys: queries, project, max_results, delay, max_retries, country, language,
    require_project_name, extra_blocked_domains.
    """
    values = {}
    if config_file:
        with open(config_file, encoding='utf-8') as f:
            values = json.load(f)
        if isinstance(values.get('queries'), str):
            values['queries'] = [values['queries']]
    values.update({k: v for k, v in cli_values.items() if v is not None})

    if not values.get('queries'):
        raise click.UsageError('Give at least one search, e.g. search_cli.py "round river solar miami county" '
                               '(or "queries" in the config file).')
    if values.get('require_project_name') and not values.get('project'):
        raise click.UsageError('--require-project-name needs --project: the name to look for in each page.')

    known = SearchConfig.__dataclass_fields__
    unknown = set(values) - set(known)
    if unknown:
        raise click.UsageError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    return SearchConfig(**values)


@cli.command()
@click.argument('queries', nargs=-1)
@click.option('--project', '-p', help='Project name to check each result for, e.g. "Round River Solar" (optional; '
                                      'results are labelled "mentions project" or not)')
@click.option('--config', '-c', 'config_file', type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help='JSON config file (command-line options override it)')
@click.option('--max-results', '-m', type=int, help=f'Max results per search  [default: {DEFAULT_MAX_RESULTS}]')
@click.option('--delay', type=float, help=f'Seconds between requests  [default: {DEFAULT_DELAY}]')
@click.option('--max-retries', type=int, help=f'Retries on 429/5xx/timeouts/CAPTCHAs  [default: {DEFAULT_MAX_RETRIES}]')
@click.option('--country', help=f'Google country code, e.g. uk or us  [default: {DEFAULT_COUNTRY}]')
@click.option('--language', help=f'Google interface language  [default: {DEFAULT_LANGUAGE}]')
@click.option('--require-project-name/--keep-all', 'require_project_name', default=None,
              help='Keep only pages whose text contains the --project name  [default: keep all]')
@click.option('--application', '-a', 'application',
              help='Key of the application this search is for, e.g. wiltshire_pl_2025_05830; '
                   'results go to <application>_search')
@click.option('--key', '-k', help='Storage key, if not using --application  [default: search_<project or search>]')
@click.option('--fresh', is_flag=True, help="Discard this key's earlier results and start over")
@click.option('--dry-run', is_flag=True, help='Print the searches and API requests without running them')
@click.option('--search-only', is_flag=True, help='Run the searches and save the results table, but download nothing')
@click.option('--no-extract', is_flag=True, help='Download pages but skip text extraction')
def search(queries, project, config_file, max_results, delay, max_retries, country, language,
           require_project_name, application, key, fresh, dry_run, search_only, no_extract):
    """
    Run each QUERY as a Google search (via SerpAPI), exactly as written, and extract the text of every result.

    Example:
        python search_cli.py "round river solar miami county indiana" --country us -k miamicounty_round_river_solar_search
        python search_cli.py '"Round River Solar" lawsuit OR zoning' -p "Round River Solar" --country us --dry-run
        python search_cli.py -a wiltshire_pl_2025_05830 "flaxlands solar farm" "PL/2025/05830" -p "Flaxlands"

    Results build up under one key: each run adds its new results to those already there.
    """
    if application and key:
        raise click.UsageError('Give either --application or --key, not both.')
    if application:
        key = f"{application}_search"
        if not (METADATA_DIR / f"{application}_metadata.json").exists():
            click.echo(f"⚠️  No scraped application '{application}' found in {METADATA_DIR}/; "
                       f"saving under '{key}' anyway.", err=True)

    config = load_config(
        config_file, queries=list(queries) or None, project=project, max_results=max_results,
        delay=delay, max_retries=max_retries, country=country, language=language,
        require_project_name=require_project_name,
    )
    crawler = WebSearchCrawler(config, key=key, fresh=fresh)

    if dry_run:
        plan = crawler.plan()
        click.echo(f"🔎 Dry run: {len(plan)} {'search' if len(plan) == 1 else 'searches'}, "
                   f"up to {config.max_results} results each\n")
        for i, step in enumerate(plan, 1):
            click.echo(f"{i}. {step['query']}")
            click.echo(f"   GET {step['request_url']}")
        if config.project:
            click.echo(f"\nResults will be checked for \"{config.project}\".")
        click.echo(f"\nResults would be {'saved' if fresh or not crawler.results else 'added'} under key '{crawler.key}':")
        click.echo(f"   {crawler.metadata_file}")
        click.echo(f"   {crawler.docs_dir}/")
        if not crawler.provider.api_key:
            click.echo(f"\n⚠️  {API_KEY_ENV} is not set; add it to .env before a real run.")
        return

    click.echo(f"🚀 Running {len(crawler.queries)} {'search' if len(crawler.queries) == 1 else 'searches'}")
    try:
        outcome = asyncio.run(crawler.run(fetch=not search_only, extract=not no_extract))
    except SearchError as e:
        click.echo(f"\n❌ {e}", err=True)
        sys.exit(1)

    click.echo(f"\n✅ {len(crawler.new_results)} new results, {len(crawler.new_downloads)} downloaded "
               f"({len(crawler.results)} results in total under '{crawler.key}')")
    click.echo(f"📁 Metadata: {crawler.metadata_file}")
    if outcome['extraction']:
        overall = outcome['extraction']['overall']
        counts = outcome['project_filter']
        click.echo(f"📄 Extracted {overall['successful']} pages, {overall['total_tokens']:,} tokens")
        if counts['mention'] or counts['no_mention']:
            click.echo(f"   {counts['mention']} mention the project name, {counts['no_mention']} don't"
                       + (f" ({counts['filtered']} filtered out)" if counts['filtered'] else ''))
        click.echo(f"📁 Text: {outcome['extract_dir']}/all_documents.txt")
    click.echo(f"\nSee the results: python search_cli.py list-results {crawler.key}")


@cli.command()
@click.argument('key')
@click.option('--skipped', is_flag=True, help='Also list results that were skipped, and why')
def list_results(key: str, skipped: bool):
    """List the search results saved for a key (rank, query, title, URL)."""
    metadata_file = METADATA_DIR / f"{key}_metadata.json"
    if not metadata_file.exists():
        click.echo(f"No metadata found for {key}", err=True)
        sys.exit(1)

    with open(metadata_file, encoding='utf-8') as f:
        metadata = json.load(f)

    searches = list(dict.fromkeys(s['query'] for s in metadata.get('searches', [])))
    click.echo(f"\nResults for {len(searches)} {'search' if len(searches) == 1 else 'searches'} "
               f"(last run {metadata['scraped_at'][:16]}):")
    for q in searches:
        click.echo(f"   {q}")
    click.echo('')
    for i, row in enumerate(metadata['documents_table'], 1):
        if row.get('download_error'):
            status = f"not downloaded: {row['download_error']}"
        elif 'mentions_project' in row:
            status = 'mentions project' if row['mentions_project'] else 'does not mention project'
        else:
            status = 'downloaded' if row.get('scraped_at') else 'not downloaded'
        click.echo(f"{i}. {row['title']}")
        click.echo(f"   {row['source_url']}")
        click.echo(f"   Query: {row['query']} | Rank: {row['search_rank']} | {status}")
        click.echo(f"   File: {row['filename']}\n")

    if skipped and metadata.get('skipped'):
        click.echo("Skipped:")
        for s in metadata['skipped']:
            click.echo(f"  [{s['reason']}] {s['source_url']}  (query: {s['query']}, rank {s['search_rank']})")
            if s.get('reason') == 'blocked site' and (s.get('title') or s.get('snippet')):
                click.echo(f"      {s.get('title', '')}")
                if s.get('snippet'):
                    click.echo(f"      \"{s['snippet']}\"")


if __name__ == '__main__':
    cli()
