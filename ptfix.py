"""Sandbox-compat shim for the test run.

pytest's temp-dir machinery creates "<prefix>current" *symlinks*, which this
sandbox denies (no SeCreateSymbolicLinkPrivilege); the failed symlink attempts
then poison the whole basetemp with access-denied. Neutralise the symlink bits
and the aggressive cleanups so the suite stays runnable. Test semantics are
unchanged: numbered dirs and the tmp_path fixture still work.
"""
import _pytest.pathlib as _pl
import _pytest.tmpdir as _tmp


def _noop_force_symlink(*args, **kwargs):
    return None


def _safe_cleanup_dead_symlinks(*args, **kwargs):
    try:
        return _pl.__dict__["_cleanup_dead_symlinks_orig"](*args, **kwargs)
    except OSError:
        return None


if not hasattr(_pl, "_sandbox_patched"):
    _pl._sandbox_patched = True
    _pl._cleanup_dead_symlinks_orig = _pl.cleanup_dead_symlinks
    _pl.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks
    _pl._force_symlink = _noop_force_symlink
    if hasattr(_tmp, "cleanup_dead_symlinks"):
        _tmp.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks
