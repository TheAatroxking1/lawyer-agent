import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lawyer_agent.infrastructure.documents import word_driver
from lawyer_agent.infrastructure.documents.conversion_paths import ConversionPaths
from lawyer_agent.infrastructure.documents.word_conversion import WordConversionError


@pytest.fixture(autouse=True)
def unavailable_word_host(monkeypatch):
    # Replace only the driver's module bindings, never the shared os.name or
    # shutil.which. Path/pytest still use the actual host's filesystem semantics.
    monkeypatch.setattr(word_driver, "os", SimpleNamespace(
        **(vars(word_driver.os) | {"name": "posix"}),
    ))
    monkeypatch.setattr(word_driver, "shutil", SimpleNamespace(
        **(vars(word_driver.shutil) | {"which": lambda _name: None}),
    ))


def test_batch_driver_available():
    assert hasattr(word_driver, "BatchPowerShellWordConverter")


def make_driver(tmp_path, monkeypatch, maximum=25):
    cls = word_driver.BatchPowerShellWordConverter
    source = tmp_path / "source"
    source.mkdir()
    doc = source / "source.doc"
    doc.write_bytes(b"synthetic")
    script = Path(__file__).parents[3] / "scripts/word_batch_worker.ps1"

    def synthetic_host_setup(
        self, *, source_root, output_root, work_root, worker_script, timeout_seconds=120,
    ):
        # The batch constructor and lifecycle are real. Only the base class's
        # Windows installation probe is replaced with synthetic dependency state.
        self._paths = ConversionPaths(source_root, output_root)
        self._root = self._paths.output(work_root)
        self._root.mkdir(parents=True)
        self._script = worker_script
        self._executable = "synthetic-powershell"
        self._timeout = timeout_seconds

    monkeypatch.setattr(word_driver.PowerShellWordConverter, "__init__", synthetic_host_setup)
    value = cls(source_root=source, output_root=tmp_path / "out",
                work_root=tmp_path / "out/work", worker_script=script,
                max_files=maximum)
    events = []

    class Process:
        def poll(self):
            return None

        def wait(self, timeout):
            value._control.write_text(json.dumps({"nonce": value._nonce,
                "setting_changed": False, "status": "closed"}), encoding="utf-8")
            events.append("wait")
            return 0

        def kill(self):
            events.append("kill")

    def launch(command):
        events.append("start")
        return Process()

    def exchange(payload):
        events.append(payload["action"])
        value._control.write_text(json.dumps({"nonce": value._nonce,
            "setting_changed": payload["action"] != "close", "status": "closed"}),
            encoding="utf-8")
        return {"nonce": value._nonce, "sequence": payload["sequence"],
                "status": "converted", "converter_version": "16"}

    monkeypatch.setattr(value, "_launch", launch)
    monkeypatch.setattr(value, "_exchange", exchange)
    return value, doc, events


def test_two_files_reuse_one_worker_and_finally_close(tmp_path, monkeypatch):
    value, doc, events = make_driver(tmp_path, monkeypatch)
    with value:
        value(doc, value._paths.output_root / "one.docx")
        value(doc, value._paths.output_root / "two.docx")
    assert events == ["start", "convert", "convert", "close", "wait"]


def test_limit_restarts_only_after_close(tmp_path, monkeypatch):
    value, doc, events = make_driver(tmp_path, monkeypatch)
    with value:
        for index in range(26):
            value(doc, value._paths.output_root / f"{index}.docx")
    assert events.count("start") == 2
    assert events[26:28] == ["wait", "start"]


@pytest.mark.parametrize("failure", [TimeoutError, KeyboardInterrupt, WordConversionError])
def test_failure_stops_worker_and_recovers_before_next(tmp_path, monkeypatch, failure):
    value, doc, events = make_driver(tmp_path, monkeypatch)
    def fail(payload):
        raise failure("word_conversion_failed")
    monkeypatch.setattr(value, "_exchange", fail)
    monkeypatch.setattr(value, "_recover", lambda *args: events.append("recover"))
    with pytest.raises(failure):
        value(doc, value._paths.output_root / "one.docx")
    assert events == ["start", "kill", "wait", "recover"]
    assert value._process is None


def test_unused_context_does_not_start_word(tmp_path, monkeypatch):
    value, _, events = make_driver(tmp_path, monkeypatch)
    with value:
        pass
    assert not events


@pytest.mark.parametrize("response", ["not json", '{"nonce":"wrong","sequence":1}',
                                     '{"nonce":"nonce","sequence":99}'])
def test_bad_response_is_rejected(tmp_path, monkeypatch, response):
    value, _, _ = make_driver(tmp_path, monkeypatch)
    value._nonce = "nonce"
    folder = value._root / "nonce"
    folder.mkdir()
    value._control = folder / "control.json"
    (folder / "response-1.json").write_text(response, encoding="utf-8")
    with pytest.raises(WordConversionError, match="word_conversion_failed"):
        word_driver.BatchPowerShellWordConverter._exchange(value,
            {"nonce": "nonce", "sequence": 1, "action": "convert"})


def test_restore_failure_blocks_reuse(tmp_path, monkeypatch):
    value, doc, events = make_driver(tmp_path, monkeypatch)
    def fail(*args):
        raise WordConversionError("word_settings_restore_failed")
    monkeypatch.setattr(value, "_exchange", fail)
    monkeypatch.setattr(value, "_recover", fail)
    for _ in range(2):
        with pytest.raises(WordConversionError):
            value(doc, value._paths.output_root / "target.docx")
    assert events.count("start") == 1


def idle_race(tmp_path, monkeypatch, *, returncode=0, status="closed",
              restored=True, accepted=1, retire_all=False):
    value, doc, events = make_driver(tmp_path, monkeypatch)
    monkeypatch.setattr(value, "_exchange",
                        word_driver.BatchPowerShellWordConverter._exchange.__get__(value))
    monkeypatch.setattr(value, "_recover", lambda *args: events.append("recover"))

    class Process:
        def __init__(self, retiring):
            self.retiring = retiring
            self.exited = False

        def poll(self):
            if self.exited:
                return returncode
            request = value._control.parent / f"request-{value._sequence}.json"
            if not request.exists():
                return None
            payload = json.loads(request.read_text(encoding="utf-8"))
            if self.retiring:
                # Alive on preflight poll, but request arrives after idle decision.
                value._control.write_text(json.dumps({"nonce": value._nonce,
                    "setting_changed": not restored, "status": status,
                    "accepted_sequence": accepted}), encoding="utf-8")
                self.exited = True
                return returncode
            response = {"nonce": value._nonce, "sequence": value._sequence,
                "status": "converted" if payload["action"] == "convert" else "closed",
                "converter_version": "16"}
            (request.parent / f"response-{value._sequence}.json").write_text(
                json.dumps(response), encoding="utf-8")
            return None

        def wait(self, timeout):
            events.append("wait")
            if not self.retiring:
                value._control.write_text(json.dumps({"nonce": value._nonce,
                    "setting_changed": False, "status": "closed",
                    "accepted_sequence": value._sequence}), encoding="utf-8")
            return returncode

        def kill(self):
            self.exited = True
            events.append("kill")

    def launch(command):
        events.append("start")
        return Process(retire_all)

    monkeypatch.setattr(value, "_launch", launch)
    value._start()
    value._process = Process(True)
    value._sequence = 1
    return value, doc, events


@pytest.mark.parametrize("maximum", [0, 26])
def test_synthetic_fixture_keeps_actual_batch_bounds(tmp_path, monkeypatch, maximum):
    with pytest.raises(ValueError, match="invalid_batch_bounds"):
        make_driver(tmp_path, monkeypatch, maximum=maximum)


def test_real_constructor_still_requires_word_host(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="requires Windows PowerShell"):
        word_driver.BatchPowerShellWordConverter(
            source_root=source, output_root=tmp_path / "out",
            work_root=tmp_path / "out/work",
            worker_script=Path(__file__).parents[3] / "scripts/word_batch_worker.ps1",
        )


def test_idle_exit_between_poll_and_request_retries_once(tmp_path, monkeypatch):
    value, doc, events = idle_race(tmp_path, monkeypatch)
    assert value(doc, value._paths.output_root / "target.docx").endswith("/batch-v1")
    value.close()
    assert events.count("start") == 2
    assert "recover" not in events
    assert not list(value._root.glob("*/input-*.doc"))


def test_close_accepts_proven_unaccepted_idle_exit(tmp_path, monkeypatch):
    value, _, events = idle_race(tmp_path, monkeypatch)
    value.close()
    assert value._process is None
    assert events.count("start") == 1
    assert "recover" not in events


@pytest.mark.parametrize("override", [
    {"returncode": 1}, {"status": "failed"}, {"restored": False},
    {"accepted": 2}, {"accepted": None}, {"accepted": True},
])
def test_idle_retry_requires_all_exit_proofs(tmp_path, monkeypatch, override):
    value, doc, events = idle_race(tmp_path, monkeypatch, **override)
    with pytest.raises(WordConversionError):
        value(doc, value._paths.output_root / "target.docx")
    assert events.count("start") == 1
    assert events.count("recover") == 1


def test_idle_retry_is_bounded_to_one(tmp_path, monkeypatch):
    value, doc, events = idle_race(tmp_path, monkeypatch, accepted=0, retire_all=True)
    value._sequence = 0
    with pytest.raises(WordConversionError):
        value(doc, value._paths.output_root / "target.docx")
    assert events.count("start") == 2


@pytest.mark.parametrize("override", [{"returncode": 1}, {"status": "failed"}])
def test_close_rejects_failed_already_exited_worker(tmp_path, monkeypatch, override):
    value, _, events = idle_race(tmp_path, monkeypatch, **override)
    value._process.exited = True
    value._control.write_text(json.dumps({"nonce": value._nonce,
        "setting_changed": False, "status": override.get("status", "closed")}),
        encoding="utf-8")
    with pytest.raises(WordConversionError):
        value.close()
    assert events.count("recover") == 1


def test_final_response_written_during_exit_poll_is_consumed(tmp_path, monkeypatch):
    value, _, _ = make_driver(tmp_path, monkeypatch)
    value._start()

    class Process:
        def poll(self):
            (value._control.parent / "response-1.json").write_text(json.dumps({
                "nonce": value._nonce, "sequence": 1, "status": "converted",
                "converter_version": "16"}), encoding="utf-8")
            return 0

    value._process = Process()
    result = word_driver.BatchPowerShellWordConverter._exchange(value,
        {"nonce": value._nonce, "sequence": 1, "action": "convert"})
    assert result["status"] == "converted"
