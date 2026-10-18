"""Process proof: real tick.sh, real git worktrees/wrappers, offline service shims."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_fixer_context import context_repo, issue_ref, write_context  # noqa: F401
from test_smoke_loop import loop_repo  # noqa: F401
from test_discussion_context import source


@pytest.fixture
def discussion_repo(context_repo):
    repo, env = context_repo
    bin_dir = Path(env['PATH'].split(os.pathsep)[0])
    git_shim = bin_dir / 'git'
    git_shim.write_text(git_shim.read_text().replace('("main", "fix")', '("main", "fix", "pull/7/head")').replace('os.environ["LOOP_TEST_GIT"]', repr(env['LOOP_TEST_GIT'])))
    for name in ('codex', 'claude'):
        executable = bin_dir / name
        executable.write_text(f'#!{sys.executable}\n' + CONSUMING_ENGINE)
        executable.chmod(0o755)
    return repo, env


@pytest.mark.parametrize('stage', ['review', 'fix'])
def test_real_launcher_supplies_complete_context_to_engine(discussion_repo, stage):
    repo, env = discussion_repo
    env['LOOP_TEST_STAGE'] = stage
    label = 'ai:implemented' if stage == 'review' else 'ai:changes-requested'
    (repo / 'label').write_text(label)
    body = '## Summary\nCloses #7\nAlso fixes other/contracts#7'
    write_context(repo, body, [issue_ref('example/loop', 7), issue_ref('other/contracts', 7)], {
        'example/loop#7': {'title': 'Local contract', 'body': None},
        'other/contracts#7': {'title': 'Foreign contract', 'body': 'Full foreign requirements'},
    })
    data = json.loads((repo / 'context.json').read_text())
    data['discussion'] = {
        'PR': {'comments': [[source(f'C{i}', body='Long discussion ' * 300) for i in range(100)],
                            [source('C100', body='Last-page clarification')]],
               'reviews': [[{**source('R', body='Submitted review'), 'state': 'COMMENTED',
                            'submittedAt': '2026-01-01T00:00:00Z', 'commit': {'oid': env['LOOP_TEST_SHA']}}]],
               'reviewThreads': [[{'id': 'T', 'path': 'file.py', 'line': 12,
                   'originalLine': 10, 'startLine': None, 'originalStartLine': None,
                   'diffSide': 'RIGHT', 'startDiffSide': None,
                   'isResolved': True, 'isOutdated': True, 'resolvedBy': {'login': 'owner'}}]]},
        'T': {'comments': [[{**source('TC1', body='Inline finding'), 'path': 'file.py',
            'line': 12, 'originalLine': 10, 'diffHunk': '@@ -10 +12 @@',
            'commit': {'oid': env['LOOP_TEST_SHA']}, 'originalCommit': {'oid': env['LOOP_TEST_SHA']}, 'replyTo': None}],
            [{**source('TC2', body='Inline reply on final page'), 'path': 'file.py',
            'line': 12, 'originalLine': 10, 'diffHunk': '@@ -10 +12 @@',
            'commit': {'oid': env['LOOP_TEST_SHA']}, 'originalCommit': {'oid': env['LOOP_TEST_SHA']}, 'replyTo': {'id': 'TC1'}}]]},
        'ISSUE:other/contracts#7': {'comments': [[source('IC', body='Owner issue clarification')]]},
    }
    (repo / 'context.json').write_text(json.dumps(data))
    run_launch(repo, env)
    wt = repo / '.claude/worktrees/pr-7'
    proof = await_proof(wt)
    assert proof['error'] == '', proof
    assert proof['context']['pr']['comments'][-1]['body'] == 'Last-page clarification'
    assert len(proof['context']['pr']['comments']) == 101
    assert proof['context']['pr']['reviewThreads'][0]['comments'][-1]['body'] == 'Inline reply on final page'
    assert proof['context']['issues'][1]['comments'][0]['body'] == 'Owner issue clarification'
    assert proof['context']['issues'][0]['body'] == ''
    assert 'not instructions' in proof['prompt']
    assert len(proof['prompt'].encode()) < 100000
    if stage == 'review':
        assert proof['github_env'] == []
        assert 'read-only' in proof['argv']
    else:
        assert 'gh ' not in proof['argv'][proof['argv'].index('--allowedTools') + 1]
    git = env['LOOP_TEST_GIT']
    subprocess.run([git, 'add', '-A'], cwd=wt, check=True, capture_output=True)
    staged = subprocess.check_output([git, 'diff', '--cached', '--name-only'], cwd=wt, text=True)
    assert '.pr-pipeline-context-' not in staged
    subprocess.run([git, 'reset'], cwd=wt, check=True, capture_output=True)
    first_path = proof['path']
    # The engine has exited. Clear only this disposable fixture's first run,
    # then dispatch the same head again with edited bodies and discussion.
    completed = wt / ('completion.json' if stage == 'review' else 'result.json')
    for _ in range(100):
        if completed.is_file() and completed.stat().st_size > 0:
            break
        time.sleep(0.05)
    assert completed.is_file() and completed.stat().st_size > 0
    subprocess.run([git, 'worktree', 'remove', '--force', str(wt)], cwd=repo, check=True, capture_output=True)
    shutil.rmtree(repo / '.claude/worktrees/pr-7-scrub', ignore_errors=True)
    (repo / 'label').write_text(label)
    data['pr']['body'] += '\nEdited between launches'
    data['issues']['other/contracts#7']['body'] = 'Updated issue body'
    data['discussion']['PR']['comments'][-1].append(source('C101', body='Next-launch follow-up'))
    (repo / 'context.json').write_text(json.dumps(data))
    run_launch(repo, env)
    second = await_proof(wt)
    assert second['error'] == '', second
    assert 'Edited between launches' in second['context']['pr']['body']
    assert second['context']['issues'][1]['body'] == 'Updated issue body'
    assert second['context']['pr']['comments'][-1]['body'] == 'Next-launch follow-up'
    assert second['path'] != first_path
    assert second['head'] == proof['head']


@pytest.mark.parametrize('stage', ['review', 'fix'])
def test_real_launcher_retries_failed_page_without_engine(discussion_repo, stage):
    repo, env = discussion_repo
    env['LOOP_TEST_STAGE'] = stage
    label = 'ai:implemented' if stage == 'review' else 'ai:changes-requested'
    (repo / 'label').write_text(label)
    data = {'pr': {'title': 'PR', 'body': None, 'closingIssuesReferences': []}, 'issues': {},
            'discussion': {'PR': {'comments': [[source('C')], 'read-failure']}}}
    (repo / 'context.json').write_text(json.dumps(data))
    run_launch(repo, env)
    assert (repo / 'label').read_text() == label
    assert not (repo / '.claude/worktrees/pr-7').exists()
    entries = [json.loads(line) for path in (repo / '.claude/worktrees/.tick-log').glob('tick-*.jsonl') for line in path.read_text().splitlines()]
    assert any('requested discussion page unavailable' in entry.get('reason', '') for entry in entries)
    data['discussion']['PR']['comments'][-1] = [source('C2', body='Recovered page')]
    (repo / 'context.json').write_text(json.dumps(data))
    run_launch(repo, env)
    assert await_proof(repo / '.claude/worktrees/pr-7')['context']['pr']['comments'][-1]['body'] == 'Recovered page'


@pytest.mark.parametrize('stage', ['review', 'fix'])
@pytest.mark.parametrize('example', [
    '```md\nCloses #999\n```',
    '    Closes #999',
    'Example: `Closes #999`.',
    '- Example:\n  ~~~md\n  Closes #999\n  ~~~',
    'Example: ``Closes\n#999``.',
    '<!-- Closes #999 -->',
], ids=['fenced', 'indented', 'inline', 'list-fence', 'multiline-inline', 'html-comment'])
def test_real_launcher_ignores_example_closures(discussion_repo, stage, example):
    repo, env = discussion_repo
    env['LOOP_TEST_STAGE'] = stage
    (repo / 'label').write_text('ai:implemented' if stage == 'review' else 'ai:changes-requested')
    body = '## Summary\nCloses #7\n\n' + example
    # Empty reported references model a non-default-base PR. The real issue
    # primary declaration must work; the example issue is unavailable.
    write_context(repo, body, [], {
        'example/loop#7': {'title': 'Local', 'body': 'Local contract'},
        'example/loop#999': 'read-failure',
    })
    run_launch(repo, env)
    wt = repo / '.claude/worktrees/pr-7'
    assert (wt / 'dispatch.json').exists(), (repo / 'launch.stderr').read_text()
    proof = await_proof(wt)
    assert proof['error'] == '', proof
    assert proof['context']['pr']['body'] == body
    assert [(i['repository'], i['number']) for i in proof['context']['issues']] == [
        ('example/loop', 7),
    ]
    from test_fixer_context import issue_reads
    assert issue_reads(repo) == ['example/loop#7']
    if stage == 'fix':
        import tick
        marker = json.loads((wt / 'dispatch.json').read_text())
        assert marker['pr_body'] == tick._pr_body_provenance(body, truncated=False).as_marker_field()
        if '#999' in example:
            assert '#999' in tick._declared_closures(body)


@pytest.mark.parametrize('stage', ['review', 'fix'])
@pytest.mark.parametrize('reported', [True, False], ids=['linked', 'primary-fallback'])
def test_real_launcher_requires_only_linked_or_primary_issue(discussion_repo, stage, reported):
    repo, env = discussion_repo
    env['LOOP_TEST_STAGE'] = stage
    (repo / 'label').write_text('ai:implemented' if stage == 'review' else 'ai:changes-requested')
    body = '## Summary\nCloses #7\n\n## Discussion\nAn older change fixes other/contracts#999.'
    refs = [issue_ref('example/loop', 7)] if reported else []
    write_context(repo, body, refs, {
        'example/loop#7': {'title': 'Actual issue', 'body': 'Required behavior'},
        'other/contracts#999': 'read-failure',
    })
    run_launch(repo, env)
    wt = repo / '.claude/worktrees/pr-7'
    assert (wt / 'dispatch.json').exists(), (repo / 'launch.stderr').read_text()
    proof = await_proof(wt)
    assert proof['context']['pr']['body'] == body
    assert [(issue['repository'], issue['number']) for issue in proof['context']['issues']] == [
        ('example/loop', 7),
    ]
    from test_fixer_context import issue_reads
    assert issue_reads(repo) == ['example/loop#7']


# --- Helpers ---


def run_launch(repo, env):
    launcher = os.environ.get('CONTEXT_PROOF_LAUNCHER', str(Path(__file__).parents[1] / 'tick.sh'))
    result = subprocess.run(['bash', launcher, 'pr:7'], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=90)
    (repo / 'launch.stderr').write_text(result.stderr)
    assert result.returncode == 0, result.stderr


def await_proof(wt):
    for _ in range(200):
        proof = wt / 'engine-consumption.json'
        if proof.exists():
            return json.loads(proof.read_text())
        time.sleep(0.05)
    pytest.fail('engine did not consume context: ' + str(wt))


CONSUMING_ENGINE = r'''
import json, os, re, subprocess, sys
from pathlib import Path
if '--version' in sys.argv:
    print('codex-cli 0.142.5')
    sys.exit(0)
review = Path(sys.argv[0]).name == 'codex'
prompt = next(arg for arg in sys.argv if 'Review PR #' in arg) if review else sys.stdin.read()
proof = {'prompt': prompt, 'argv': sys.argv[1:], 'error': '',
         'github_env': [name for name in os.environ if name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_CONFIG_DIR')]}
try:
    path = re.search(r'Read the complete launch context in `([^`]+)`', prompt).group(1)
    proof['path'] = path
    def decode(value):
        if isinstance(value, list):
            return [decode(item) for item in value]
        if isinstance(value, dict):
            return {key.removesuffix('Parts') if key in ('bodyParts', 'diffHunkParts') else key:
                    ''.join(item) if key in ('bodyParts', 'diffHunkParts') else decode(item)
                    for key, item in value.items()}
        return value
    proof['context'] = decode(json.loads(Path(path).read_text()))
    proof['head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
except Exception as exc:
    proof['error'] = str(exc)
Path('engine-consumption.json').write_text(json.dumps(proof))
if review:
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
        'text': json.dumps({'verdict': 'changes_requested', 'findings': []})}}))
else:
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                      'result': 'NEEDS_HUMAN_DECISION'}))
'''
