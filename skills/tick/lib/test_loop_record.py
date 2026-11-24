"""Loop evidence, with offline proof through the shipped CLI."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
import tick
from test_smoke_loop import loop_repo, wait_for, cadence_lines  # noqa: F401


@pytest.fixture
def record_path(tmp_path):
    path = tmp_path / '.tick-state/loop.json'
    path.parent.mkdir()
    return path


@pytest.fixture
def record():
    return {
        'schema_version': 1, 'invocation_id': 'test-invocation', 'pid': 123,
        'process_start_time': 100.0, 'recorded_at': 200.0,
        'host': tick.platform.node(), 'scope': {'mode': 'full', 'targets': []},
    }


@pytest.mark.parametrize(('start', 'proc_state', 'alive', 'state', 'reason'), [
    (100.0, 'S', True, 'running', 'identity_verified'),
    (100.0, 'R+', True, 'running', 'identity_verified'),
    (101.0, 'S', True, 'not_running', 'identity_mismatch'),
    (None, None, False, 'not_running', 'process_dead'),
    (None, None, True, 'unknown', 'identity_unreadable'),
    # A killed child that its parent has not reaped keeps its PID and start time.
    (100.0, 'Z', True, 'not_running', 'process_zombie'),
    (100.0, 'Z+', True, 'not_running', 'process_zombie'),
    (100.0, 'X', True, 'not_running', 'process_zombie'),
    # Verified identity without readable state is not proof of either outcome.
    (100.0, None, True, 'unknown', 'process_state_unreadable'),
    # The process can exit between the state and identity reads.
    (100.0, None, False, 'not_running', 'process_dead'),
])
def test_classifier_identity_evidence(record_path, record, start, proc_state, alive, state, reason):
    record_path.write_text(json.dumps(record))
    with mock.patch.object(tick.process_control, 'get_process_start_time', return_value=start), \
         mock.patch.object(tick.process_control, 'get_process_state', return_value=proc_state), \
         mock.patch.object(tick.process_control, 'pid_is_alive', return_value=alive):
        result = tick.classify_loop_process(str(record_path.parents[1]))
    assert result['schema_version'] == 1
    assert result['state'] == state
    assert result['reason'] == reason
    assert result['record_present'] is True
    assert result['source_health']['record'] == 'readable'
    assert result['observed_at'] > 0
    assert result['record'] == record


def test_missing_record_is_unavailable_without_creating_state(tmp_path):
    result = tick.classify_loop_process(str(tmp_path))
    assert result['state'] == 'unavailable'
    assert result['record_present'] is False
    assert result['reason'] == 'record_missing'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('payload', ['{', '[]', '{}', 'null', '{"schema_version":2}'])
def test_malformed_record_is_present_but_unavailable(record_path, payload):
    record_path.write_text(payload)
    result = tick.classify_loop_process(str(record_path.parents[1]))
    assert result['state'] == 'unavailable'
    assert result['record_present'] is True
    assert result['reason'] == 'record_malformed'


@pytest.mark.parametrize(('key', 'value'), [
    ('pid', True), ('pid', 0), ('pid', 1.5), ('pid', 10**100),
    ('process_start_time', float('nan')), ('process_start_time', True),
    ('recorded_at', float('inf')), ('host', ''), ('invocation_id', ''),
    ('schema_version', True), ('scope', {'mode': 'only', 'targets': []}),
])
def test_invalid_record_fields_are_unavailable(record_path, record, key, value):
    record[key] = value
    record_path.write_text(json.dumps(record))
    assert tick.classify_loop_process(str(record_path.parents[1]))['state'] == 'unavailable'


@pytest.mark.parametrize('presence', [True, None])
def test_unreadable_record_preserves_known_or_unknown_presence(record_path, presence):
    record_path.write_text('{}')
    with mock.patch('builtins.open', side_effect=PermissionError), \
         mock.patch.object(Path, 'stat', side_effect=None if presence else PermissionError):
        result = tick.classify_loop_process(str(record_path.parents[1]))
    assert result['state'] == 'unavailable'
    assert result['record_present'] is presence
    assert result['reason'] == 'record_unreadable'


def test_foreign_host_identity_is_unknown(record_path, record):
    record['host'] += '-other'
    record_path.write_text(json.dumps(record))
    with mock.patch.object(tick.process_control, 'get_process_start_time', side_effect=AssertionError):
        result = tick.classify_loop_process(str(record_path.parents[1]))
    assert result['state'] == 'unknown'
    assert result['reason'] == 'host_mismatch'


def test_classifier_has_no_lock_write_or_signal_delivery(record_path, record):
    record["pid"] = os.getpid()
    record_path.write_text(json.dumps(record))
    before = record_path.read_bytes()
    with mock.patch.object(tick.process_control, 'get_process_start_time', return_value=None), \
         mock.patch.object(tick.os, 'kill', return_value=None) as probe, \
         mock.patch.object(tick.os, 'killpg', side_effect=AssertionError), \
         mock.patch.object(tick.TickLock, 'acquire', side_effect=AssertionError), \
         mock.patch.object(tick.pipeline, 'atomic_write', side_effect=AssertionError), \
         mock.patch.object(tick.os, 'unlink', side_effect=AssertionError), \
         mock.patch.object(tick.os, 'makedirs', side_effect=AssertionError):
        assert tick.classify_loop_process(str(record_path.parents[1]))['state'] == 'unknown'
    probe.assert_called_once_with(record['pid'], 0)  # Existence probe delivers no signal.
    assert record_path.read_bytes() == before


@pytest.mark.parametrize('mode', [[], ['--loop']])
def test_real_cli_record_lifecycle_and_lock_refusal(loop_repo, mode):
    repo, env = loop_repo
    (repo / 'label').write_text('ai:blocked')
    env['LOOP_TEST_STAGE'] = 'fix'
    path = repo / '.claude/worktrees/.tick-state/loop.json'
    stderr = repo / 'loop-record.stderr'
    script = Path(__file__).parents[1] / 'tick.sh'
    proc = None
    try:
        with stderr.open('w') as err:
            proc = subprocess.Popen(['bash', str(script), *mode], cwd=repo, env=env,
                                    stdout=err, stderr=err, start_new_session=True)
            wait_for(lambda: cadence_lines(stderr), proc, stderr, timeout=45)
            if not mode:
                assert proc.wait(timeout=10) == 0
                assert not path.exists(), 'one-shot ticks must never publish loop records'
                return
            record = json.loads(path.read_text())
            assert record['pid'] == proc.pid
            assert record['scope'] == {'mode': 'full', 'targets': []}
            result = read_cli(repo, env)
            assert result['state'] == 'running'
            assert result['record_present'] is True
            before = path.read_bytes()
            contender = subprocess.run(['bash', str(script), '--loop'], cwd=repo, env=env,
                                       capture_output=True, text=True, timeout=30)
            assert contender.returncode == 5, contender.stderr
            assert path.read_bytes() == before
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0
            assert not path.exists()
            assert read_cli(repo, env)['state'] == 'unavailable'
            assert not (repo / 'unexpected').exists()
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_real_sigkill_retains_dead_record_and_next_loop_replaces_it(loop_repo):
    repo, env = loop_repo
    env['LOOP_TEST_STAGE'] = 'fix'
    (repo / 'label').write_text('ai:blocked')
    path = repo / '.claude/worktrees/.tick-state/loop.json'
    script = Path(__file__).parents[1] / 'tick.sh'
    prior = None
    for attempt in range(2):
        stderr = repo / f'kill-{attempt}.stderr'
        with stderr.open('w') as err:
            proc = subprocess.Popen(['bash', str(script), '--loop', '--only=#7,pr:8'],
                                    cwd=repo, env=env, stdout=err, stderr=err)
            try:
                wait_for(lambda: path.exists() and json.loads(path.read_text())['pid'] == proc.pid,
                         proc, stderr, timeout=30)
                record = json.loads(path.read_text())
                assert record['scope'] == {'mode': 'only', 'targets': ['#7', 'pr:8']}
                assert record['invocation_id'] != prior
                assert read_cli(repo, env)['state'] == 'running'
                prior = record['invocation_id']
                proc.kill()
                # Defer reaping: the killed loop stays a zombie with its PID and start time.
                assert wait_without_reaping(lambda: zombie(proc.pid)), 'killed loop never became a zombie'
                unreaped = read_cli(repo, env)
                assert (unreaped['state'], unreaped['reason']) == ('not_running', 'process_zombie')
                assert unreaped['source_health'] == {'record': 'readable', 'process': 'zombie'}
                assert json.loads(path.read_text()) == record
                proc.wait(timeout=10)
                assert path.exists()
                assert read_cli(repo, env)['state'] == 'not_running'
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)


def test_cleanup_is_owned_and_precedes_unlock(monkeypatch, tmp_path):
    path = Path(tick.WT_ROOT) / '.tick-state/loop.json'
    monkeypatch.setattr(tick, 'repo_root', lambda: str(tmp_path))
    monkeypatch.setattr(tick, '_start_wake_watcher', lambda *args: None)
    original_release = tick.TickLock.release
    def release(lock):
        assert not path.exists()
        original_release(lock)
    def body(*args, **kwargs):
        assert tick.classify_loop_process()['state'] == 'running'
        assert callable(signal.getsignal(signal.SIGTERM))
        return tick.TickOutcome(3, None)
    monkeypatch.setattr(tick, '_run_single_tick_locked', body)
    monkeypatch.setattr(tick.TickLock, 'release', release)
    assert tick.main(loop=True) == 3


def test_cleanup_preserves_replacement_record(monkeypatch):
    path = Path(tick.WT_ROOT) / '.tick-state/loop.json'
    def body(*args, **kwargs):
        record = json.loads(path.read_text())
        record['invocation_id'] = 'replacement'
        path.write_text(json.dumps(record))
        return tick.TickOutcome(3, None)
    monkeypatch.setattr(tick, '_start_wake_watcher', lambda *args: None)
    monkeypatch.setattr(tick, '_run_single_tick_locked', body)
    assert tick._run_loop_locked(('pr', 117), '', '') == 3
    assert json.loads(path.read_text())['invocation_id'] == 'replacement'
    assert json.loads(path.read_text())['scope'] == {'mode': 'target', 'targets': ['pr:117']}


@pytest.mark.parametrize('failure', ['identity', 'publication', 'watcher'])
def test_startup_failure_restores_handlers_and_cleans_only_published_record(monkeypatch, failure):
    path = Path(tick.WT_ROOT) / '.tick-state/loop.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('stale record')
    previous = signal.getsignal(signal.SIGTERM)
    if failure == 'identity':
        monkeypatch.setattr(tick.process_control, 'get_process_start_time', lambda _: None)
    elif failure == 'publication':
        monkeypatch.setattr(tick.pipeline, 'atomic_write', mock.Mock(side_effect=OSError('full')))
    else:
        monkeypatch.setattr(tick, '_start_wake_watcher', mock.Mock(side_effect=RuntimeError('start failed')))
    with mock.patch.object(tick, '_run_single_tick_locked', side_effect=AssertionError):
        assert tick._run_loop_locked(None, '', '') == tick.TICK_CRASH_EXIT_CODE
    assert signal.getsignal(signal.SIGTERM) == previous
    if failure == 'watcher':
        assert not path.exists()
    else:
        assert path.read_text() == 'stale record'


def test_atomic_publication_exposes_only_complete_records(monkeypatch):
    path = Path(tick.WT_ROOT) / '.tick-state/loop.json'
    path.parent.mkdir(parents=True)
    path.write_text('old evidence')
    replace = os.replace
    def inspect_replace(source, destination):
        assert path.read_text() == 'old evidence'
        pending = json.loads(Path(source).read_text())
        assert pending['pid'] == os.getpid()
        assert callable(signal.getsignal(signal.SIGTERM))
        replace(source, destination)
        assert json.loads(path.read_text()) == pending
    monkeypatch.setattr(tick.os, 'replace', inspect_replace)
    monkeypatch.setattr(tick, '_start_wake_watcher', lambda *args: None)
    monkeypatch.setattr(tick, '_run_single_tick_locked', lambda *args, **kwargs: tick.TickOutcome(3, None))
    assert tick._run_loop_locked(None, '', '') == 3
    assert not path.exists()


def test_stop_during_publication_cleans_before_any_tick(monkeypatch):
    publish = tick.pipeline.atomic_write
    def interrupt(path, data):
        publish(path, data)
        signal.raise_signal(signal.SIGTERM)
    monkeypatch.setattr(tick.pipeline, 'atomic_write', interrupt)
    monkeypatch.setattr(tick, '_start_wake_watcher', lambda *args: None)
    with mock.patch.object(tick, '_run_single_tick_locked', side_effect=AssertionError):
        assert tick._run_loop_locked(None, '', '') == 0
    assert not (Path(tick.WT_ROOT) / '.tick-state/loop.json').exists()


def test_one_shot_never_calls_record_writer(monkeypatch, tmp_path):
    monkeypatch.setattr(tick, 'repo_root', lambda: str(tmp_path))
    monkeypatch.setattr(tick, '_run_single_tick_locked', lambda *args, **kwargs: tick.TickOutcome(0, None))
    with mock.patch.object(tick, '_publish_loop_record', side_effect=AssertionError):
        assert tick.main() == 0


def test_failed_identity_probe_cannot_prove_death(record_path, record):
    record_path.write_text(json.dumps(record))
    with mock.patch.object(tick.process_control, 'get_process_start_time', return_value=None), \
         mock.patch.object(tick.process_control, 'pid_is_alive', side_effect=OSError):
        result = tick.classify_loop_process(str(record_path.parents[1]))
    assert result['state'] == 'unknown'
    assert result['record_present'] is True


def test_real_startup_refusal_removes_loop_record(loop_repo):
    repo, env = loop_repo
    Path(env['PR_PIPELINE_CONFIG']).write_text('not valid toml')
    result = subprocess.run(['bash', str(Path(__file__).parents[1] / 'tick.sh'), '--loop'],
                            cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 3, result.stderr
    assert not (repo / '.claude/worktrees/.tick-state/loop.json').exists()
    assert not (repo / 'calls.jsonl').exists(), 'startup refusal must not reach GitHub/engines'


# --- Helpers ---


def wait_without_reaping(predicate, timeout=10.0):
    # Popen.poll/wait would reap the child and erase the state under test.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def zombie(pid):
    probe = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
    return probe.stdout.strip().startswith('Z')


def read_cli(repo, env):
    proc = subprocess.run([sys.executable, str(Path(tick.__file__)), 'loop-status'],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)
