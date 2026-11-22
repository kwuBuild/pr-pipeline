"""Reviewer output recency through the real wrapper and operator CLI."""
from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import pytest

import reviewer_observability
import tick
from test_only_smoke import only_repo, run_tick, write_items
from test_smoke_loop import loop_repo, GIT_SHIM


@pytest.fixture
def reviewer(tmp_path):
    worktree = tmp_path / 'worktrees' / 'pr-7'
    worktree.mkdir(parents=True)
    processes = []

    def launch(*, broken_heartbeat=False, observation=True):
        if broken_heartbeat:
            (worktree / 'heartbeat').mkdir()
        command = wrapper_command(worktree)
        if observation:
            command += ['--observation-path', str(worktree / 'review-observation.json')]
        proc = subprocess.Popen(
            [*command, '--', sys.executable, '-c', ENGINE, '-o', 'placeholder'],
            cwd=worktree, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        processes.append(proc)
        wait_until(lambda: (worktree / 'started').exists() or proc.poll() is not None)
        assert proc.poll() is None, proc.communicate()[1].decode()
        return worktree, proc

    yield launch
    for proc in processes:
        if proc.poll() is None:
            (worktree / 'finish').touch()
            (worktree / 'emit').touch()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.communicate(timeout=10)


def test_stream_heartbeat_preserves_bounded_observation_and_throttles(tmp_path):
    events = b'{}\n' + b'x' * 80 + b'\nnot-json\n{}\n'
    expected = reviewer_observability.observe_event_stream(BytesIO(events), max_event_bytes=32)
    heartbeat = tmp_path / 'heartbeat'
    with mock.patch('time.monotonic', side_effect=[0, 1, 4.99, 5]), \
            mock.patch('os.utime', wraps=os.utime) as touched:
        observed = reviewer_observability.observe_event_stream(
            BytesIO(events), max_event_bytes=32, heartbeat_path=str(heartbeat),
        )
    assert touched.call_count == 2
    assert heartbeat.read_bytes() == b''
    assert observed == expected


@pytest.mark.parametrize('broken_heartbeat,observation', [(False, True), (True, True), (False, False)])
def test_real_wrapper_heartbeat_is_advisory(reviewer, broken_heartbeat, observation):
    worktree, proc = reviewer(broken_heartbeat=broken_heartbeat, observation=observation)
    marker = json.loads((worktree / 'dispatch.json').read_text())
    assert (marker['stall_seconds'], marker['stall_startup_grace_seconds']) == (60, 120)
    assert marker['pid'] == marker['pgid'] == proc.pid
    (worktree / 'emit').touch()
    (worktree / 'finish').touch()
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode == 0, stderr.decode()
    assert stdout == stderr == b''
    assert json.loads((worktree / 'review.json').read_text()) == RESULT
    assert json.loads((worktree / 'completion.json').read_text())['result_valid'] is True
    if not broken_heartbeat:
        assert (worktree / 'heartbeat').read_bytes() == b''
    if observation:
        expected = reviewer_observability.observe_event_lines([EVENT.encode(), b'{}\n'])
        assert json.loads((worktree / 'review-observation.json').read_text()) == expected


@pytest.mark.parametrize('exit_status', [0, 9])
def test_real_wrapper_drains_after_observer_failure(tmp_path, exit_status):
    """A failed observer must not deadlock the pipe or change engine exit status."""
    worktree = tmp_path / 'pr-7'
    worktree.mkdir()
    command = wrapper_command(worktree)
    bootstrap = (
        f'import sys; sys.path.insert(0, {str(Path(tick.__file__).resolve().parent.parent)!r}); '
        'import engine_wrapper; '
        'engine_wrapper.reviewer_observability.observe_event_stream = '
        'lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("observer failure")); '
        'raise SystemExit(engine_wrapper.main(sys.argv[1:]))'
    )
    engine = (
        'import sys; from pathlib import Path; '
        'sys.stdout.write("x" * (1024 * 1024)); '
        f'Path(sys.argv[sys.argv.index("-o") + 1]).write_text({json.dumps(RESULT)!r}); '
        f'sys.exit({exit_status})'
    )
    result = subprocess.run(
        [sys.executable, '-c', bootstrap, *command[2:],
         '--observation-path', str(worktree / 'review-observation.json'),
         '--', sys.executable, '-c', engine, '-o', 'placeholder'],
        cwd=worktree, capture_output=True, timeout=15, start_new_session=True,
    )
    assert result.returncode == exit_status
    assert result.stdout == b''
    envelope = json.loads((worktree / 'completion.json').read_text())
    assert envelope['exit_status'] == exit_status
    assert envelope['result_valid'] is (exit_status == 0)
    assert json.loads((worktree / 'review-observation.json').read_text()) == reviewer_observability.unavailable_observation()


def test_real_reviewer_progress_lifecycle(reviewer, monkeypatch):
    worktree, proc = reviewer()
    monkeypatch.setattr(tick, 'WT_ROOT', str(worktree.parent))
    # A no-output launch receives the extra grace under its recorded limits.
    marker_path = worktree / 'dispatch.json'
    marker = json.loads(marker_path.read_text())
    with mock.patch('tick.time.time', return_value=marker['start_time'] + 90):
        assert tick.engine_progress_report(str(worktree.parent)) == [('pr-7', 'quiet-1m')]
    assert progress_cli(worktree) == 'pr-7\tquiet-0m\n'
    assert not (worktree / 'heartbeat').exists()

    (worktree / 'emit').touch()
    heartbeat = worktree / 'heartbeat'
    wait_until(heartbeat.exists)
    first = heartbeat.stat().st_mtime
    assert progress_cli(worktree) == 'pr-7\tquiet-0m\n'
    # First output removes startup grace; non-default 60s beats shipped 1800s.
    old = time.time() - 90
    os.utime(heartbeat, (old, old))
    assert progress_cli(worktree) == 'pr-7\tSTALLED-1m\n'
    with mock.patch.object(tick, '_report_engine_stalled') as report, \
            mock.patch('process_control.get_process_start_time', return_value=marker['start_time']):
        assert tick._scan_stalled_engines() is True
        assert tick._scan_stalled_engines() is False
        assert report.call_args.kwargs['source'] == 'heartbeat'
        assert report.call_args.kwargs['marker']['stage'] == 'review'
        report.assert_called_once()

    marker_path.write_text(json.dumps({**marker, 'start_time': marker['start_time'] - 3600}))
    assert progress_cli(worktree) == 'pr-7\tquiet-1m\n'
    marker_path.write_text(json.dumps({**marker, 'deadline': time.time() - 1}))
    assert progress_cli(worktree) == 'pr-7\tquiet-1m\n'
    marker_path.write_text(json.dumps(marker))
    # A later event advances the mtime after the real five-second throttle.
    time.sleep(max(0, first + 5.1 - time.time()))
    (worktree / 'finish').touch()
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode == 0, stderr.decode()
    assert heartbeat.stat().st_mtime > first
    assert stdout == stderr == b''
    os.utime(heartbeat, (old, old))
    assert progress_cli(worktree) == 'pr-7\tquiet-1m\n'
    # Use review.json even for legacy wrappers; result.json is absent.
    assert tick._classify_marker(str(worktree), marker, result_file='review.json')[0] == 'complete'
    with mock.patch.object(tick, '_classify_marker', wraps=tick._classify_marker) as classify:
        assert tick._stalled_engine_identity(str(worktree), marker) is None
        assert classify.call_args.kwargs['result_file'] == 'review.json'


@pytest.mark.parametrize('deadline_key', [
    'implementer_timeout_seconds', 'fixer_timeout_seconds', 'review_timeout_seconds',
])
@pytest.mark.parametrize('deadline', [900, 1200])
def test_real_startup_rejects_stall_at_or_above_each_deadline(only_repo, deadline_key, deadline):
    repo, env = only_repo
    config = Path(env['PR_PIPELINE_CONFIG'])
    config.write_text(config.read_text() + f'engine_stall_seconds=1200\n{deadline_key}={deadline}\n')
    write_items(repo, {})
    result = run_tick(repo, env)
    assert result.returncode == 3, result.stderr
    assert 'engine_stall_seconds (1200)' in result.stderr
    assert f'{deadline_key} ({deadline})' in result.stderr
    assert not list((repo / '.claude/worktrees').glob('*/dispatch.json'))
    assert not (repo / 'engine-started').exists()


@pytest.mark.parametrize('arguments', [[], ['pr:7'], ['--only', 'pr:7']])
def test_real_dispatch_supplies_reviewer_heartbeat_and_limits(loop_repo, arguments):
    repo, env = loop_repo
    env['LOOP_TEST_STAGE'] = 'review'
    env['PR_PIPELINE_PYTHON'] = sys.executable
    (repo / 'label').write_text('ai:implemented')
    config = Path(env['PR_PIPELINE_CONFIG'])
    config.write_text(config.read_text() + 'review_timeout_seconds=2400\nengine_stall_seconds=900\nengine_stall_startup_grace_seconds=60\n')
    bin_dir = Path(env['PATH'].split(os.pathsep)[0])
    (bin_dir / 'git').write_text(f'#!{sys.executable}\n' + GIT_SHIM.replace('("main", "fix")', '("main", "fix", "pull/7/head")'))
    (bin_dir / 'codex').write_text(f'#!{sys.executable}\n' + DISPATCH_ENGINE)
    worktree = repo / '.claude/worktrees/pr-7'
    result = run_tick(repo, env, *arguments)
    try:
        assert result.returncode == 0, result.stderr
        wait_until(lambda: (worktree / 'heartbeat').exists())
        marker = json.loads((worktree / 'dispatch.json').read_text())
        assert (marker['stall_seconds'], marker['stall_startup_grace_seconds']) == (900, 60)
        assert tick.pipeline.valid_marker(marker)
        assert 2350 < marker['deadline'] - time.time() <= 2400
        assert (worktree / 'heartbeat').read_bytes() == b''
        assert progress_cli(worktree) == 'pr-7\tquiet-0m\n'
        assert (repo / 'label').read_text() == 'ai:reviewing'
        assert not (repo / 'unexpected').exists()
    finally:
        if worktree.exists():
            (worktree / 'finish').touch()
            wait_until(lambda: (worktree / 'completion.json').exists())


# --- Helpers ---

RESULT = {'verdict': 'ready_for_human', 'findings': []}
EVENT = json.dumps({'type': 'item.completed', 'item': {
    'type': 'command_execution', 'command': 'cat .agents/skills/example/SKILL.md',
    'exit_code': 0, 'aggregated_output': 'private candidate content',
}}) + '\n'
ENGINE = f'''
import sys, time
from pathlib import Path
Path('started').touch()
while not Path('emit').exists(): time.sleep(0.01)
sys.stdout.write({EVENT!r}); sys.stdout.flush()
while not Path('finish').exists(): time.sleep(0.01)
print('{{}}', flush=True)
Path(sys.argv[sys.argv.index('-o') + 1]).write_text({json.dumps(RESULT)!r})
'''


def wrapper_command(worktree):
    return [sys.executable, str(Path(__file__).resolve().parents[1] / 'engine_wrapper.py'), 'run',
            '--dispatch-id', 'd-heartbeat', '--marker-path', str(worktree / 'dispatch.json'),
            '--result-path', str(worktree / 'review.json'), '--envelope-path', str(worktree / 'completion.json'),
            '--heartbeat', str(worktree / 'heartbeat'), '--stall-seconds', '60',
            '--stall-startup-grace-seconds', '120', '--engine', 'codex', '--stage', 'review',
            '--pr-number', '7', '--deadline', str(time.time() + 3600), '--base-ref', 'main',
            '--base-sha', 'b1', '--head-sha', 'h1', '--pre-label', 'ai:implemented']


def wait_until(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail('reviewer subprocess did not reach the expected state')


def progress_cli(worktree):
    result = subprocess.run([sys.executable, str(Path(tick.__file__)), 'engine-progress',
                             '--wt-root', str(worktree.parent)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


DISPATCH_ENGINE = """
import sys, time
from pathlib import Path
if sys.argv[1:] == ['--version']:
    print('codex-cli 0.142.5')
    sys.exit(0)
assert sys.argv[1] == 'exec'
assert '--json' in sys.argv and sys.argv[sys.argv.index('-s') + 1] == 'read-only'
print('{}', flush=True)
while not Path('finish').exists(): time.sleep(0.01)
Path(sys.argv[sys.argv.index('-o') + 1]).write_text('{}')
"""
