from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from armory.config import load_config
from armory.http import Fetcher
from armory.state import StateConflictError, StateStore


def _save_section(path, section, barrier):
    state = StateStore(path)
    state.data[section] = [section]
    barrier.wait(timeout=10)
    state.save()


def test_concurrent_writers_preserve_independent_sections(tmp_path):
    import multiprocessing

    path = tmp_path / "state.json"
    StateStore(path).save()
    with multiprocessing.Manager() as manager:
        barrier = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_save_section, path, key, barrier) for key in ("first", "second")]
            for future in futures:
                future.result(timeout=20)
    result = StateStore(path)
    assert result.data["first"] == ["first"]
    assert result.data["second"] == ["second"]


def test_stale_save_preserves_other_writer_and_refreshes_snapshot(tmp_path):
    path = tmp_path / "state.json"
    worker, editor = StateStore(path), StateStore(path)
    editor.data["preferences"] = ["new preference"]
    editor.save()
    worker.data["progress"] = {"cursor": 1}
    worker.save()
    assert worker.data["preferences"] == ["new preference"]
    worker.data["progress"]["cursor"] = 2
    worker.save()
    assert StateStore(path).data["progress"] == {"cursor": 2}
    assert StateStore(path).data["preferences"] == ["new preference"]


def test_competing_edits_fail_without_overwriting(tmp_path):
    path = tmp_path / "state.json"
    first, second = StateStore(path), StateStore(path)
    first.data["preferences"] = ["first"]
    second.data["preferences"] = ["second"]
    first.save()
    with pytest.raises(StateConflictError, match="reload and retry"):
        second.save()
    assert StateStore(path).data["preferences"] == ["first"]
    fresh = StateStore(path)
    fresh.data["preferences"] = ["retry"]
    fresh.save()  # The failed save released its lock.


def test_failed_replace_preserves_state_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = StateStore(path)
    state.save()
    original = path.read_bytes()
    state.data["preferences"] = ["new"]

    def fail_replace(*args):
        raise OSError("simulated replacement failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError):
            state.save()
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
    state.save()
    assert StateStore(path).data["preferences"] == ["new"]


@pytest.mark.parametrize("driver", ["http", "impersonate"])
def test_get_forwards_requested_timeout(driver):
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, text="ok", url=url)

    fetcher = Fetcher.__new__(Fetcher)
    fetcher.driver = driver
    fetcher.headers = {}
    fetcher._session = SimpleNamespace(get=get)
    fetcher.get("https://example.invalid", timeout=0.25)
    assert calls[0]["timeout"] == 0.25


@pytest.mark.parametrize("configured", [None, "custom.json", "/tmp/absolute-state.json"])
def test_state_path_is_anchored_to_config(tmp_path, monkeypatch, configured):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(f"state_path: {configured}\n" if configured else "{}\n")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(config_path)
    expected = Path(configured) if configured else Path("armory.state.json")
    if not expected.is_absolute():
        expected = config_dir / expected
    assert Path(cfg.state_path) == expected


@pytest.mark.parametrize("command", [
    ["status"], ["keywords", "list"], ["keywords", "add", "example"],
    ["keywords", "remove", "example"], ["rules", "list"],
    ["rules", "add", "example"], ["rules", "remove", "example"],
])
def test_local_commands_do_not_initialize_clients(tmp_path, monkeypatch, command):
    from armory import cli

    config = tmp_path / "config.yaml"
    config.write_text("{}\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("Local command initialized clients")

    monkeypatch.setattr(cli, "_boot", forbidden)
    monkeypatch.setattr(cli, "build_adapters", forbidden)
    monkeypatch.setattr(cli, "Alerters", forbidden)
    monkeypatch.setattr(cli, "load_env", lambda: None)
    result = CliRunner().invoke(cli.app, [*command, "--config", str(config)])
    assert result.exit_code == 0, result.output or repr(result.exception)
