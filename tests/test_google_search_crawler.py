"""
Tests for the web search crawler. No network or API key needed: SerpAPI is replaced by a
fake provider and result pages are served from a local aiohttp server.

Run: pytest
"""

import asyncio
import json

import pytest
from aiohttp import web
from click.testing import CliRunner

import google_search_crawler as gsc
import pdf_extractor
import search_cli
from google_search_crawler import (
    RetryableError, SearchConfig, SerpApiProvider, WebSearchCrawler,
    mentions_project, noise_reason, normalize_url, page_block_reason, with_retries,
)

PROJECT = "Example Solar Farm"

ARTICLE_HTML = """<html><head><title>Council approves Example Solar Farm</title></head><body>
<nav>Home | News | Contact</nav>
<article><h1>Council approves Example Solar Farm</h1>
<p>The planning committee approved the Example Solar Farm application on Tuesday despite
objections from residents about the loss of farmland and the impact on views.</p>
<p>The 49 MW scheme will cover 80 hectares and is expected to be connected to the grid in 2028,
according to the developer's planning statement submitted to the council last year.</p>
</article><footer>Copyright 2026</footer></body></html>"""

OTHER_HTML = """<html><head><title>Solar news roundup</title></head><body><article>
<h1>Solar news roundup</h1>
<p>Several new solar schemes were submitted for planning across the region this month, with
councils reporting a rise in applications for battery storage alongside solar panels.</p>
<p>Campaigners say cumulative impact should be assessed more carefully by planning officers.</p>
</article></body></html>"""

LOGIN_HTML = "<html><body><h1>Sign in to continue</h1><form>...</form></body></html>"


# ---------------------------------------------------------------------------
# Queries & URLs
# ---------------------------------------------------------------------------

def test_normalize_url_ignores_tracking_fragment_www_and_trailing_slash():
    a = normalize_url("https://www.example.com/news/solar/?utm_source=x&id=3#comments")
    b = normalize_url("https://example.com/news/solar?id=3")
    assert a == b


def test_normalize_url_keeps_meaningful_params():
    assert normalize_url("https://example.com/a?id=1") != normalize_url("https://example.com/a?id=2")


@pytest.mark.parametrize("url, reason", [
    ("https://www.google.com/maps/place/x", "Google page"),
    ("https://maps.google.co.uk/x", "Google page"),
    ("https://www.googleadservices.com/pagead/aclk?x", "ad"),
    ("https://www.facebook.com/groups/residents", "blocked site"),
    ("https://uk.linkedin.com/posts/x", "blocked site"),
    ("https://x.com/council/status/1", "blocked site"),
    ("https://www.youtube.com/watch?v=2Qrf-OKI044", "blocked site"),
    ("https://www.bbc.co.uk/news/uk-england-1", None),
    ("https://www.eastriding.gov.uk/planning", None),
    ("https://box.com/file", None),  # must not match the blocked 'x.com' as a suffix
    # Council planning registers are skipped (Phase 1 collects them); council news is kept
    ("https://development.wiltshire.gov.uk/pr/s/web-comment/a07Q3000011U94aIAC/julia-humes-comment",
     "council planning register"),
    ("https://development.wiltshire.gov.uk/pr3/s/planning-application/a0iQ300000FJY0XIAX/pl202505830",
     "council planning register"),
    ("https://publicaccess.leeds.gov.uk/online-applications/applicationDetails.do?keyVal=ABC",
     "council planning register"),
    ("https://newplanningaccess.eastriding.gov.uk/newplanningaccess/applicationDetails.do?keyVal=X",
     "council planning register"),
    ("https://www.wiltshire.gov.uk/news/solar-farm-decision", None),
    ("https://www.eastriding.gov.uk/council/press-releases/battery-storage", None),
])
def test_noise_reason(url, reason):
    assert noise_reason(url) == reason


def test_page_block_reason():
    assert page_block_reason('<div class="cf-chl-widget">Verify you are human</div>') == 'CAPTCHA / bot check'
    assert page_block_reason(LOGIN_HTML) == 'login wall'
    assert page_block_reason(ARTICLE_HTML) is None


def test_page_block_reason_detects_salesforce_shell():
    shell = ('<html><link href="/pr/s/sfsites/runtimedownload/fonts.css"><div class="siteforceContentArea">'
             'Loading</div><div>Sorry to interrupt</div></html>')
    assert page_block_reason(shell) == 'JavaScript-only page'
    # An ordinary article that merely mentions one of the markers is not a shell
    assert page_block_reason(ARTICLE_HTML.replace('Tuesday', 'Tuesday (see siteforce docs)')) is None


def test_extract_html_rejects_loading_screens(tmp_path):
    page = tmp_path / 'shell.html'
    page.write_text('<html><body><p>Loading</p><p>Sorry to interrupt</p><p>CSS Error</p></body></html>')
    result = asyncio.run(pdf_extractor.extract_html(page))
    assert result['status'] == 'error' and 'No main text' in result['error']

    page.write_text(ARTICLE_HTML)
    assert asyncio.run(pdf_extractor.extract_html(page))['status'] == 'success'


def test_mentions_project_ignores_case_and_line_breaks():
    assert mentions_project("plans for the example solar\nfarm were approved", PROJECT)
    assert not mentions_project("plans for a solar farm were approved", PROJECT)
    assert not mentions_project(None, PROJECT)


# ---------------------------------------------------------------------------
# SerpAPI provider
# ---------------------------------------------------------------------------

SERPAPI_RESPONSE = {
    "search_metadata": {"id": "abc123", "google_url": "https://www.google.co.uk/search?q=x"},
    "ads": [{"link": "https://ad.example.com"}],
    "organic_results": [
        {"position": 1, "title": "Council approves", "link": "https://news.example.com/a",
         "snippet": "The planning committee...", "displayed_link": "news.example.com", "date": "3 days ago"},
        {"position": 2, "title": "No link"},
    ],
}


def test_parse_results_uses_organic_results_only():
    results = SerpApiProvider.parse_results(SERPAPI_RESPONSE)
    assert results == [{
        'search_rank': 1, 'source_url': 'https://news.example.com/a', 'title': 'Council approves',
        'snippet': 'The planning committee...', 'displayed_link': 'news.example.com',
        'result_date': '3 days ago',
    }]


def test_request_url_masks_api_key():
    url = SerpApiProvider("secret-key").request_url('"Example Solar Farm" objection')
    assert 'secret-key' not in url
    assert 'engine=google' in url and 'gl=uk' in url and 'api_key=%2A%2A%2A' in url


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def test_with_retries_backs_off_then_succeeds(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(gsc.asyncio, 'sleep', fake_sleep)

    calls = {'n': 0}

    async def flaky():
        calls['n'] += 1
        if calls['n'] < 3:
            raise RetryableError("status 429", retry_after=None)
        return 'ok'

    assert asyncio.run(with_retries(flaky, max_retries=3, base_delay=2, description='test')) == 'ok'
    assert len(sleeps) == 2
    assert 2 <= sleeps[0] < 3 and 4 <= sleeps[1] < 5  # 2s, then 4s, plus jitter


def test_with_retries_honours_retry_after_and_gives_up(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(gsc.asyncio, 'sleep', fake_sleep)

    async def always_429():
        raise RetryableError("status 429", retry_after=30)

    with pytest.raises(RetryableError):
        asyncio.run(with_retries(always_429, max_retries=2, base_delay=1, description='test'))
    assert sleeps == [30, 30]


# ---------------------------------------------------------------------------
# End to end (fake search provider + local web server)
# ---------------------------------------------------------------------------

class FakeProvider:
    """Stands in for SerpAPI: returns canned results per query."""
    name = 'fake'
    api_key = 'test'

    def __init__(self, results_by_query):
        self.results_by_query = results_by_query

    def request_url(self, query, start=0):
        return f"fake://{query}"

    async def search(self, session, query, max_results, max_retries, delay):
        results = [
            {'search_rank': i, 'source_url': url, 'title': title, 'snippet': f'snippet {i}',
             'displayed_link': '', 'result_date': '', 'search_id': f'id-{query}'}
            for i, (url, title) in enumerate(self.results_by_query.get(query, []), 1)
        ]
        return results[:max_results], [{'search_id': f'id-{query}', 'start': 0}]


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    """Point every module's data folders at a temporary directory."""
    dirs = {
        'DOCUMENTS_DIR': tmp_path / 'documents',
        'METADATA_DIR': tmp_path / 'metadata',
        'EXTRACTED_DIR': tmp_path / 'extracted',
    }
    for name, path in dirs.items():
        for module in (gsc, pdf_extractor, search_cli):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, path)
    return dirs


async def _run_against_local_server(config, results_by_query_template, key=None, fresh=False):
    """Serve test pages locally, then run the crawler with result URLs pointing at them."""
    def page(html=None, status=200):
        async def handler(request):
            return web.Response(text=html, content_type='text/html', status=status)
        return handler

    app = web.Application()
    app.router.add_get('/article', page(ARTICLE_HTML))
    app.router.add_get('/other', page(OTHER_HTML))
    app.router.add_get('/login', page(LOGIN_HTML))
    app.router.add_get('/missing', page(status=404))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        results = {q: [(u.format(base=base), t) for u, t in rows] for q, rows in results_by_query_template.items()}
        crawler = WebSearchCrawler(config, key=key, fresh=fresh, provider=FakeProvider(results))
        outcome = await crawler.run()
        return crawler, outcome, base
    finally:
        await runner.cleanup()


def _example_run(data_dirs, require_project_name):
    q1, q2 = '"Example Solar Farm" objection', 'example solar farm approved'
    config = SearchConfig(queries=[q1, q2], project=PROJECT, delay=0, max_retries=0,
                          require_project_name=require_project_name)
    return asyncio.run(_run_against_local_server(config, {
        q1: [
            ('{base}/article?utm_source=google', 'Council approves Example Solar Farm'),
            ('https://www.facebook.com/groups/residents', 'Residents group'),
            ('{base}/login', 'Members area'),
        ],
        q2: [
            ('{base}/article', 'Council approves Example Solar Farm'),  # duplicate of q1's first result
            ('{base}/other', 'Solar news roundup'),
            ('{base}/missing', 'Gone'),
        ],
    })), q1, q2


def test_end_to_end_search_download_extract(data_dirs):
    (crawler, outcome, base), q1, q2 = _example_run(data_dirs, require_project_name=False)

    # Noise, duplicates, login walls and dead links are skipped, with reasons
    reasons = {s['source_url'].replace(base, ''): s['reason'] for s in crawler.skipped}
    assert reasons == {
        'https://www.facebook.com/groups/residents': 'blocked site',
        '/article': 'duplicate',
        '/login': 'login wall',
        '/missing': 'status 404',
    }
    assert [r['title'] for r in crawler.results] == [
        'Council approves Example Solar Farm', 'Members area', 'Solar news roundup', 'Gone',
    ]
    article = crawler.results[0]
    assert article['queries'] == [q1, q2]

    # Skipped results keep Google's title and snippet (the only text available for social posts)
    facebook = next(s for s in crawler.skipped if 'facebook' in s['source_url'])
    assert facebook['title'] == 'Residents group' and facebook['snippet'] == 'snippet 2'

    # Metadata keeps everything needed to retrace a result
    metadata = json.loads((data_dirs['METADATA_DIR'] / f'{crawler.key}_metadata.json').read_text())
    assert metadata['projects'] == [PROJECT]
    assert [s['query'] for s in metadata['searches']] == [q1, q2]
    assert metadata['searches'][0]['project_name'] == PROJECT
    row = metadata['documents_table'][0]
    for key in ('source_url', 'query', 'search_rank', 'title', 'snippet', 'search_id', 'searched_at', 'scraped_at',
                'project_name'):
        assert row[key], key

    # Both downloaded pages are extracted; the project filter only labels them
    combined = json.loads((data_dirs['EXTRACTED_DIR'] / crawler.key / 'all_documents.json').read_text())
    docs = {d['title']: d for d in combined['documents']}
    assert set(docs) == {'Council approves Example Solar Farm', 'Solar news roundup'}
    doc = docs['Council approves Example Solar Farm']
    assert doc['file_type'] == 'html'
    assert doc['source_url'] == f'{base}/article?utm_source=google'
    assert doc['query'] == q1 and doc['search_rank'] == 1 and doc['scraped_at']
    assert doc['mentions_project'] is True
    assert 'planning committee approved' in doc['text']
    assert 'Home | News' not in doc['text']  # navigation stripped
    assert docs['Solar news roundup']['mentions_project'] is False
    assert outcome['project_filter'] == {'mention': 1, 'no_mention': 1, 'filtered': 0}

    txt = (data_dirs['EXTRACTED_DIR'] / crawler.key / 'all_documents.txt').read_text()
    assert f'Query: {q1} | Rank: 1' in txt and f'Source: {base}/article' in txt

    listing = CliRunner().invoke(search_cli.cli, ['list-results', crawler.key, '--skipped'])
    assert listing.exit_code == 0, listing.output
    assert 'Council approves Example Solar Farm' in listing.output
    assert 'mentions project' in listing.output and 'not downloaded: login wall' in listing.output
    assert '[blocked site] https://www.facebook.com/groups/residents' in listing.output


def test_end_to_end_require_project_name_filters(data_dirs):
    (crawler, outcome, _), _, _ = _example_run(data_dirs, require_project_name=True)

    combined = json.loads((data_dirs['EXTRACTED_DIR'] / crawler.key / 'all_documents.json').read_text())
    assert [d['title'] for d in combined['documents']] == ['Council approves Example Solar Farm']
    assert outcome['project_filter']['filtered'] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_dry_run_prints_queries_without_requests(data_dirs, monkeypatch):
    monkeypatch.delenv(gsc.API_KEY_ENV, raising=False)

    async def no_network(*args, **kwargs):
        raise AssertionError("dry run must not make requests")
    monkeypatch.setattr(SerpApiProvider, '_fetch_page', no_network)

    # Searches are sent exactly as typed; without a subcommand, 'search' is assumed
    result = CliRunner().invoke(search_cli.cli, [
        'round river solar miami county indiana', '"Round River Solar" lawsuit OR zoning', '--country', 'us',
        '--dry-run',
    ])
    assert result.exit_code == 0, result.output
    assert '1. round river solar miami county indiana\n' in result.output
    assert '2. "Round River Solar" lawsuit OR zoning\n' in result.output
    assert 'q=round+river+solar+miami+county+indiana&gl=us' in result.output
    assert "under key 'search_round_river_solar_miami_county_indiana'" in result.output  # named after the first search
    assert 'checked for' not in result.output

    result = CliRunner().invoke(search_cli.cli, ['round river solar', '-p', 'Round River Solar', '--dry-run'])
    assert 'Results will be checked for "Round River Solar"' in result.output
    assert "under key 'search_round_river_solar'" in result.output


def test_cli_config_file_with_cli_override(tmp_path):
    config_file = tmp_path / 'search.json'
    config_file.write_text(json.dumps({'queries': ['example solar farm objection'], 'project': PROJECT,
                                       'max_results': 5}))
    config = search_cli.load_config(config_file, queries=None, project=None, max_results=20)
    assert config.queries == ['example solar farm objection'] and config.project == PROJECT
    assert config.max_results == 20


@pytest.mark.parametrize("args, message", [
    (['--dry-run'], 'Give at least one search'),
    (['round river solar', '--require-project-name', '--dry-run'], '--require-project-name needs --project'),
])
def test_cli_rejects_bad_arguments(args, message):
    result = CliRunner().invoke(search_cli.cli, ['search'] + args)
    assert result.exit_code == 2 and message in result.output


def test_cli_missing_api_key_fails_cleanly(data_dirs, monkeypatch):
    monkeypatch.delenv(gsc.API_KEY_ENV, raising=False)
    result = CliRunner().invoke(search_cli.cli, ['example solar farm'])
    assert result.exit_code == 1
    assert 'SERPAPI_API_KEY is not set' in result.output


def test_runs_accumulate_under_one_key(data_dirs):
    """A second search for the same application adds to the first instead of replacing it."""
    key = 'wiltshire_pl_2025_05830_search'
    q1, q2 = 'example solar farm objection', 'solar news roundup'
    first = SearchConfig(queries=[q1], project=PROJECT, delay=0, max_retries=0)
    second = SearchConfig(queries=[q2], project='Solar news', delay=0, max_retries=0)

    crawler, _, base = asyncio.run(_run_against_local_server(first, {q1: [('{base}/article', 'Article')]}, key=key))
    assert [r['title'] for r in crawler.results] == ['Article']

    # Port changes between runs, so the article URL differs; reuse the first run's URL to test dedupe
    article_url = crawler.results[0]['source_url']
    crawler, outcome, base = asyncio.run(_run_against_local_server(second, {
        q2: [(article_url, 'Article again'), ('{base}/other', 'Solar news roundup')],
    }, key=key))

    assert [r['title'] for r in crawler.new_results] == ['Solar news roundup']
    assert [r['title'] for r in crawler.results] == ['Article', 'Solar news roundup']
    assert crawler.results[0]['queries'] == [q1, q2]          # the repeat is recorded, not re-added
    assert len(crawler.new_downloads) == 1                      # the article isn't downloaded again
    assert sorted(p.name.split('_')[0] for p in crawler.docs_dir.iterdir()) == ['01', '02']

    metadata = json.loads((data_dirs['METADATA_DIR'] / f'{key}_metadata.json').read_text())
    assert metadata['linked_application'] == 'wiltshire_pl_2025_05830'
    assert metadata['projects'] == [PROJECT, 'Solar news']
    assert [s['query'] for s in metadata['searches']] == [q1, q2]

    # Each result is checked against the project name of its own search
    combined = json.loads((data_dirs['EXTRACTED_DIR'] / key / 'all_documents.json').read_text())
    mentions = {d['title']: d['mentions_project'] for d in combined['documents']}
    assert mentions == {'Article': True, 'Solar news roundup': True}


def test_fresh_starts_the_key_over(data_dirs):
    key = 'x_search'
    q1 = 'example solar farm objection'
    config = SearchConfig(queries=[q1], project=PROJECT, delay=0, max_retries=0)
    asyncio.run(_run_against_local_server(config, {q1: [('{base}/article', 'Article')]}, key=key))
    crawler, _, _ = asyncio.run(_run_against_local_server(config, {q1: [('{base}/other', 'Other')]},
                                                          key=key, fresh=True))
    assert [r['title'] for r in crawler.results] == ['Other']
    assert [p.name for p in crawler.docs_dir.iterdir()] == ['01_127_0_0_1_other.html']


def test_cli_application_option_sets_key(data_dirs, monkeypatch):
    monkeypatch.delenv(gsc.API_KEY_ENV, raising=False)
    result = CliRunner().invoke(search_cli.cli, [
        '-a', 'wiltshire_pl_2025_05830', 'flaxlands solar farm', '--dry-run'])
    assert result.exit_code == 0, result.output
    assert "under key 'wiltshire_pl_2025_05830_search'" in result.output

    result = CliRunner().invoke(search_cli.cli, ['-a', 'x', '-k', 'y', 'flaxlands', '--dry-run'])
    assert result.exit_code == 2 and 'not both' in result.output


def test_results_without_project_are_not_labelled(data_dirs):
    q = 'example solar farm'
    config = SearchConfig(queries=[q], delay=0, max_retries=0)
    crawler, outcome, _ = asyncio.run(_run_against_local_server(config, {q: [('{base}/article', 'Article')]}))
    assert outcome['project_filter'] == {'mention': 0, 'no_mention': 0, 'filtered': 0}
    combined = json.loads((data_dirs['EXTRACTED_DIR'] / crawler.key / 'all_documents.json').read_text())
    assert 'mentions_project' not in combined['documents'][0]
