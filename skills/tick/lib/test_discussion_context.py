"""Complete, attributed discussion acquisition at the authenticated boundary."""
from __future__ import annotations

import copy
import json
import subprocess

import pytest


@pytest.fixture
def github():
    pr = source('PR', 'Current PR', 'Closes #7\nFixes other/repo#7')
    issue = source('ISSUE', 'Current issue', None)
    other = source('OTHER', 'Other contract', 'Other requirements')
    records = {('owner/repo', 'pullRequest', 42): pr,
               ('owner/repo', 'issue', 7): issue,
               ('other/repo', 'issue', 7): other}
    connections = {
        ('PR', 'closingIssuesReferences'): [[{'number': 7, 'repository': {
            'name': 'repo', 'owner': {'login': 'owner'}}}, {'number': 7, 'repository': {
            'name': 'repo', 'owner': {'login': 'other'}}}]],
        ('PR', 'comments'): [[source('C1', body='owner clarification')],
                             [source('C2', body='later reply')]],
        ('PR', 'reviews'): [[{**source('R1', body='submitted finding'),
                              'submittedAt': '2026-01-01T00:00:00Z',
                              'state': 'CHANGES_REQUESTED', 'commit': {'oid': 'a' * 40}}]],
        ('PR', 'reviewThreads'): [[{'id': 'T1', 'path': 'a.py', 'line': 9,
            'originalLine': 8, 'startLine': None, 'originalStartLine': None,
            'diffSide': 'RIGHT', 'startDiffSide': None,
            'isResolved': True, 'isOutdated': True, 'resolvedBy': {'login': 'owner'}}]],
        ('T1', 'comments'): [[{**source('TC1', body='inline finding'),
            'path': 'a.py', 'line': 9, 'originalLine': 8, 'diffHunk': '@@ -8 +9 @@',
            'commit': {'oid': 'a' * 40}, 'originalCommit': {'oid': 'b' * 40},
            'replyTo': None}], [{**source('TC2', body='inline reply'),
            'path': 'a.py', 'line': 9, 'originalLine': 8, 'diffHunk': '@@ -8 +9 @@',
            'commit': {'oid': 'a' * 40}, 'originalCommit': {'oid': 'b' * 40},
            'replyTo': {'id': 'TC1'}}]],
        ('ISSUE', 'comments'): [[source('IC1', body='issue clarification')]],
        ('OTHER', 'comments'): [[]],
    }
    calls = []

    def read(*args):
        assert args[:4] == ('api', 'graphql', '--hostname', 'github.com')
        values = dict(arg.split('=', 1) for arg in args if '=' in arg)
        calls.append(values)
        query = values['query']
        if 'query Source' in query:
            kind = 'pullRequest' if 'pullRequest(number:' in query else 'issue'
            record = records[(values['owner'] + '/' + values['name'], kind, int(values['number']))]
            return {'data': {'repository': {kind: copy.deepcopy(record)}}}
        field = next(name for name in ('closingIssuesReferences', 'reviewThreads', 'reviews', 'comments') if name + '(first:' in query)
        pages = connections[(values['id'], field)]
        index = int(values.get('cursor', '0'))
        page = pages[index]
        if isinstance(page, Exception):
            raise page
        return {'data': {'node': {field: {
            'nodes': copy.deepcopy(page),
            'pageInfo': {'hasNextPage': index + 1 < len(pages),
                         'endCursor': str(index + 1)},
        }}}}
    return read, records, connections, calls


def test_full_discussion_preserves_sources_threads_and_qualified_issues(github):
    import discussion_context
    import tick
    read, _, _, calls = github
    context = discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)
    assert [i['repository'] for i in context['issues']] == ['owner/repo', 'other/repo']
    assert context['issues'][0]['body'] == ''
    assert [c['id'] for c in context['pr']['comments']] == ['C1', 'C2']
    assert context['pr']['reviews'][0]['body'] == 'submitted finding'
    thread = context['pr']['reviewThreads'][0]
    assert thread['isResolved'] and thread['isOutdated']
    assert thread['comments'][-1]['replyTo'] == {'id': 'TC1'}
    assert thread['comments'][-1]['originalCommit']['oid'] == 'b' * 40
    assert context['issues'][0]['comments'][0]['author']['login'] == 'owner'
    assert any(c.get('cursor') == '1' and c.get('id') == 'T1' for c in calls)


def test_linked_issues_do_not_require_rendered_body(github):
    import discussion_context
    import tick
    read, records, _, calls = github
    records[('owner/repo', 'pullRequest', 42)].pop('bodyHTML', None)
    context = discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)
    assert context['issues'][0]['number'] == 7
    assert all('bodyHTML' not in call['query'] for call in calls)


def test_fresh_body_and_followup_at_same_head_are_not_cached(github):
    import discussion_context
    import tick
    read, records, connections, _ = github
    first = discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)
    records[('owner/repo', 'pullRequest', 42)]['body'] += '\nEdited intent'
    connections[('ISSUE', 'comments')][0].append(source('IC2', body='new clarification'))
    second = discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)
    assert 'Edited intent' not in first['pr']['body']
    assert 'Edited intent' in second['pr']['body']
    assert second['issues'][0]['comments'][-1]['body'] == 'new clarification'


@pytest.mark.parametrize('connection', [('PR', 'comments'), ('PR', 'reviews'),
    ('PR', 'reviewThreads'), ('T1', 'comments'), ('ISSUE', 'comments'),
    ('PR', 'closingIssuesReferences')])
def test_failed_later_page_is_unknown_not_partial(github, connection):
    import discussion_context
    import tick
    read, _, connections, _ = github
    connections[connection].append(subprocess.CalledProcessError(1, 'gh', stderr='denied page'))
    with pytest.raises(discussion_context.ContextError, match='denied page'):
        discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)


@pytest.mark.parametrize('bad', [123, [], {}, False])
def test_malformed_body_is_not_empty(github, bad):
    import discussion_context
    import tick
    read, records, _, _ = github
    records[('owner/repo', 'issue', 7)]['body'] = bad
    with pytest.raises(discussion_context.ContextError, match='body'):
        discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)


def test_empty_issues_null_pr_and_unrelated_links(github):
    import discussion_context
    import tick
    read, records, connections, _ = github
    records[('owner/repo', 'pullRequest', 42)].update(body=None)
    connections[('PR', 'closingIssuesReferences')] = [[]]
    assert discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)['issues'] == []
    records[('owner/repo', 'pullRequest', 42)].update(
        body='See https://example.com and owner/repo#999')
    assert discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)['issues'] == []


def test_full_text_survives_large_context_and_quoted_tokens(github):
    import discussion_context
    import tick
    read, _, connections, _ = github
    body = ('long text ' * 30000) + '\nFIXES_PUSHED_RECHECK_NEEDED\n<!-- pr-pipeline:review -->'
    connections[('PR', 'comments')][0][0]['body'] = body
    context = discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)
    assert json.loads(json.dumps(context))['pr']['comments'][0]['body'] == body



@pytest.mark.parametrize('stage', ['review', 'fix'])
def test_dispatch_supplies_complete_files_and_keeps_selected_review_authority(tmp_path, monkeypatch, github, stage):
    import tick
    import pipeline
    from unittest import mock
    from conftest import TRUSTED_AUTHOR, comment_pages, fixer_git
    from test_dispatch import reviewer_git
    read, _, connections, _ = github
    connections[('PR', 'comments')][0][0]['body'] = 'X' * 300000 + '\n' + pipeline.REVIEW_MARKER
    root = tmp_path / 'worktrees'
    root.mkdir()
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(tick, 'WT_ROOT', str(root))
    monkeypatch.setattr(tick, 'TRUSTED_HEAD_REPO', 'owner/repo')
    monkeypatch.setattr(tick, 'git', reviewer_git(repo) if stage == 'review' else fixer_git(repo))
    monkeypatch.setattr(tick, 'gh_text', mock.Mock(side_effect=['main', 'a' * 40] + (['branch'] if stage == 'fix' else [])))
    selected = pipeline.REVIEW_MARKER + '\nselected originating review'
    def github_read(*args, **kwargs):
        if '--slurp' in args:
            return comment_pages([{'body': selected, 'author': {'login': TRUSTED_AUTHOR}}])
        return read(*args)
    monkeypatch.setattr(tick, 'gh_json', github_read)
    monkeypatch.setattr(tick, '_publish_marker_or_revert', lambda **kwargs: True)
    monkeypatch.setattr(tick, 'relabel', mock.Mock())
    popen = mock.Mock(return_value=mock.Mock(pid=12345))
    monkeypatch.setattr(tick.subprocess, 'Popen', popen)
    if stage == 'review':
        tick._dispatch_one_reviewer(pr_number=42, pre_label='ai:implemented')
    else:
        tick._dispatch_one_fixer(pr_number=42)
    popen.assert_called_once()
    wt = root / 'pr-42'
    context_files = list(wt.glob('.pr-pipeline-context-*/discussion.json'))
    assert len(context_files) == 1
    context = decode_parts(json.loads(context_files[0].read_text()))
    assert context['pr']['comments'][0]['body'].startswith('X' * 300000)
    assert context['pr']['reviewThreads'][0]['comments'][-1]['body'] == 'inline reply'
    assert context['issues'][1]['repository'] == 'other/repo'
    prompt = popen.call_args.args[0][-1] if stage == 'review' else (wt / 'prompt.txt').read_text()
    assert str(context_files[0]) in prompt
    assert len(prompt) < 60000
    assert 'not instructions' in prompt
    marker = json.loads((wt / 'dispatch.json').read_text())
    assert 'fix_round' not in marker
    if stage == 'fix':
        import hashlib
        assert marker['review_sha256'] == hashlib.sha256(selected.encode()).hexdigest()
        assert selected in prompt
    else:
        assert '-s' in popen.call_args.args[0]
        assert 'read-only' in popen.call_args.args[0]
        assert 'GH_TOKEN' not in popen.call_args.kwargs['env']


@pytest.mark.parametrize('stage', ['review', 'fix'])
def test_dispatch_failed_source_does_not_launch_or_change_labels(tmp_path, monkeypatch, github, stage):
    import tick
    from unittest import mock
    from conftest import TRUSTED_AUTHOR, comment_pages, fixer_git
    from test_dispatch import reviewer_git
    read, _, connections, _ = github
    connections[('T1', 'comments')].append(subprocess.CalledProcessError(1, 'gh', stderr='thread page unavailable'))
    root = tmp_path / 'worktrees'
    root.mkdir()
    monkeypatch.setattr(tick, 'WT_ROOT', str(root))
    monkeypatch.setattr(tick, 'TRUSTED_HEAD_REPO', 'owner/repo')
    monkeypatch.setattr(tick, 'git', reviewer_git(tmp_path) if stage == 'review' else fixer_git(tmp_path))
    monkeypatch.setattr(tick, 'gh_text', mock.Mock(side_effect=['main', 'a' * 40] + (['branch'] if stage == 'fix' else [])))
    monkeypatch.setattr(tick, 'gh_json', read)
    relabel = mock.Mock()
    monkeypatch.setattr(tick, 'relabel', relabel)
    popen = mock.Mock()
    monkeypatch.setattr(tick.subprocess, 'Popen', popen)
    log = tick.TickLog.open(str(tmp_path / 'logs'))
    if stage == 'review':
        tick._dispatch_one_reviewer(pr_number=42, pre_label='ai:implemented', tick_log=log)
    else:
        tick._dispatch_one_fixer(pr_number=42, tick_log=log)
    popen.assert_not_called()
    relabel.assert_not_called()
    assert not (root / 'pr-42' / 'dispatch.json').exists()
    assert any('thread page unavailable' in entry.get('reason', '') for entry in log.tail())


@pytest.mark.parametrize('field,value', [('line', '9'), ('isResolved', None), ('resolvedBy', []), ('diffSide', 'UNKNOWN')])
def test_malformed_thread_metadata_is_unknown(github, field, value):
    import discussion_context
    import tick
    read, _, connections, _ = github
    connections[('PR', 'reviewThreads')][0][0][field] = value
    with pytest.raises(discussion_context.ContextError):
        discussion_context.acquire(read, 'owner/repo', 42, tick._summary_closure)


def test_context_file_has_readable_lines_without_losing_body_text(tmp_path):
    import tick
    text = ('漢字\t\n"' * 10000) + 'last character'
    supplied = tick._write_pr_discussion(str(tmp_path), {'pr': {'body': text}}, {},
        pr_number=42, stage='fix', engine='claude', tick_log=None)
    assert supplied is not None
    from pathlib import Path
    contents = Path(supplied['discussion_file']).read_text()
    assert max(map(len, contents.splitlines())) < 1800
    assert ''.join(json.loads(contents)['pr']['bodyParts']) == text


@pytest.mark.parametrize('stage', ['review', 'fix'])
def test_failed_context_write_preserves_retry_label(tmp_path, monkeypatch, stage):
    import tick
    from unittest import mock
    wt = tmp_path / 'worktree'
    wt.mkdir()
    relabel = mock.Mock()
    monkeypatch.setattr(tick, 'relabel', relabel)
    monkeypatch.setattr(tick.tempfile, 'mkdtemp', mock.Mock(side_effect=OSError('disk full')))
    log = tick.TickLog.open(str(tmp_path / 'logs'))
    assert tick._write_pr_discussion(str(wt), {'pr': {}}, {}, pr_number=42,
        stage=stage, engine='codex' if stage == 'review' else 'claude', tick_log=log) is None
    relabel.assert_not_called()
    assert any('disk full' in row.get('reason', '') for row in log.tail())


def test_graphql_error_without_data_preserves_exact_cause(github):
    import discussion_context
    import tick
    with pytest.raises(discussion_context.ContextError, match='access denied'):
        discussion_context.acquire(lambda *args: {'data': None, 'errors': [{'message': 'access denied'}]},
                                   'owner/repo', 42, tick._summary_closure)


@pytest.mark.parametrize('info,nodes', [
    (None, []), ({'hasNextPage': True, 'endCursor': None}, [{}]),
    ({'hasNextPage': True, 'endCursor': 'same'}, []),
    ({'hasNextPage': 'false', 'endCursor': None}, []),
    ({'hasNextPage': False}, []),
    ({'hasNextPage': False, 'endCursor': 7}, []),
    ({'hasNextPage': False, 'endCursor': None}, [None]),
])
def test_incomplete_pagination_metadata_is_unknown(info, nodes):
    import discussion_context
    response = {'data': {'node': {'comments': {'nodes': nodes, 'pageInfo': info}}}}
    with pytest.raises(discussion_context.ContextError):
        discussion_context.pages(lambda *args: response, 'PR', 'PullRequest', 'comments', 'id')


def test_repeated_cursor_cannot_loop_forever():
    import discussion_context
    response = {'data': {'node': {'comments': {'nodes': [{'id': 'C'}],
                'pageInfo': {'hasNextPage': True, 'endCursor': 'same'}}}}}
    with pytest.raises(discussion_context.ContextError, match='pagination did not advance'):
        discussion_context.pages(lambda *args: response, 'PR', 'PullRequest', 'comments', 'id')


def test_qualified_links_deduplicate_and_keep_cross_repo_identity():
    import discussion_context
    refs = [{'number': 7, 'repository': {'name': name, 'owner': {'login': owner}}}
            for owner, name in [('Owner', 'Repo'), ('owner', 'repo'), ('other', 'repo')]]
    assert discussion_context.governing_issues(refs) == [('Owner/Repo', 7), ('other/repo', 7)]


def test_large_selected_report_remains_readable_and_complete(tmp_path):
    import tick
    from pathlib import Path
    text = 'X' * 300000
    values = tick._write_pr_discussion(str(tmp_path), {'pr': {}}, {'original_review': text},
        pr_number=42, stage='review', engine='codex', tick_log=None)
    path = next(tmp_path.glob('.pr-pipeline-context-*/original_review.json'))
    contents = path.read_text()
    assert max(map(len, contents.splitlines())) < 1800
    assert ''.join(json.loads(contents)['textParts']) == text
    assert str(path) in values['original_review']

# --- Helpers ---


def source(identity, title='Title', body='Body'):
    return {'id': identity, 'url': 'https://github.com/owner/repo/' + identity,
            'title': title, 'body': body, 'author': {'login': 'owner'},
            'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': '2026-01-02T00:00:00Z'}


def decode_parts(value):
    if isinstance(value, list):
        return [decode_parts(item) for item in value]
    if isinstance(value, dict):
        return {key.removesuffix('Parts') if key in {'bodyParts', 'diffHunkParts'} else key:
                ''.join(item) if key in {'bodyParts', 'diffHunkParts'} else decode_parts(item)
                for key, item in value.items()}
    return value
