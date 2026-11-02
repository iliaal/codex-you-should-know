import importlib.util
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev_update", ROOT / "scripts/dev_update.py")
assert spec is not None and spec.loader is not None
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def configure(root):
    for key, value in (
        ("user.name", "Development update test"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", "/dev/null"),
    ):
        git(root, "config", key, value)


def commit(root, name, value):
    (root / name).write_text(value)
    git(root, "add", name)
    git(root, "commit", "-m", name)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "upstream with spaces"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    configure(remote)
    commit(remote, "target.txt", "original\n")
    commit(remote, "preferences.txt", "original preferences\n")
    local = tmp_path / "local with spaces"
    git(tmp_path, "clone", str(remote), str(local))
    configure(local)
    return local, remote


def pending_edits(root):
    (root / "preferences.txt").write_text("staged preferences\n")
    git(root, "add", "preferences.txt")
    (root / "preferences.txt").write_text("unstaged preferences\n")
    (root / "untracked notes.txt").write_text("private notes\n")
    return git(root, "diff"), git(root, "diff", "--cached")


def assert_edits(root, expected):
    assert (git(root, "diff"), git(root, "diff", "--cached")) == expected
    assert (root / "untracked notes.txt").read_text() == "private notes\n"


def test_fetch_and_fast_forward_preserve_staged_unstaged_untracked_and_old_stash(repos):
    local, remote = repos
    (local / "preferences.txt").write_text("earlier saved work\n")
    git(local, "stash", "push", "-m", "existing stash")
    old_stash = git(local, "stash", "list", "--format=%H")
    expected = pending_edits(local)
    target = commit(remote, "new upstream.txt", "new upstream feature\n")

    updater.update_source(local)

    assert git(local, "rev-parse", "HEAD") == target
    assert_edits(local, expected)
    assert git(local, "stash", "list", "--format=%H") == old_stash


def test_diverged_branches_are_merged_without_rewriting_local_commits(repos):
    local, remote = repos
    mine = commit(local, "local feature.txt", "our feature\n")
    theirs = commit(remote, "upstream feature.txt", "upstream feature\n")
    updater.update_source(local)
    assert git(local, "rev-list", "--parents", "-1", "HEAD").split()[1:] == [mine, theirs]
    assert (local / "local feature.txt").exists()
    assert (local / "upstream feature.txt").exists()


def test_merge_conflict_aborts_and_restores_original_edits(repos):
    local, remote = repos
    before = commit(local, "target.txt", "local committed version\n")
    commit(remote, "target.txt", "upstream committed version\n")
    expected = pending_edits(local)

    with pytest.raises(updater.UpdateError, match="could not be merged automatically"):
        updater.update_source(local)

    assert git(local, "rev-parse", "HEAD") == before
    assert_edits(local, expected)
    assert git(local, "stash", "list") == ""
    assert not (local / ".git/MERGE_HEAD").exists()


def test_restoration_conflict_retains_saved_edits_and_stops(repos):
    local, remote = repos
    (local / "target.txt").write_text("our uncommitted change\n")
    (local / "untracked.txt").write_text("also preserve this\n")
    target = commit(remote, "target.txt", "their change\n")

    with pytest.raises(updater.UpdateError, match="remain saved in Git stash"):
        updater.update_source(local)

    assert git(local, "rev-parse", "HEAD") == target
    assert git(local, "show", "stash:target.txt") == "our uncommitted change"
    assert git(local, "show", "stash^3:untracked.txt") == "also preserve this"
    assert "target.txt" in git(local, "diff", "--name-only", "--diff-filter=U")
    with pytest.raises(updater.UpdateError, match="existing Git conflicts"):
        updater.update_source(local)


def test_failed_fetch_leaves_work_unstashed_and_unchanged(repos):
    local, remote = repos
    expected = pending_edits(local)
    git(local, "remote", "set-url", "origin", str(remote / "does-not-exist"))
    with pytest.raises(updater.UpdateError, match="fetch"):
        updater.update_source(local)
    assert_edits(local, expected)
    assert git(local, "stash", "list") == ""


def test_detached_head_and_untracked_branch_are_actionable_errors(repos):
    local, _ = repos
    git(local, "checkout", "--detach")
    with pytest.raises(updater.UpdateError, match="detached"):
        updater.update_source(local)
    git(local, "switch", "-c", "local-only")
    with pytest.raises(updater.UpdateError, match="set-upstream-to"):
        updater.update_source(local)


@pytest.fixture
def source(tmp_path):
    package = updater.runpy.run_path(str(ROOT / "scripts/package_plugin.py"))
    root = package["build_package"](ROOT, tmp_path / "source with spaces")
    shutil.copyfile(ROOT / "scripts/package_plugin.py", root / "scripts/package_plugin.py")
    return root


def test_content_version_changes_only_with_package_content_and_keeps_old_build(source):
    original = (source / ".codex-plugin/plugin.json").read_bytes()
    first = updater.build(source)
    assert "-dev.g" in first.name
    assert updater.build(source) == first
    (source / "src/you_should_know/config.py").write_text("# changed runtime\n")
    second = updater.build(source)
    assert second != first
    assert first.is_dir()
    assert (source / ".codex-plugin/plugin.json").read_bytes() == original


def test_modified_existing_bundle_is_not_replaced(source):
    bundle = updater.build(source)
    (bundle / "README.md").write_text("local modification\n")
    with pytest.raises(updater.UpdateError, match="leaving it intact"):
        updater.build(source)
    assert (bundle / "README.md").read_text() == "local modification\n"


class FakeCodex:
    def __init__(self, root, source):
        self.home = root / "fake codex home"
        self.marketplace = {"name": updater.NAME, "root": str(root / "published marketplace")}
        shutil.copytree(source, self.marketplace["root"])
        installed_version = json.loads(
            (Path(self.marketplace["root"]) / ".codex-plugin/plugin.json").read_text()
        )["version"]
        self.plugins = [
            {
                "name": updater.NAME,
                "pluginId": updater.PLUGIN,
                "marketplaceName": updater.NAME,
                "version": installed_version,
                "enabled": True,
            }
        ]
        self.calls = []
        self.fail_next_add = False
        cache = self.home / "plugins/cache" / updater.NAME / updater.NAME / installed_version
        shutil.copytree(source, cache)

    def __call__(self, root, *args):
        self.calls.append(args)
        if args == ("list",):
            return {"installed": self.plugins.copy()}
        if args == ("marketplace", "list"):
            return {"marketplaces": [self.marketplace] if self.marketplace else []}
        if args[0] == "remove":
            self.plugins = []
            shutil.rmtree(self.home / "plugins/cache" / updater.NAME, ignore_errors=True)
        elif args[:2] == ("marketplace", "remove"):
            self.marketplace = None
        elif args[:2] == ("marketplace", "add"):
            assert self.marketplace is None
            self.marketplace = {"name": updater.NAME, "root": args[2]}
        elif args[0] == "add":
            if self.fail_next_add:
                self.fail_next_add = False
                raise updater.UpdateError("simulated installation failure")
            assert self.marketplace is not None
            marketplace = Path(self.marketplace["root"])
            catalog = json.loads((marketplace / ".agents/plugins/marketplace.json").read_text())
            package = marketplace / catalog["plugins"][0]["source"]["path"]
            manifest = package / ".codex-plugin/plugin.json"
            version = json.loads(manifest.read_text())["version"]
            cache = self.home / "plugins/cache" / updater.NAME / updater.NAME / version
            shutil.copytree(package, cache)
            self.plugins = [
                {
                    "name": updater.NAME,
                    "pluginId": updater.PLUGIN,
                    "marketplaceName": updater.NAME,
                    "version": version,
                    "enabled": True,
                }
            ]
        else:
            pytest.fail(f"Unexpected Codex operation {args}")
        return {}


@pytest.fixture
def installation(source, tmp_path, monkeypatch):
    fake = FakeCodex(tmp_path, source)
    monkeypatch.setenv("CODEX_HOME", str(fake.home))
    monkeypatch.setattr(updater, "codex", fake)
    monkeypatch.setattr(updater, "require_closed_codex", lambda root: None)
    return source, fake


def test_published_install_switches_to_local_and_repeated_install_is_unchanged(installation):
    root, fake = installation
    bundle = updater.build(root)
    updater.install(root, bundle)
    assert fake.marketplace["root"] == str(bundle)
    assert fake.plugins[0]["version"] == bundle.name
    mutations = [c for c in fake.calls if "add" in c or "remove" in c]
    assert mutations == [
        ("remove", updater.PLUGIN),
        ("marketplace", "remove", updater.NAME),
        ("marketplace", "add", str(bundle)),
        ("add", updater.PLUGIN),
    ]
    fake.calls.clear()
    updater.install(root, bundle)
    assert not [c for c in fake.calls if "add" in c or "remove" in c]
    assert not list((root / ".scratch").glob("dev-update-backup-*"))


def test_failed_install_restores_previous_files_without_network(installation, capsys):
    root, fake = installation
    bundle = updater.build(root)
    fake.fail_next_add = True
    with pytest.raises(updater.UpdateError, match="restoration details"):
        updater.install(root, bundle)
    backup = Path(fake.marketplace["root"])
    assert (
        fake.plugins[0]["version"]
        == json.loads((backup / "plugin/.codex-plugin/plugin.json").read_text())["version"]
    )
    assert backup.is_dir()
    assert (backup.parent / "previous.json").exists()
    assert backup != bundle
    assert fake.plugins[0]["pluginId"] == updater.PLUGIN
    output = capsys.readouterr().err
    assert "Restored the previous files from a local backup" in output
    assert "Automatic restoration failed" not in output


@pytest.mark.parametrize("mismatch", ["marketplace", "plugin_id"])
def test_failed_install_does_not_claim_restoration_with_wrong_registration(
    installation, monkeypatch, capsys, mismatch
):
    root, fake = installation
    bundle = updater.build(root)
    fake.fail_next_add = True

    def codex(root, *args):
        result = fake(root, *args)
        if args == ("add", updater.PLUGIN):
            if mismatch == "marketplace":
                fake.marketplace["root"] = str(root / "wrong marketplace")
            else:
                fake.plugins[0]["pluginId"] = "wrong-plugin@you-should-know"
        return result

    monkeypatch.setattr(updater, "codex", codex)
    with pytest.raises(updater.UpdateError, match="restoration details"):
        updater.install(root, bundle)

    output = capsys.readouterr().err
    assert "Automatic restoration failed" in output
    assert "Restored the previous files from a local backup" not in output
    backups = list((root / ".scratch").glob("dev-update-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "previous.json").is_file()
    assert (backups[0] / "marketplace/plugin/.codex-plugin/plugin.json").is_file()


@pytest.mark.parametrize("ancestor", [False, True])
def test_checkout_marketplace_backup_does_not_copy_into_itself(installation, monkeypatch, ancestor):
    root, fake = installation
    fake.marketplace["root"] = str(root.parent if ancestor else root)
    fake.plugins = []
    bundle = updater.build(root)
    original = shutil.copytree
    destinations = []

    def bounded_copy(source, destination, *args, **kwargs):
        if not destinations:
            destinations.append(Path(destination))
        assert Path(source) != destinations[0], "Backup entered its own destination"
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", bounded_copy)
    updater.install(root, bundle)
    assert fake.plugins[0]["version"] == bundle.name


@pytest.mark.parametrize("failure", ["missing", "mismatched"])
def test_unverifiable_installed_package_stops_before_removing_plugin(installation, failure):
    root, fake = installation
    installed = updater.installed_package(fake.plugins[0])
    manifest = installed / ".codex-plugin/plugin.json"
    if failure == "missing":
        manifest.unlink()
    else:
        data = json.loads(manifest.read_text())
        data["version"] = "9.0.0"
        manifest.write_text(json.dumps(data))
    with pytest.raises(updater.UpdateError, match="installed YSK package|does not match"):
        updater.install(root, updater.build(root))
    assert fake.plugins[0]["enabled"] is True
    assert not [c for c in fake.calls if "add" in c or "remove" in c]


@pytest.mark.parametrize("enabled", [None, 0, 1, "false", "true", [], {}])
def test_invalid_installed_enablement_stops_before_removal(installation, enabled):
    root, fake = installation
    fake.plugins[0]["enabled"] = enabled
    original = updater.package_contents(updater.installed_package(fake.plugins[0]))
    with pytest.raises(updater.UpdateError, match="enablement"):
        updater.install(root, updater.build(root))
    assert not [c for c in fake.calls if "add" in c or "remove" in c]
    assert updater.package_contents(updater.installed_package(fake.plugins[0])) == original
    assert not list((root / ".scratch").glob("dev-update-backup-*"))


def test_missing_installed_enablement_stops_before_removal(installation):
    root, fake = installation
    del fake.plugins[0]["enabled"]
    with pytest.raises(updater.UpdateError, match="enablement"):
        updater.install(root, updater.build(root))
    assert not [c for c in fake.calls if "add" in c or "remove" in c]
    assert updater.installed_package(fake.plugins[0]).is_dir()
    assert not list((root / ".scratch").glob("dev-update-backup-*"))


@pytest.mark.parametrize("other_marketplace", [False, True])
def test_other_installed_plugins_are_not_removed(installation, other_marketplace):
    root, fake = installation
    fake.plugins.append(
        {
            "name": updater.NAME if other_marketplace else "another-plugin",
            "marketplaceName": "another-marketplace" if other_marketplace else updater.NAME,
        }
    )
    with pytest.raises(updater.UpdateError):
        updater.install(root, updater.build(root))
    assert not [c for c in fake.calls if "add" in c or "remove" in c]


def test_codex_process_guard_prevents_installation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        updater,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "12 S bash\n34 Ss /opt/codex\n"),
    )

    # Do not accidentally read an unrelated real process with this test PID.
    def unavailable(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(updater.UpdateError, match="process IDs: 34"):
        updater.require_closed_codex(tmp_path)


@pytest.mark.parametrize("state", ["Z", "Zs", "Z+"])
def test_zombie_codex_processes_do_not_block_installation(monkeypatch, tmp_path, state):
    def process_list(args, *rest, **kwargs):
        status = f"{state} " if "stat=" in args[-1] else ""
        return subprocess.CompletedProcess(args, 0, f"76843 {status}codex [codex] <defunct>\n")

    monkeypatch.setattr(updater, "run", process_list)
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: pytest.fail("Read arguments of an exited process")
    )
    updater.require_closed_codex(tmp_path)


def test_persistent_codex_services_do_not_block_installation(monkeypatch, tmp_path):
    # These services remain after the user closes the app's chat windows.
    processes = (
        "6222 Ssl codex /opt/codex app-server daemon pid-update-loop\n"
        "6875 Ssl codex /opt/codex app-server --listen unix:// --managed-daemon\n"
        "7228 Sl codex-code-mode- /opt/codex-code-mode-host\n"
    )
    monkeypatch.setattr(
        updater, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, processes)
    )

    def unavailable(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    updater.require_closed_codex(tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["/opt/codex"],
        ["/opt/codex", "resume", "example-chat"],
        ["/opt/codex", "exec", "check app-server"],
        ["/opt/codex", "Review the app-server logs"],
    ],
)
def test_cli_still_blocks_when_background_services_are_present(monkeypatch, tmp_path, argv):
    processes = (
        "6222 Ssl codex /opt/codex app-server daemon pid-update-loop\n"
        "76843 Zs codex [codex] <defunct>\n34 Ss codex " + " ".join(argv) + "\n"
    )
    monkeypatch.setattr(
        updater, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, processes)
    )
    commands = {"6222": ["/opt/codex", "app-server", "daemon", "pid-update-loop"], "34": argv}
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: ("\0".join(commands[path.parent.name]) + "\0").encode()
    )
    with pytest.raises(updater.UpdateError, match=r"CLI process IDs: 34\)"):
        updater.require_closed_codex(tmp_path)


def test_service_detection_uses_exact_arguments_for_paths_with_spaces(monkeypatch, tmp_path):
    processes = "6222 Ssl codex /opt/Codex App/codex app-server --managed-daemon\n"
    monkeypatch.setattr(
        updater, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, processes)
    )
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: b"/opt/Codex App/codex\0app-server\0--managed-daemon\0"
    )
    updater.require_closed_codex(tmp_path)


def test_validation_failure_stops_before_build_or_install(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(updater, "__file__", str(root / "scripts/dev_update.py"))
    monkeypatch.setattr(updater, "check_git_state", lambda root: None)

    def fail(root):
        raise updater.UpdateError("validation failed")

    monkeypatch.setattr(updater, "validate", fail)
    monkeypatch.setattr(updater, "build", lambda root: pytest.fail("Built after failed checks"))
    monkeypatch.setattr(
        updater, "install", lambda *args: pytest.fail("Installed after failed checks")
    )
    assert updater.main(["--prepare-only", "--no-fetch"]) == 1


@pytest.mark.skipif(
    os.environ.get("YSK_TEST_NATIVE_CODEX") != "1" or not shutil.which("codex"),
    reason="Set YSK_TEST_NATIVE_CODEX=1 for installation in an isolated Codex profile",
)
@pytest.mark.parametrize("git_marketplace", [False, True])
def test_native_codex_switches_marketplaces_and_installs_changed_development_version(
    source, tmp_path, monkeypatch, git_marketplace
):
    home = tmp_path / "isolated codex home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    # Only this isolated profile is modified; the real client may stay open.
    monkeypatch.setattr(updater, "require_closed_codex", lambda root: None)
    published = tmp_path / "old published bundle"
    shutil.copytree(source, published)
    location = str(published)
    if git_marketplace:
        git(published, "init", "-b", "main")
        configure(published)
        git(published, "add", ".")
        git(published, "commit", "-m", "Published package")
        # Exercise a real Git-backed Codex marketplace without network requests.
        location = "https://github.com/ysk-test/published.git"
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{published.as_uri()}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", location)
    updater.codex(source, "marketplace", "add", location)
    updater.codex(source, "add", updater.PLUGIN)
    data = home / "you-should-know"
    data.mkdir()
    (data / "config.toml").write_text('review_instructions = "Keep this."\n')
    (data / "state.sqlite3").write_bytes(b"existing notes are not read or modified")

    first = updater.build(source)
    updater.install(source, first)
    updater.install(source, first)
    (source / "src/you_should_know/config.py").write_text("# another development build\n")
    second = updater.build(source)
    updater.install(source, second)

    plugins, marketplace = updater.inventory(source)
    assert len(plugins) == 1 and plugins[0]["version"] == second.name
    assert marketplace and Path(marketplace["root"]) == second
    assert first.is_dir()

    # Fail after unregistering the previous installation, then restore it using
    # the real CLI and the saved files rather than another remote fetch.
    (source / "src/you_should_know/config.py").write_text("# third development build\n")
    third = updater.build(source)
    original = updater.codex
    failures = []

    def fail_once(root, *args):
        if args == ("add", updater.PLUGIN) and not failures:
            failures.append(True)
            raise updater.UpdateError("simulate a failed install")
        return original(root, *args)

    monkeypatch.setattr(updater, "codex", fail_once)
    with pytest.raises(updater.UpdateError, match="restoration details"):
        updater.install(source, third)
    restored, registered = updater.inventory(source)
    assert restored[0]["version"] == second.name
    assert registered and (Path(registered["root"]) / ".agents/plugins/marketplace.json").exists()
    assert (data / "config.toml").read_text() == 'review_instructions = "Keep this."\n'
    assert (data / "state.sqlite3").read_bytes() == b"existing notes are not read or modified"


@pytest.mark.skipif(
    os.environ.get("YSK_TEST_NATIVE_CODEX") != "1" or not shutil.which("codex"),
    reason="Set YSK_TEST_NATIVE_CODEX=1 for installation in an isolated Codex profile",
)
@pytest.mark.parametrize("change", ["disabled", "marketplace_advanced"])
def test_native_codex_failed_update_restores_installed_bytes_and_enabled_state(
    source, tmp_path, monkeypatch, change
):
    home = tmp_path / "isolated codex home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(updater, "require_closed_codex", lambda root: None)
    published = tmp_path / "published"
    shutil.copytree(source, published)
    updater.codex(source, "marketplace", "add", str(published))
    added = updater.codex(source, "add", updater.PLUGIN)
    installed = Path(added["installedPath"])
    (installed / "installed-only.txt").write_text("Preserve the installed package.\n")
    expected = {
        p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()
    }
    config = home / "config.toml"
    config.write_text(
        config.read_text()
        + "\n[notice]\n# Preserve this comment\nhide_rate_limit_model_nudge = true\n"
    )
    if change == "disabled":
        config = home / "config.toml"
        text = config.read_text()
        assert text.count("enabled = true") == 1
        config.write_text(text.replace("enabled = true", "enabled = false"))
    before, _ = updater.inventory(source)
    previous_settings = tomllib.loads(config.read_text())
    assert before[0]["enabled"] is (change != "disabled")
    if change == "marketplace_advanced":
        manifest = published / ".codex-plugin/plugin.json"
        data = json.loads(manifest.read_text())
        data["version"] = "9.0.0"
        manifest.write_text(json.dumps(data))
        (published / "src/you_should_know/config.py").write_text("# changed marketplace\n")
    bundle = updater.build(source)
    original = updater.codex
    failures = []

    def fail_once(root, *args):
        if args == ("add", updater.PLUGIN) and not failures:
            failures.append(True)
            raise updater.UpdateError("simulate a failed install")
        return original(root, *args)

    monkeypatch.setattr(updater, "codex", fail_once)
    with pytest.raises(updater.UpdateError, match="restoration details"):
        updater.install(source, bundle)
    restored, _ = updater.inventory(source)
    assert restored[0]["version"] == before[0]["version"]
    assert restored[0]["enabled"] == before[0]["enabled"]
    actual = {p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()}
    assert actual == expected
    assert tomllib.loads(config.read_text())["notice"] == previous_settings["notice"]
    assert "# Preserve this comment" in config.read_text()
