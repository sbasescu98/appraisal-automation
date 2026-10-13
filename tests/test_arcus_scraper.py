"""
Tests for the Arcus public register comment scraper. The register is replaced by a local
aiohttp server that mimics the page HTML and the Aura API, so no network is needed.

Run: pytest
"""

import asyncio
import json
from urllib.parse import parse_qs, quote

import pytest
from aiohttp import web
from click.testing import CliRunner

import arcus_scraper
import cli
import pdf_extractor
from arcus_scraper import ArcusScraper, ArcusError, council_from_host, is_arcus_url

WILTSHIRE_URL = ("https://development.wiltshire.gov.uk/pr3/s/planning-application/"
                 "a0iQ300000FJY0XIAX/pl202505830?c__r=Arcus_BE_Public_Register&tabset-72eec=3")

HEADER = {
    "title": "PL/2025/05830",
    "fields": [
        {"label": "Site Address", "value": "Land at Flaxlands Farm, Royal Wootton Bassett, Swindon, SN4 8DY"},
        {"label": "Proposal", "value": "The construction and operation of a 49.9MW solar farm"},
    ],
}
DETAILS = {"sections": [
    {"fields": [{"label": "Application Type", "type": "Picklist", "value": "Full planning permission"}]},
    {"fields": [{"label": "Decision Date", "type": "Date", "value": "2026-09-14 00:00:00"},
                {"label": "Issued Decision", "type": "String", "value": "Approve with Conditions"}]},
]}
COMMENTS = [
    {"Id": "a07A", "CreatedDate": "2026-05-27T15:25:14.000Z", "arcshared__Stance__c": "Object",
     "arcshared__Name__c": "Jane Smith", "arcshared__Text__c": "I object to the loss of farmland."},
    {"Id": "a07B", "CreatedDate": "2026-05-27T16:00:00.000Z", "arcshared__Stance__c": "Object",
     "arcshared__Name__c": "Jane Smith", "arcshared__Text__c": "Further objection about glare."},
    {"Id": "a07C", "CreatedDate": "2026-06-01T09:00:00.000Z", "arcshared__Stance__c": "Support",
     "arcshared__Name__c": "Natural England", "arcshared__Text__c": "No objection subject to conditions."},
]

FILES = [
    {"Id": "068A", "Title": "Decision Notice", "FileExtension": "txt", "ContentSize": 33,
     "CreatedDate": "2026-09-14T15:51:49.000Z", "arcshared__Document_Date__c": "2026-09-14",
     "arcshared__Category__c": "Decision", "Description": ".", "arcusbuiltenv__Status__c": "Submitted"},
    {"Id": "068B", "Title": "Site Location Plan", "FileExtension": "pdf", "ContentSize": 10,
     "CreatedDate": "2025-07-09T10:00:00.000Z", "arcshared__Document_Date__c": "2025-07-09",
     "arcshared__Category__c": "Plan", "Description": "Figure 1", "arcusbuiltenv__Status__c": "Submitted"},
    {"Id": "068C", "Title": "Biodiversity Net Gain Plan", "FileExtension": "pdf", "ContentSize": 10,
     "CreatedDate": "2026-06-23T10:00:00.000Z", "arcshared__Document_Date__c": "2026-06-23",
     "arcshared__Category__c": "Revised Plans/Documents", "Description": ".", "arcusbuiltenv__Status__c": "Submitted"},
    {"Id": "068D", "Title": "Highway Comments", "FileExtension": "pdf", "ContentSize": 10,
     "CreatedDate": "2025-09-18T10:00:00.000Z", "arcshared__Document_Date__c": "2025-09-18",
     "arcusbuiltenv__Status__c": "Submitted"},
]
FILE_BODIES = {"068A": b"Application approved with conditions", "068B": b"%PDF-plan.", "068C": b"%PDF-bng..",
               "068D": b"%PDF-hwy.."}
FILES[0]["ContentSize"] = len(FILE_BODIES["068A"])


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def test_parses_wiltshire_url():
    s = ArcusScraper(WILTSHIRE_URL)
    assert s.base_url == "https://development.wiltshire.gov.uk"
    assert s.site_prefix == "/pr3"
    assert s.record_id == "a0iQ300000FJY0XIAX"
    assert s.register == "Arcus_BE_Public_Register"
    assert s.council == "wiltshire"
    assert s.page_url == "https://development.wiltshire.gov.uk/pr3/s/planning-application/a0iQ300000FJY0XIAX"


def test_parses_url_without_site_prefix_or_register():
    s = ArcusScraper("https://planning.example.gov.uk/s/planning-application/a0i000000000001AAA")
    assert s.site_prefix == "" and s.register == arcus_scraper.DEFAULT_REGISTER


@pytest.mark.parametrize("url, expected", [
    (WILTSHIRE_URL, True),
    ("https://newplanningaccess.eastriding.gov.uk/newplanningaccess/applicationDetails.do?keyVal=X", False),
    ("https://example.com/s/planning-application/short", False),
])
def test_is_arcus_url(url, expected):
    assert is_arcus_url(url) is expected


@pytest.mark.parametrize("host, council", [
    ("development.wiltshire.gov.uk", "wiltshire"),
    ("planning.somerset.gov.uk", "somerset"),
    ("register.example.co.uk", "co"),  # no 'gov': falls back to the second-to-last label
])
def test_council_from_host(host, council):
    assert council_from_host(host) == council


# ---------------------------------------------------------------------------
# Fake register
# ---------------------------------------------------------------------------

PAGE_HTML = (
    '<html><script src="/pr3/s/sfsites/l/'
    + quote('{"mode":"PROD","fwuid":"FWUID123","loaded":{"APPLICATION@markup://siteforce:communityApp":"APP456"}}')
    + '/inline.js"></script></html>'
)


async def _run_against_fake_register(comments, fail_method=None, status=200, files=FILES, skip_categories=None,
                                     max_downloads=None):
    """Serve a fake register, run the scraper against it, and return (scraper, requests seen)."""
    seen = []
    downloads = []

    async def download(request):
        file_id = request.match_info['file_id']
        downloads.append(file_id)
        return web.Response(body=FILE_BODIES[file_id], content_type='application/octet-stream')

    async def page(request):
        return web.Response(text=PAGE_HTML, content_type='text/html')

    async def aura(request):
        form = await request.post()
        message = json.loads(form['message'])
        context = json.loads(form['aura.context'])
        seen.append({'context': context, 'methods': [a['params']['method'] for a in message['actions']],
                     'params': message['actions'][0]['params']['params']})
        if status != 200:
            return web.Response(status=status)
        returns = {'getRecordHeaderDetails': HEADER, 'getRecordDetails': DETAILS, 'getComments': comments,
                   'getFiles': files}
        actions = []
        for a in message['actions']:
            method = a['params']['method']
            if method == fail_method:
                actions.append({'id': a['id'], 'state': 'ERROR', 'error': [{'message': 'Something broke'}]})
            else:
                actions.append({'id': a['id'], 'state': 'SUCCESS',
                                'returnValue': {'returnValue': returns[method], 'cacheable': True}})
        return web.Response(text='while(1);\n' + json.dumps({'actions': actions}), content_type='application/json')

    app = web.Application()
    app.router.add_get('/pr3/s/planning-application/{record}', page)
    app.router.add_post('/pr3/s/sfsites/aura', aura)
    app.router.add_get('/pr3/sfc/servlet.shepherd/version/download/{file_id}', download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        scraper = ArcusScraper(f"http://127.0.0.1:{port}/pr3/s/planning-application/a0iQ300000FJY0XIAX/x",
                               skip_categories=skip_categories)
        scraper.max_retries = 0
        scraper.download_delay = 0
        await scraper.run(max_downloads=max_downloads)
        scraper.downloads_requested = downloads
        return scraper, seen
    finally:
        await runner.cleanup()


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    """Point every module's data folders at a temporary directory."""
    dirs = {
        'DOCUMENTS_DIR': tmp_path / 'documents',
        'METADATA_DIR': tmp_path / 'metadata',
        'EXTRACTED_DIR': tmp_path / 'extracted',
    }
    for name, path in dirs.items():
        for module in (arcus_scraper, pdf_extractor):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, path)
    return dirs


def test_scrapes_comments_end_to_end(data_dirs):
    scraper, seen = asyncio.run(_run_against_fake_register(COMMENTS))

    # The API is called with the version IDs from the page, for the right record
    assert seen[0]['context']['fwuid'] == 'FWUID123'
    assert seen[0]['context']['loaded'] == {'APPLICATION@markup://siteforce:communityApp': 'APP456'}
    assert seen[0]['methods'] == ['getRecordHeaderDetails', 'getRecordDetails', 'getComments', 'getFiles']
    assert seen[0]['params'] == {'recordId': 'a0iQ300000FJY0XIAX', 'registerName': 'Arcus_BE_Public_Register'}

    assert scraper.reference == 'PL/2025/05830'
    assert scraper.application_key == '0_pl_2025_05830'  # council name comes from the host (here 127.0.0.1)
    assert scraper.details['Issued Decision'] == 'Approve with Conditions'
    assert scraper.details['Decision Date'] == '2026-09-14'

    # One text file per comment (same-day comments from one person get unique names), plus every
    # document except the skipped categories (Plan, Revised Plans/Documents, Superseded)
    files = sorted(p.name for p in scraper.app_dir.iterdir())
    assert files == [
        '2025-09-18_uncategorised_highway_comments.pdf',
        '2026-05-27_comment_object_jane_smith.txt',
        '2026-05-27_comment_object_jane_smith_2.txt',
        '2026-06-01_comment_support_natural_england.txt',
        '2026-09-14_decision_decision_notice.txt',
    ]
    assert sorted(scraper.downloads_requested) == ['068A', '068D']
    assert (scraper.app_dir / '2026-09-14_decision_decision_notice.txt').read_bytes() == FILE_BODIES['068A']
    text = (scraper.app_dir / '2026-05-27_comment_object_jane_smith.txt').read_text()
    assert 'Commenter: Jane Smith' in text and 'Stance: Object' in text
    assert 'I object to the loss of farmland.' in text

    metadata = json.loads((data_dirs['METADATA_DIR'] / f'{scraper.application_key}_metadata.json').read_text())
    assert metadata['portal'] == 'arcus' and metadata['reference'] == 'PL/2025/05830'
    assert metadata['summary']['by_stance'] == {'Object': 2, 'Support': 1}
    assert metadata['summary']['by_category'] == {
        'Decision': 1, 'Plan': 1, 'Revised Plans/Documents': 1, 'Unknown': 1}
    row = metadata['documents_table'][0]
    assert row['comment_id'] == 'a07A' and row['date_published'] == '27 May 2026'
    assert row['description'] == 'Object: Jane Smith'
    plan = next(r for r in metadata['documents_table'] if r.get('file_id') == '068B')
    assert plan['description'] == 'Site Location Plan (Figure 1)' and plan['document_type'] == 'Plan'
    assert plan['file_url'].endswith('/pr3/sfc/servlet.shepherd/version/download/068B')

    # The standard extractor picks the comments up, with commenter and stance carried through
    summary = asyncio.run(pdf_extractor.DocumentExtractor(scraper.application_key).extract_all_documents())
    assert summary['overall']['successful'] == 4  # 3 comments + the .txt decision (the fake PDF fails)
    combined = json.loads(
        (data_dirs['EXTRACTED_DIR'] / scraper.application_key / 'all_documents.json').read_text())
    doc = combined['documents'][0]
    assert doc['commenter'] == 'Jane Smith' and doc['stance'] == 'Object' and doc['comment_id'] == 'a07A'

    # Comments and documents are also compiled separately
    extract_dir = data_dirs['EXTRACTED_DIR'] / scraper.application_key
    comments = json.loads((extract_dir / 'comments.json').read_text())
    assert comments['total_comments'] == 3 and [c['stance'] for c in comments['comments']] == ['Object', 'Object', 'Support']
    documents = json.loads((extract_dir / 'documents.json').read_text())
    assert [d['title'] for d in documents['documents']] == ['Decision Notice']
    comments_txt = (extract_dir / 'comments.txt').read_text()
    assert comments_txt.startswith(f'APPLICATION {scraper.application_key} - 3 comments\n2 Object, 1 Support\n')
    assert 'COMMENT 3/3' in comments_txt and 'Decision Notice' not in comments_txt


def test_rerun_replaces_old_comment_files(data_dirs):
    scraper, _ = asyncio.run(_run_against_fake_register(COMMENTS, files=[]))
    (scraper.app_dir / 'keep_me.pdf').write_text('not a comment')
    scraper, _ = asyncio.run(_run_against_fake_register(COMMENTS[2:], files=[]))
    assert sorted(p.name for p in scraper.app_dir.iterdir()) == [
        '2026-06-01_comment_support_natural_england.txt', 'keep_me.pdf',
    ]


def test_api_error_is_reported(data_dirs):
    with pytest.raises(ArcusError, match='getComments failed: Something broke'):
        asyncio.run(_run_against_fake_register(COMMENTS, fail_method='getComments'))


def test_http_error_is_reported(data_dirs):
    with pytest.raises(ArcusError, match='status 403'):
        asyncio.run(_run_against_fake_register(COMMENTS, status=403))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args, message", [
    ([], 'Give either --application-key or --url'),
    (['-k', 'X', '--url', WILTSHIRE_URL], 'Give either --application-key or --url'),
    (['--url', 'https://example.com/planning/123'], 'Unsupported portal URL'),
])
def test_cli_rejects_bad_arguments(args, message):
    result = CliRunner().invoke(cli.cli, ['scrape'] + args)
    assert result.exit_code == 2
    assert message in result.output


@pytest.mark.parametrize("council, reference, key", [
    ("wiltshire", "PL/2025/05830", "wiltshire_pl_2025_05830"),
    ("eastriding", "25/01104/PLF", "eastriding_25_01104_plf"),
    ("eastriding", "18/30161/CONDET", "eastriding_18_30161_condet"),
])
def test_application_storage_key(council, reference, key):
    from planning_spider import application_storage_key
    assert application_storage_key(council, reference) == key


def test_rerun_does_not_download_documents_again(data_dirs):
    asyncio.run(_run_against_fake_register(COMMENTS))
    scraper, _ = asyncio.run(_run_against_fake_register(COMMENTS))
    assert scraper.downloads_requested == []
    assert sum(1 for d in scraper.downloaded if d['document_type'] != 'Comment') == 2


def test_all_categories_and_max_downloads(data_dirs):
    scraper, _ = asyncio.run(_run_against_fake_register(COMMENTS, skip_categories=set()))
    assert sorted(scraper.downloads_requested) == ['068A', '068B', '068C', '068D']

    scraper, _ = asyncio.run(_run_against_fake_register(COMMENTS[:1], skip_categories=set(), max_downloads=1,
                                                        files=FILES[1:2]))
    assert len(scraper.comments) == 1 and len([d for d in scraper.downloaded if d['document_type'] != 'Comment']) == 1
