"""The runner must refuse live paths even inside a trusted library prefix."""
from pathlib import Path

import pytest

from scripts.run_offline_tests import OfflineGuard, snapshot


def test_code_snapshot_does_not_copy_private_or_operational_files(tmp_path):
    source, target = tmp_path / 'checkout', tmp_path / 'copy'
    source.mkdir()
    (source / 'monitor.py').write_text('pass', encoding='utf-8')
    (source / 'dispatcher_schedules.json').write_text('{}', encoding='utf-8')
    private_paths = ['portfolio.xlsx', 'credentials.json', 'data/stop_levels.json', 'analysis/synthetic.csv']
    for relative in private_paths:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic private content', encoding='utf-8')
    snapshot(source, target)
    assert (target / 'monitor.py').read_text(encoding='utf-8') == 'pass'
    assert sorted(path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()) == [
        'dispatcher_schedules.json', 'monitor.py']


def test_original_checkout_cannot_be_read_through_broad_library_allowlist(tmp_path):
    source = tmp_path / 'library' / 'checkout'
    guard = OfflineGuard(tmp_path / 'isolated', [tmp_path / 'library'], source, source / 'scripts/run_offline_tests.py')
    with pytest.raises(PermissionError, match='original-checkout-access'):
        guard.path_check(source / 'data/stop_levels.json')
    guard.path_check(source / 'scripts/run_offline_tests.py')
    with pytest.raises(PermissionError, match='original-checkout-access'):
        guard.path_check(source / 'scripts/run_offline_tests.py', writing=True)


@pytest.mark.parametrize('event,args', [
    ('socket.connect', (None, ('synthetic.invalid', 443))),
    ('socket.bind', (None, ('127.0.0.1', 0))),
    ('socket.getaddrinfo', ('synthetic.invalid', 443)),
    ('socket.gethostbyname', ('synthetic.invalid',)),
    ('socket.gethostbyaddr', ('192.0.2.1',)),
    ('socket.getnameinfo', (('192.0.2.1', 443), 0)),
    ('socket.sendto', (None, ('192.0.2.1', 443))),
    ('socket.sendmsg', (None, ('192.0.2.1', 443))),
    ('subprocess.Popen', ('synthetic', [], None, None)),
    ('os.system', ('synthetic',)),
    ('os.posix_spawn', ('synthetic', [], {})),
    ('os.startfile', ('synthetic', 'open')),
])
def test_guard_rejects_external_effects_before_the_call(event, args, tmp_path):
    guard = OfflineGuard(tmp_path, [])
    with pytest.raises(PermissionError):
        guard.audit(event, args)
    assert len(guard.events) == 1


def test_file_operations_stay_inside_scratch_snapshot(tmp_path):
    scratch = tmp_path / 'scratch'
    guard = OfflineGuard(scratch, [])
    guard.path_check(scratch / 'state.json', writing=True)
    with pytest.raises(PermissionError, match='outside-snapshot-write'):
        guard.audit('os.rename', (str(scratch / 'state.json'), str(tmp_path / 'outside.json'), -1, -1))
    with pytest.raises(PermissionError, match='dotenv-file-access'):
        guard.path_check(scratch / '.env')


def test_repository_local_virtualenv_remains_readable_without_allowing_product_data(tmp_path):
    source = tmp_path / 'checkout'
    libraries = source / '.venv' / 'Lib' / 'site-packages'
    guard = OfflineGuard(tmp_path / 'scratch', [libraries], source)
    guard.path_check(libraries / 'synthetic_dependency.py')
    with pytest.raises(PermissionError):
        guard.path_check(source / 'data/stop_levels.json')
    with pytest.raises(PermissionError):
        guard.path_check(libraries / 'synthetic_dependency.py', writing=True)
