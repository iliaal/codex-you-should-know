import importlib.util
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_plugin", ROOT / "scripts/package_plugin.py")
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def snapshot(root):
    entries = {}
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            content = ("symlink", str(path.readlink()))
        elif path.is_dir():
            content = ("directory", None)
        else:
            content = ("file", path.read_bytes())
        entries[str(path.relative_to(root))] = (stat.S_IMODE(path.lstat().st_mode), content)
    return entries


@pytest.fixture
def packages(tmp_path):
    source = builder.build_package(ROOT, tmp_path / "source with spaces")
    output = builder.build_package(source, tmp_path / "output with spaces")
    (output / "previous-output").write_bytes(b"Keep the previous successful build.\n")
    (output / "previous-output").chmod(0o640)
    return source, output, snapshot(output)


@pytest.mark.parametrize("relative", builder.FILES)
def test_missing_declared_file_preserves_previous_package(packages, relative):
    source, output, before = packages
    (source / relative).unlink()

    with pytest.raises(ValueError, match="Missing package file"):
        builder.build_package(source, output)

    assert snapshot(output) == before
    assert not (source / relative).exists()
    assert not list(output.parent.glob(".ysk-build-*"))


def test_empty_module_directory_preserves_previous_package(packages):
    source, output, before = packages
    for module in (source / "src/you_should_know").glob("*.py"):
        module.unlink()

    with pytest.raises(ValueError, match="no Python modules"):
        builder.build_package(source, output)

    assert snapshot(output) == before
    assert not list(output.parent.glob(".ysk-build-*"))


@pytest.mark.parametrize("relative", ["README.md", "src/you_should_know/cli.py"])
@pytest.mark.parametrize("dangling", [False, True], ids=["readable", "dangling"])
def test_source_file_links_preserve_previous_package(packages, relative, dangling):
    source, output, before = packages
    target = source.parent / "outside-source"
    if not dangling:
        target.write_bytes(b"Do not copy this external file.\n")
    path = source / relative
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(ValueError, match="Refusing symlink path"):
        builder.build_package(source, output)

    assert snapshot(output) == before
    assert path.is_symlink()
    assert target.exists() is not dangling
    if not dangling:
        assert target.read_bytes() == b"Do not copy this external file.\n"
    assert not list(output.parent.glob(".ysk-build-*"))


@pytest.mark.parametrize("relative", ["skills", "src/you_should_know"])
def test_source_directory_links_preserve_previous_package(packages, relative):
    source, output, before = packages
    path = source / relative
    target = source.parent / "outside-source"
    path.rename(target)
    outside = snapshot(target)
    path.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="Refusing symlink path"):
        builder.build_package(source, output)

    assert snapshot(output) == before
    assert snapshot(target) == outside
    assert path.is_symlink()
    assert not list(output.parent.glob(".ysk-build-*"))


@pytest.mark.parametrize("marker_kind", ["wrong-owner", "wrong-source", "symlink", "directory"])
def test_invalid_ownership_marker_preserves_previous_package(packages, marker_kind):
    source, output, _ = packages
    marker = output / builder.MARKER
    if marker_kind == "wrong-owner":
        marker.write_text('{"package": "another-package"}\n')
    elif marker_kind == "wrong-source":
        marker.write_text('{"package": "you-should-know", "source": "another-source"}\n')
    elif marker_kind == "symlink":
        target = source.parent / "external-marker"
        marker.rename(target)
        marker.symlink_to(target)
    else:
        marker.unlink()
        marker.mkdir()
    before = snapshot(output)

    with pytest.raises(ValueError, match="unowned output directory"):
        builder.build_package(source, output)

    assert snapshot(output) == before
    if marker_kind == "symlink":
        assert marker.is_symlink()
    elif marker_kind == "directory":
        assert marker.is_dir()
    assert not list(output.parent.glob(".ysk-build-*"))


@pytest.mark.parametrize("existing", [False, True], ids=["first-build", "rebuild"])
@pytest.mark.parametrize("failed_copy", [1, 3, -1], ids=["first-file", "middle-file", "last-file"])
def test_partial_copy_failure_preserves_output_and_cleans_staging(
    packages, monkeypatch, existing, failed_copy
):
    source, previous, before = packages
    output = previous if existing else source.parent / "new output"
    original_source = snapshot(source)
    count = len(builder.FILES) + len(list((source / "src/you_should_know").glob("*.py")))
    failure_at = count if failed_copy == -1 else failed_copy
    copied = 0
    real_copy = builder.shutil.copyfile

    def fail_during_copy(source_file, target):
        nonlocal copied
        copied += 1
        if copied == failure_at:
            target.write_bytes(b"incomplete staged file")
            raise OSError("synthetic copy failure")
        return real_copy(source_file, target)

    monkeypatch.setattr(builder.shutil, "copyfile", fail_during_copy)
    with pytest.raises(OSError, match="synthetic copy failure"):
        builder.build_package(source, output)

    assert copied == failure_at
    assert snapshot(previous) == before
    assert snapshot(source) == original_source
    assert output.exists() is existing
    assert not list(output.parent.glob(".ysk-build-*"))


@pytest.mark.parametrize("existing", [False, True], ids=["first-build", "rebuild"])
def test_staged_marker_failure_preserves_output_and_cleans_staging(packages, monkeypatch, existing):
    source, previous, before = packages
    output = previous if existing else source.parent / "new output"
    original_source = snapshot(source)
    real_write = Path.write_text
    attempted = []

    def fail_marker_write(path, *args, **kwargs):
        if path.name == builder.MARKER and path.parent.parent.name.startswith(".ysk-build-"):
            attempted.append(path)
            path.write_bytes(b"partial ownership marker")
            raise OSError("synthetic marker write failure")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_marker_write)
    with pytest.raises(OSError, match="synthetic marker write failure"):
        builder.build_package(source, output)

    assert len(attempted) == 1
    assert snapshot(previous) == before
    assert snapshot(source) == original_source
    assert output.exists() is existing
    assert not list(output.parent.glob(".ysk-build-*"))
