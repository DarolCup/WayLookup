#!/usr/bin/env python3
"""Build release artifacts; optionally publish them through an authenticated GitHub CLI.

Default: local preparation only. --publish explicitly creates a PUBLIC repository/release.
Never reads tokens, changes credential settings or force-pushes repository history.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
DESCRIPTION = (
    "Click-to-translate OCR overlay for Arch Linux + Hyprland / Wayland, written in Python."
)


def run(*args, cwd=ROOT, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=capture)


def git(*args):
    return run("git", *args, capture=True).stdout.strip()


def prepare(version, head):
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    destination = ROOT / "release" / f"v{version}"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"waylookup-{version}.tar.gz"
    run(
        "git",
        "archive",
        "--format=tar.gz",
        f"--prefix=waylookup-{version}/",
        f"--output={archive}",
        head,
    )
    # Build from that exact commit in a temporary directory, not from stale dist/ files.
    with tempfile.TemporaryDirectory(prefix="waylookup-release-") as temporary:
        staging = Path(temporary)
        shutil.copy2(archive, staging / archive.name)
        shutil.copy2(ROOT / "packaging/PKGBUILD", staging / "PKGBUILD")
        run("makepkg", "--noconfirm", "--force", "-p", "PKGBUILD", cwd=staging)
        packages = list(staging.glob(f"waylookup-{version}-*-any.pkg.tar.zst"))
        wheels = list((staging / f"src/waylookup-{version}/dist").glob("*.whl"))
        if len(packages) != 1 or len(wheels) != 1:
            raise RuntimeError("Expected exactly one Arch package and one wheel")
        package = destination / packages[0].name
        wheel = destination / wheels[0].name
        shutil.copy2(packages[0], package)
        shutil.copy2(wheels[0], wheel)
    provenance = destination / "SOURCE-COMMIT.txt"
    provenance.write_text(head + "\n", encoding="utf-8")
    assets = [archive, package, wheel, provenance]
    checksum = destination / "SHA256SUMS"
    checksum.write_text(
        "".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(assets)
        ),
        encoding="utf-8",
    )
    assets.append(checksum)
    print(f"\nPrepared release from {head}: {destination}", flush=True)
    return assets


def authenticate(repo):
    if not shutil.which("gh"):
        raise RuntimeError(
            "GitHub CLI отсутствует. Установите: sudo pacman -S github-cli; затем gh auth login"
        )
    run("gh", "auth", "status", "--hostname", "github.com")
    login = json.loads(run("gh", "api", "user", capture=True).stdout)["login"]
    if login.casefold() != repo.split("/")[0].casefold():
        raise RuntimeError(
            f"В gh выбран {login}, а публикация запрошена в {repo}. Смените аккаунт или --repo."
        )


def publish(repo, version, head, assets):
    # Names and source URLs must not become command/option injection.
    url = f"https://github.com/{repo}"
    view = subprocess.run(
        ["gh", "repo", "view", repo, "--json", "isEmpty,isPrivate,defaultBranchRef"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if view.returncode:
        run("gh", "repo", "create", repo, "--public", "--description", DESCRIPTION)
    else:
        metadata = json.loads(view.stdout)
        if metadata["isPrivate"]:
            raise RuntimeError("Existing repository is private; refusing to change its visibility")
        if not metadata["isEmpty"]:
            remote_head = (metadata.get("defaultBranchRef") or {}).get("target", {}).get("oid")
            if not remote_head:
                # Resolve the default branch with a read-only REST request if GraphQL omits target.
                branch = metadata["defaultBranchRef"]["name"]
                remote_head = json.loads(
                    run("gh", "api", f"repos/{repo}/commits/{branch}", capture=True).stdout
                )["sha"]
            if subprocess.run(
                ["git", "merge-base", "--is-ancestor", remote_head, head],
                cwd=ROOT,
                capture_output=True,
            ).returncode:
                raise RuntimeError(
                    "Existing repository has unrelated or newer history; refusing to overwrite it"
                )
    # Use gh's normal credential helper for this command only; no global Git config changes.
    run(
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "push",
        url + ".git",
        "HEAD:refs/heads/main",
    )
    run(
        "gh",
        "repo",
        "edit",
        repo,
        "--default-branch",
        "main",
        "--description",
        DESCRIPTION,
        "--add-topic",
        "hyprland",
        "--add-topic",
        "wayland",
        "--add-topic",
        "ocr",
        "--add-topic",
        "translation",
        "--add-topic",
        "python",
        "--add-topic",
        "arch-linux",
    )
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True
    )
    if remote.returncode:
        run("git", "remote", "add", "origin", url + ".git")
    tag = f"v{version}"
    exists = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo], cwd=ROOT, capture_output=True
    )
    if exists.returncode == 0:
        raise RuntimeError(
            f"Release {tag} already exists; source was pushed, existing assets were left unchanged"
        )
    run(
        "gh",
        "release",
        "create",
        tag,
        *(str(p) for p in assets),
        "--repo",
        repo,
        "--target",
        head,
        "--title",
        f"WayLookup {version}",
        "--notes-file",
        str(ROOT / "docs/release-notes.md"),
    )
    metadata = json.loads(run("gh", "api", f"repos/{repo}/commits/main", capture=True).stdout)
    if metadata["sha"] != head:
        raise RuntimeError("Published branch differs from the prepared source commit")
    release = json.loads(run("gh", "api", f"repos/{repo}/releases/tags/{tag}", capture=True).stdout)
    if {p.name for p in assets} - {p["name"] for p in release["assets"]}:
        raise RuntimeError("One or more release assets are missing")
    print(f"\nPublished repository: {url}\nRelease: {release['html_url']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish a PUBLIC repository and release after preparation",
    )
    parser.add_argument(
        "--repo",
        default="DarolCup/WayLookup",
        help="Target OWNER/REPO (default: DarolCup/WayLookup)",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be OWNER/REPO")
    try:
        if git("status", "--porcelain"):
            raise RuntimeError("Commit project changes before preparing a release")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        version = project["version"]
        if f"pkgver={version}\n" not in (ROOT / "packaging/PKGBUILD").read_text():
            raise RuntimeError("PKGBUILD and Python versions differ")
        head = git("rev-parse", "HEAD")
        if args.publish:
            authenticate(args.repo)
        assets = prepare(version, head)
        if args.publish:
            publish(args.repo, version, head, assets)
        else:
            print(
                "No GitHub changes made. After gh auth login, run: python scripts/publish.py --publish"
            )
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Release failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
