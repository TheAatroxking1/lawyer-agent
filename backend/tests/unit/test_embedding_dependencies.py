import tomllib
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def test_cpu_embedding_extra_is_explicit_and_version_bounded():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extra = {item.name: item for item in map(
        Requirement, project["optional-dependencies"]["embedding-cpu"],
    )}
    assert "6.0.1" in extra["sentence-transformers"].specifier
    assert "7.0.0" not in extra["sentence-transformers"].specifier
    assert "2.14.0+cpu" in extra["torch"].specifier
    assert "2.15.0" not in extra["torch"].specifier
    assert not {"torch", "sentence-transformers"}.intersection(
        Requirement(item).name for item in project["dependencies"]
    )


def test_only_torch_uses_explicit_official_cpu_index_on_windows_and_linux():
    uv = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]
    index = next(item for item in uv["index"] if item["name"] == "pytorch-cpu")
    assert index["url"] == "https://download.pytorch.org/whl/cpu"
    assert index["explicit"] is True
    sources = uv["sources"]
    assert set(sources) == {"torch"}
    source = sources["torch"][0]
    assert source["index"] == index["name"]
    marker = Marker(source["marker"])
    assert marker.evaluate({"sys_platform": "linux"})
    assert marker.evaluate({"sys_platform": "win32"})
    assert not marker.evaluate({"sys_platform": "darwin"})


def test_lock_has_cpu_torch_and_keeps_generic_packages_on_pypi():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    cpu = [item for item in lock["package"] if item["source"].get("registry") == (
        "https://download.pytorch.org/whl/cpu"
    )]
    assert cpu and all(item["name"] == "torch" for item in cpu)
    assert all(item["version"].endswith("+cpu") for item in cpu)
    assert any(item["name"] == "sentence-transformers" for item in lock["package"])
