"""Bounded Windows Word worker with an ownership and preference recovery journal."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from lawyer_agent.infrastructure.documents.conversion_paths import (
    ConversionPaths,
    unredirected_path,
)
from lawyer_agent.infrastructure.documents.source_format import is_legacy_word_source
from lawyer_agent.infrastructure.documents.word_conversion import WordConversionError


class PowerShellWordConverter:
    def __init__(
        self,
        *,
        work_root: Path,
        source_root: Path,
        output_root: Path,
        worker_script: Path,
        timeout_seconds: int = 120,
    ) -> None:
        self._paths = ConversionPaths(source_root, output_root)
        self._root = self._paths.output(work_root)
        if self._root == self._paths.output_root:
            raise ValueError("unsafe_work_root")
        if timeout_seconds < 10 or timeout_seconds > 600:
            raise ValueError("conversion timeout must be between 10 and 600 seconds")
        executable = shutil.which("powershell.exe")
        if os.name != "nt" or executable is None or not worker_script.is_file():
            raise ValueError("Word conversion requires Windows PowerShell and the worker script")
        self._executable = executable
        self._script = unredirected_path(worker_script)
        self._timeout = timeout_seconds
        self._work(self._root).mkdir(parents=True, exist_ok=True)

    @property
    def fingerprint(self) -> str:
        payload = {
            "protocol": "controlled-word-v2",
            "worker_sha256": hashlib.sha256(self._script.read_bytes()).hexdigest(),
            "timeout_seconds": self._timeout,
            "powershell_executable": self._executable,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _work(self, path: Path) -> Path:
        self._paths.output(self._root)
        candidate = self._paths.output(path)
        if not candidate.is_relative_to(self._root):
            raise ValueError("unsafe_work_path")
        return candidate

    def _command(self, action: str, control: Path, nonce: str) -> list[str]:
        return [
            self._executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-STA",
            "-File",
            str(self._script),
            "-Action",
            action,
            "-ControlFile",
            str(control),
            "-Nonce",
            nonce,
        ]

    def _run(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - executable is resolved, arguments are separate
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _state(self, control: Path, nonce: str) -> dict[str, Any]:
        try:
            for child in self._work(control.parent).iterdir():
                self._work(child)
            state = json.loads(self._work(control).read_text(encoding="utf-8-sig"))
            if not isinstance(state, dict) or state.get("nonce") != nonce:
                raise ValueError("invalid journal")
            return state
        except (OSError, ValueError) as exc:
            raise WordConversionError("word_ownership_unverified") from exc

    def _recover(self, control: Path, nonce: str) -> None:
        self._state(control, nonce)
        try:
            result = self._run(self._command("Recover", control, nonce), 60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WordConversionError("word_settings_restore_failed") from exc
        if result.returncode != 0:
            raise WordConversionError("word_settings_restore_failed")
        if self._state(control, nonce).get("setting_changed") is not False:
            raise WordConversionError("word_settings_restore_failed")

    def recover_pending(self) -> None:
        for folder in self._work(self._root).iterdir():
            self._work(folder)
            if folder.is_dir() and not (folder / "control.json").is_file():
                raise WordConversionError("word_ownership_unverified")
        for control in sorted(self._root.glob("*/control.json")):
            if not control.resolve().is_relative_to(self._root):
                raise WordConversionError("word_ownership_unverified")
            nonce = control.parent.name
            state = self._state(control, nonce)
            if state.get("setting_changed") is not False:
                self._recover(control, nonce)

    def __call__(self, source: Path, target: Path) -> str:
        source = self._paths.source(source)
        target = self._paths.output(target)
        if not source.is_file() or not is_legacy_word_source(source):
            raise ValueError("source must be DOC")
        self.recover_pending()
        nonce = uuid.uuid4().hex
        folder = self._root / nonce
        self._work(folder).mkdir()
        control = self._work(folder / "control.json")
        copy = self._work(folder / "input.doc")
        shutil.copyfile(self._paths.source(source), self._work(copy))
        self._paths.output(target)
        self._work(control)
        command = self._command("Convert", control, nonce)
        command.extend(["-Source", str(copy), "-Destination", str(target)])
        cleanup = False
        try:
            try:
                result = self._run(command, self._timeout)
            except subprocess.TimeoutExpired as exc:
                # subprocess.run has stopped the worker; recovery can only stop
                # the Word PID with the journal's matching executable/start time.
                self._recover(control, nonce)
                cleanup = True
                raise TimeoutError("conversion timed out") from exc
            except BaseException:
                self._recover(control, nonce)
                cleanup = True
                raise
            state = self._state(control, nonce)
            if state.get("setting_changed") is not False:
                self._recover(control, nonce)
                cleanup = True
                raise WordConversionError("word_conversion_failed")
            cleanup = True
            if result.returncode != 0 or state.get("status") != "converted":
                if state.get("status") == "rejected" and state.get("error_code") == "word_busy":
                    raise WordConversionError("word_busy")
                raise WordConversionError("word_conversion_failed")
            version = state.get("converter_version")
            if not isinstance(version, str) or not version.strip():
                raise WordConversionError("word_conversion_failed")
            return "ms-word-" + version + "/worker-v1"
        finally:
            if cleanup:
                self._work(copy).unlink(missing_ok=True)


class _UnacceptedIdleExit(Exception):
    """A clean worker exit whose durable journal proves no request acceptance."""


class BatchPowerShellWordConverter(PowerShellWordConverter):
    """Serial atomic requests to a bounded Word process; explicit close is mandatory."""

    def __init__(self, *, max_files: int = 25, idle_seconds: int = 30, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not 1 <= max_files <= 25 or not 5 <= idle_seconds <= 120:
            raise ValueError("invalid_batch_bounds")
        self._maximum = max_files
        self._idle = idle_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._sequence = 0
        self._nonce = ""
        self._control = self._root / "unstarted"
        self._blocked = False

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            f"word-batch-v1:{super().fingerprint}:{self._maximum}:{self._idle}".encode()
        ).hexdigest()

    def __enter__(self) -> BatchPowerShellWordConverter:
        self.recover_pending()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _launch(self, command: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603 - resolved executable and separate arguments
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _start(self) -> None:
        if self._blocked:
            raise WordConversionError("word_settings_restore_failed")
        self.recover_pending()
        self._nonce = uuid.uuid4().hex
        folder = self._work(self._root / self._nonce)
        folder.mkdir()
        self._control = self._work(folder / "control.json")
        self._sequence = 0
        command = self._command("Convert", self._control, self._nonce)
        command += ["-MaxFiles", str(self._maximum), "-IdleSeconds", str(self._idle)]
        self._process = self._launch(command)

    def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        folder = self._work(self._control.parent)
        request = self._work(folder / f"request-{payload['sequence']}.json")
        temporary = self._work(request.with_suffix(".tmp"))
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(self._work(temporary), self._work(request))
        response = folder / f"response-{payload['sequence']}.json"
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if self._work(response).is_file():
                try:
                    state = json.loads(response.read_text(encoding="utf-8-sig"))
                except (ValueError, OSError) as exc:
                    raise WordConversionError("word_conversion_failed") from exc
                if (not isinstance(state, dict) or state.get("nonce") != self._nonce
                        or state.get("sequence") != payload["sequence"]):
                    raise WordConversionError("word_conversion_failed")
                return state
            returncode = self._process.poll() if self._process is not None else None
            if self._process is None or returncode is not None:
                # A response may have arrived between the first check and poll.
                if self._work(response).is_file():
                    continue
                if returncode == 0:
                    journal = self._state(self._control, self._nonce)
                    accepted = journal.get("accepted_sequence")
                    if (journal.get("status") == "closed"
                            and journal.get("setting_changed") is False
                            and type(accepted) is int
                            and accepted == payload["sequence"] - 1):
                        raise _UnacceptedIdleExit
                raise WordConversionError("word_conversion_failed")
            time.sleep(0.05)
        raise TimeoutError("conversion timed out")

    def _abort(self) -> None:
        process, self._process = self._process, None
        self._blocked = True
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        self._recover(self._control, self._nonce)
        self._blocked = False

    def close(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.poll() is None and self._sequence < self._maximum:
                self._sequence += 1
                try:
                    self._exchange({"nonce": self._nonce, "sequence": self._sequence,
                                    "action": "close"})
                except _UnacceptedIdleExit:
                    pass
            returncode = self._process.wait(timeout=30)
            journal = self._state(self._control, self._nonce)
            if journal.get("setting_changed") is not False:
                raise WordConversionError("word_settings_restore_failed")
            if returncode != 0 or journal.get("status") != "closed":
                raise WordConversionError("word_conversion_failed")
            self._process = None
        except BaseException:
            self._abort()
            raise

    def __call__(self, source: Path, target: Path) -> str:
        source = self._paths.source(source)
        target = self._paths.output(target)
        if not source.is_file() or not is_legacy_word_source(source):
            raise ValueError("source must be DOC")
        for attempt in range(2):
            if self._process is not None and (
                self._sequence >= self._maximum or self._process.poll() is not None
            ):
                self.close()
            if self._process is None:
                self._start()
            self._sequence += 1
            copy = self._work(self._control.parent / f"input-{self._sequence}.doc")
            try:
                shutil.copyfile(source, copy)
                state = self._exchange({"nonce": self._nonce, "sequence": self._sequence,
                    "action": "convert", "source": str(self._work(copy)),
                    "destination": str(self._paths.output(target))})
                version = state.get("converter_version")
                if (state.get("status") != "converted"
                        or not isinstance(version, str) or not version):
                    raise WordConversionError("word_conversion_failed")
                self._work(copy).unlink()
                if self._sequence >= self._maximum:
                    self.close()
                return "ms-word-" + version + "/batch-v1"
            except _UnacceptedIdleExit:
                # _exchange proved normal exit and durable non-acceptance.
                # Reap the process, discard only our unread copy, and retry once.
                self.close()
                self._work(copy).unlink()
                if attempt:
                    raise WordConversionError("word_conversion_failed") from None
            except BaseException:
                if self._process is not None:
                    self._abort()
                raise
        raise AssertionError("unreachable")
