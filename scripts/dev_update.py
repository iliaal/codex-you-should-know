#!/usr/bin/env python3
"""Merge upstream, validate this checkout, and install one local YSK development build."""

import argparse
import fcntl
import hashlib
import json
import os
import runpy
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NAME = "you-should-know"
PLUGIN = f"{NAME}@{NAME}"


class UpdateError(RuntimeError):
    pass


def run(
    args: list[str], root: Path, *, allow_failure: bool = False, show: bool = False
) -> subprocess.CompletedProcess[str]:
    if show:
        print(f"+ {shlex.join(args)}", flush=True)
    result = subprocess.run(
        args,
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=not show,
        text=True,
        check=False,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no"),
    )
    if result.returncode and not allow_failure:
        detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part)
        raise UpdateError(f"Command failed ({result.returncode}): {shlex.join(args)}\n{detail}")
    return result


def git(root: Path, *args: str, allow_failure: bool = False) -> str:
    return run(["git", *args], root, allow_failure=allow_failure).stdout.strip()


def check_git_state(root: Path) -> None:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise UpdateError("Run this script from a complete Git checkout of the project.")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        path = root / git(root, "rev-parse", "--git-path", name)
        if path.exists():
            raise UpdateError("Finish or abort the existing Git operation before updating.")
    if git(root, "diff", "--name-only", "--diff-filter=U"):
        raise UpdateError("Resolve the existing Git conflicts before updating.")


def restore_edits(root: Path, stash: str) -> None:
    result = run(["git", "stash", "apply", "--index", stash], root, allow_failure=True)
    if result.returncode:
        raise UpdateError(
            "Upstream was merged, but your saved edits need manual conflict resolution.\n"
            f"All edits, including untracked files, remain saved in Git stash {stash}.\n"
            "Inspect git status; do not apply the stash a second time over partial changes.\n"
            f"{result.stdout}{result.stderr}"
        )
    for line in git(root, "stash", "list", "--format=%gd %H").splitlines():
        reference, identifier = line.split()
        if identifier == stash:
            git(root, "stash", "drop", reference)
            break
    print("Restored your staged, unstaged, and untracked work.", flush=True)


def update_source(root: Path) -> None:
    check_git_state(root)
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    if not branch:
        raise UpdateError("Check out a branch before updating; HEAD is detached.")
    remote = git(root, "config", f"branch.{branch}.remote", allow_failure=True)
    upstream = git(root, "rev-parse", "--abbrev-ref", "@{upstream}", allow_failure=True)
    if not upstream or not remote:
        raise UpdateError(
            f"Branch {branch!r} has no tracked upstream. Set it first, for example:\n"
            f"git branch --set-upstream-to=origin/main {shlex.quote(branch)}"
        )
    print(f"Checking upstream {upstream}…", flush=True)
    if remote != ".":
        run(["git", "fetch", "--no-tags", remote], root, show=True)
    target = git(root, "rev-parse", "--verify", "@{upstream}^{commit}")
    if (
        run(
            ["git", "merge-base", "--is-ancestor", target, "HEAD"], root, allow_failure=True
        ).returncode
        == 0
    ):
        print("This branch already includes upstream.", flush=True)
        return
    stash = ""
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        git(root, "stash", "push", "--include-untracked", "--message", "YSK development update")
        stash = git(root, "rev-parse", "refs/stash")
        print(f"Saved your uncommitted work temporarily: {stash}", flush=True)
    try:
        result = run(["git", "merge", "--no-edit", target], root, allow_failure=True)
    except BaseException:
        print(f"Update interrupted. Inspect git status. Saved edits: {stash or 'none'}", flush=True)
        raise
    if result.returncode:
        merge_head = Path(git(root, "rev-parse", "--git-path", "MERGE_HEAD"))
        if not merge_head.is_absolute():
            merge_head = root / merge_head
        if merge_head.exists():
            aborted = run(["git", "merge", "--abort"], root, allow_failure=True)
            if aborted.returncode:
                raise UpdateError(
                    "Could not abort the upstream merge. Inspect git status. "
                    f"Saved edits: {stash}\n"
                    f"{aborted.stderr}"
                )
        if stash:
            restore_edits(root, stash)
        raise UpdateError(
            "Upstream could not be merged automatically; the plugin was not changed.\n"
            f"{result.stdout}{result.stderr}"
        )
    if stash:
        restore_edits(root, stash)
    print(f"Merged upstream {target[:12]}.", flush=True)


def require_closed_codex(root: Path) -> None:
    """Check for live standalone CLI clients, not persistent app-server services.

    An app-server process can remain after its UI closes, so this cannot detect
    open app chat windows. The user still needs to close those before updating.
    """
    result = run(["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,stat=,comm=,args="], root)
    active = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) < 3:
            continue
        pid, state, command = fields[:3]
        # Zombies have exited and cannot use hook files, even before being reaped.
        if state.startswith("Z"):
            continue
        name = Path(command).name.lower()
        if name not in {"codex", "codex.exe", "codex-cli"} and not name.startswith("codex-"):
            continue
        # Linux provides exact argument boundaries, even for paths with spaces.
        # Elsewhere, ps supplies enough information for the usual service commands.
        try:
            argv = os.fsdecode(Path(f"/proc/{pid}/cmdline").read_bytes()).rstrip("\0").split("\0")
        except OSError:
            argv = fields[3].split() if len(fields) > 3 else []
        executable = Path(argv[0]).name.lower() if argv else name
        if executable == "codex-code-mode-host" or name == "codex-code-mode-host":
            continue
        if argv[1:2] == ["app-server"]:
            continue
        active.append(pid)
    if active:
        raise UpdateError(
            "Close standalone Codex CLI sessions before replacing their installed hook files "
            f"(running CLI process IDs: {', '.join(active)}).\n"
            "Use --prepare-only to check upstream, test, and build while Codex remains open."
        )


def uv_command() -> str:
    executable = shutil.which("uv")
    if not executable:
        raise UpdateError(
            "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
        )
    # The Snap launcher cannot run inside some confined shells; its actual binary can.
    direct = Path("/snap/astral-uv/current/bin/uv")
    if executable.startswith("/snap/bin/") and direct.is_file():
        return str(direct)
    return executable


def validate(root: Path) -> None:
    run([uv_command(), "--no-config", "sync", "--locked"], root, show=True)
    executables = root / ".venv/bin"
    for args in (
        [str(executables / "python"), "-m", "pytest", "--cov"],
        [str(executables / "ruff"), "check", "."],
        [str(executables / "ruff"), "format", "--check", "."],
        [str(executables / "ty"), "check", "."],
        ["git", "diff", "--check"],
    ):
        run(args, root, show=True)


def build(root: Path) -> Path:
    packaging = runpy.run_path(str(root / "scripts/package_plugin.py"))
    destination = root / "dist/development"
    packaging["reject_symlinks"](destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=destination) as temporary:
        staged = packaging["build_package"](root, Path(temporary) / "package")
        digest = hashlib.sha256()
        for path in sorted(staged.rglob("*")):
            if path.is_file() and path.name != packaging["MARKER"]:
                digest.update(str(path.relative_to(staged)).encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        manifest = staged / ".codex-plugin/plugin.json"
        data = json.loads(manifest.read_text())
        base = data["version"].split("+", 1)[0].split("-", 1)[0]
        data["version"] = f"{base}-dev.g{digest.hexdigest()[:16]}"
        manifest.write_text(json.dumps(data, indent=2) + "\n")
        bundle = destination / data["version"]
        packaging["reject_symlinks"](bundle)
        if bundle.exists():
            if any(p.is_symlink() for p in bundle.rglob("*")):
                raise UpdateError(f"Existing development bundle contains a symlink: {bundle}")
            expected = {
                p.relative_to(staged): p.read_bytes() for p in staged.rglob("*") if p.is_file()
            }
            actual = {
                p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()
            }
            if expected != actual:
                raise UpdateError(
                    f"Existing development bundle was modified; leaving it intact: {bundle}"
                )
        else:
            os.replace(staged, bundle)
    print(f"Built {data['version']} at {bundle}", flush=True)
    return bundle


def codex(root: Path, *args: str) -> dict:
    result = run(["codex", "--disable", "remote_plugin", "plugin", *args, "--json"], root)
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise UpdateError(f"Codex returned invalid JSON: {result.stdout}") from error


def inventory(root: Path) -> tuple[list[dict], dict | None]:
    plugins = codex(root, "list")["installed"]
    marketplaces = codex(root, "marketplace", "list")["marketplaces"]
    others = [p for p in plugins if p["marketplaceName"] == NAME and p["name"] != NAME]
    if others:
        raise UpdateError(
            f"The {NAME} marketplace contains other installed plugins; leaving it intact."
        )
    copies = [p for p in plugins if p["name"] == NAME]
    if len(copies) > 1:
        raise UpdateError("Codex reported multiple YSK installations; leaving them intact.")
    if any(p["marketplaceName"] != NAME for p in copies):
        raise UpdateError("YSK is also installed from another marketplace; remove that copy first.")
    if any(not isinstance(p.get("enabled"), bool) for p in copies):
        raise UpdateError("Installed YSK enablement is unavailable; leaving it intact.")
    marketplace = next((m for m in marketplaces if m["name"] == NAME), None)
    if copies and not marketplace:
        raise UpdateError(
            "Could not find the existing YSK marketplace; leaving its installation intact."
        )
    return copies, marketplace


def installed_package(plugin: dict) -> Path:
    version = plugin.get("version")
    if (
        not isinstance(version, str)
        or not version
        or version in {".", ".."}
        or Path(version).name != version
    ):
        raise UpdateError("Could not identify the installed YSK version; leaving it intact.")
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    package = home / "plugins/cache" / NAME / NAME / version
    try:
        manifest = json.loads((package / ".codex-plugin/plugin.json").read_text())
    except (OSError, ValueError) as error:
        raise UpdateError(f"Cannot back up the installed YSK package: {package}") from error
    if manifest.get("name") != NAME or manifest.get("version") != version:
        raise UpdateError(f"Installed YSK package does not match Codex inventory: {package}")
    return package.resolve()


def package_contents(package: Path) -> dict:
    result = {}
    for path in package.rglob("*"):
        relative = str(path.relative_to(package))
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path))
        elif path.is_file():
            result[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mode & 0o777,
            )
        elif path.is_dir():
            result[relative] = ("directory",)
        else:
            raise UpdateError(f"Cannot back up a special file in the installed package: {path}")
    return result


def backup_installation(root: Path, plugins: list[dict], marketplace: dict) -> Path:
    installed = installed_package(plugins[0]) if plugins else None
    scratch = root / ".scratch"
    scratch.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="dev-update-backup-", dir=scratch)).resolve()
    recovery = backup / "marketplace"
    try:
        if installed:
            shutil.copytree(installed, recovery / "plugin", symlinks=True)
            if package_contents(installed) != package_contents(recovery / "plugin"):
                raise UpdateError("Installed YSK files changed while backing them up; retry later.")
            catalog = recovery / ".agents/plugins/marketplace.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(
                json.dumps(
                    {
                        "name": NAME,
                        "plugins": [
                            {
                                "name": NAME,
                                "source": {"source": "local", "path": "./plugin"},
                                "policy": {
                                    "installation": "AVAILABLE",
                                    "authentication": "ON_INSTALL",
                                },
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )
        else:

            def ignore(directory: str, names: list[str]) -> list[str]:
                return [
                    name
                    for name in names
                    if name in {".git", ".venv", "__pycache__"}
                    or (Path(directory) / name).resolve() == backup
                ]

            shutil.copytree(marketplace["root"], recovery, symlinks=True, ignore=ignore)
        (backup / "previous.json").write_text(
            json.dumps({"marketplace": marketplace, "plugins": plugins}, indent=2) + "\n"
        )
    except BaseException:
        shutil.rmtree(backup)
        raise
    return backup


def set_enabled(root: Path, plugin_id: str, enabled: bool) -> None:
    with (
        tempfile.TemporaryFile() as errors,
        subprocess.Popen(
            [
                "codex",
                "app-server",
                "--listen",
                "stdio://",
                "--disable",
                "remote_plugin",
                "--disable",
                "apps",
            ],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        ) as process,
        selectors.DefaultSelector() as selector,
    ):
        assert process.stdin is not None and process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        pending = bytearray()
        identifier = 0

        def send(message: dict) -> None:
            assert process.stdin is not None
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        def request(method: str, params: dict) -> dict:
            nonlocal identifier, pending
            identifier += 1
            send({"id": identifier, "method": method, "params": params})
            deadline = time.monotonic() + 30
            while True:
                if time.monotonic() >= deadline:
                    raise UpdateError(f"Codex app-server timed out during {method}")
                while b"\n" not in pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise UpdateError(f"Codex app-server timed out during {method}")
                    assert process.stdout is not None
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise UpdateError(f"Codex app-server exited during {method}")
                    pending.extend(chunk)
                line, _, pending = pending.partition(b"\n")
                response = json.loads(line)
                if response.get("id") != identifier:
                    continue
                if "error" in response:
                    raise UpdateError(f"Codex {method} failed: {response['error']}")
                return response["result"]

        try:
            request(
                "initialize",
                {
                    "clientInfo": {"name": "ysk-development-updater", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            send({"method": "initialized", "params": {}})
            layers = request("config/read", {"includeLayers": True})["layers"]
            user = next(layer for layer in layers if layer["name"]["type"] == "user")
            request(
                "config/value/write",
                {
                    "keyPath": "plugins." + json.dumps(plugin_id) + ".enabled",
                    "value": enabled,
                    "mergeStrategy": "replace",
                    "filePath": user["name"]["file"],
                    "expectedVersion": user["version"],
                },
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def install(root: Path, bundle: Path) -> None:
    require_closed_codex(root)
    plugins, marketplace = inventory(root)
    version = json.loads((bundle / ".codex-plugin/plugin.json").read_text())["version"]
    if (
        marketplace
        and Path(marketplace["root"]).resolve() == bundle
        and plugins
        and plugins[0].get("version") == version
        and plugins[0].get("enabled")
    ):
        print("This development build is already installed and enabled.", flush=True)
        return
    backup = None
    if marketplace:
        backup = backup_installation(root, plugins, marketplace)
    try:
        # Removing the plugin alone leaves the conflicting marketplace registration.
        for plugin in plugins:
            codex(root, "remove", plugin["pluginId"])
        if marketplace:
            codex(root, "marketplace", "remove", NAME)
        codex(root, "marketplace", "add", str(bundle))
        codex(root, "add", PLUGIN)
        installed, registered = inventory(root)
        if (
            len(installed) != 1
            or installed[0].get("version") != version
            or not installed[0].get("enabled")
            or not registered
            or Path(registered["root"]).resolve() != bundle
        ):
            raise UpdateError("Codex did not report the expected local development installation.")
    except (Exception, KeyboardInterrupt) as error:
        print(f"Installation failed: {error}", file=sys.stderr, flush=True)
        try:
            current, registered = inventory(root)
            for plugin in current:
                codex(root, "remove", plugin["pluginId"])
            if registered:
                codex(root, "marketplace", "remove", NAME)
            if backup:
                codex(root, "marketplace", "add", str(backup / "marketplace"))
                for plugin in plugins:
                    codex(root, "add", plugin["pluginId"])
                    if not plugin["enabled"]:
                        set_enabled(root, plugin["pluginId"], False)
                restored, registered = inventory(root)
                if (
                    not registered
                    or Path(registered["root"]).resolve() != (backup / "marketplace").resolve()
                ):
                    raise UpdateError("Restored marketplace does not match the local backup")
                if [(p["pluginId"], p["version"], p["enabled"]) for p in restored] != [
                    (p["pluginId"], p["version"], p["enabled"]) for p in plugins
                ]:
                    raise UpdateError("Restored plugin identity, version, or enabled state differs")
                if restored and package_contents(
                    installed_package(restored[0])
                ) != package_contents(backup / "marketplace/plugin"):
                    raise UpdateError("Restored plugin files do not match the installed backup")
                print(f"Restored the previous files from a local backup: {backup}", file=sys.stderr)
        except (Exception, KeyboardInterrupt) as rollback_error:
            print(
                f"Automatic restoration failed: {rollback_error}\nBackup: {backup}", file=sys.stderr
            )
        raise UpdateError(
            "Development installation failed; see restoration details above."
        ) from error
    if backup:
        shutil.rmtree(backup)
    print(
        f"Installed {PLUGIN} {version}. Your YSK settings and notes were not changed.", flush=True
    )
    print(
        "Reopen the Codex client, then open /hooks and review/trust the four plugin hooks.",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Merge, test, and build without changing the installed plugin",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Use this checkout as-is, without fetching or merging upstream",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if not args.prepare_only:
            require_closed_codex(root)
        scratch = root / ".scratch"
        scratch.mkdir(exist_ok=True)
        with (scratch / "dev-update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise UpdateError("Another development update is already running.") from error
            if args.no_fetch:
                check_git_state(root)
                print("Skipping upstream fetch and merge (--no-fetch).", flush=True)
            else:
                update_source(root)
            validate(root)
            bundle = build(root)
            if args.prepare_only:
                print("Preparation complete. The installed plugin was not changed.", flush=True)
            else:
                install(root, bundle)
        return 0
    except (OSError, ValueError, UpdateError) as error:
        print(f"Update stopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Update interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
