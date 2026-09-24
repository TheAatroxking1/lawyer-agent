from pathlib import Path

import pytest


@pytest.mark.parametrize("worker", ["word_conversion_worker.ps1", "word_batch_worker.ps1"])
def test_worker_atomic_journal_and_concurrent_word_guard(worker):
    script = (Path(__file__).parents[3] / "scripts" / worker).read_text(
        encoding="utf-8-sig"
    )
    assert "[System.IO.File]::Replace(" in script
    assert ".Flush($true)" in script
    assert '[System.IO.File]::Replace($taskTemporary, $ControlFile, $null)' not in script
    assert "function Assert-Exclusive" in script
    assert script.count("Assert-Exclusive") >= 4
    assert "word_ownership_unverified" in script
    assert '.Close(0)' not in script
    assert '.Quit(0)' not in script
    assert 'function Wait-WordExit' in script
    assert "status='rejected'" in script


def test_batch_worker_durably_records_acceptance_before_request_action():
    script = (Path(__file__).parents[3] / "scripts/word_batch_worker.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "accepted_sequence=0" in script
    accepted = script.index("$taskState.accepted_sequence = $taskSequence")
    persisted = script.index("Save-Control $taskState", accepted)
    assert accepted < persisted < script.index("if ($taskRequest.action -ceq 'close')")
    assert persisted < script.index("$taskDocument = $taskWord.Documents.Open(")
