#!/usr/bin/env python3
"""Publish the latest stable Envoy release to a dedicated flat APT repository."""

import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request


ROOT = Path("/var/www/apt.iceinc.solutions")
API = "https://api.github.com/repos/envoyproxy/envoy/releases/latest"
ARCHES = {"amd64": ("x86_64", 62), "arm64": ("aarch_64", 183)}
MAX_BINARY_SIZE = 512 * 1024 * 1024


def fail(message):
    raise RuntimeError(message)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request(url, token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ice-envoy-packager"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def get_release(token):
    with request(API, token) as response:
        data = json.load(response)
    if data.get("draft") or data.get("prerelease"):
        fail("The latest upstream release is not stable")
    match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+)", data.get("tag_name", ""))
    if not match:
        fail("Unexpected upstream release tag")
    version = match.group(1)
    timestamp = int(datetime.datetime.fromisoformat(data["published_at"].replace("Z", "+00:00")).timestamp())
    assets = {}
    for arch, (upstream_arch, _) in ARCHES.items():
        name = f"envoy-{version}-linux-{upstream_arch}"
        found = [asset for asset in data["assets"] if asset.get("name") == name]
        if len(found) != 1:
            fail(f"Missing or duplicate release asset: {name}")
        digest = found[0].get("digest") or ""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            fail(f"Missing or invalid GitHub SHA-256 digest for {name}")
        size = found[0].get("size")
        if not isinstance(size, int) or not 0 < size <= MAX_BINARY_SIZE:
            fail(f"Invalid asset size for {name}")
        assets[arch] = {"name": name, "digest": digest[7:], "size": size}
    return version, timestamp, assets


def download_binary(version, asset, machine, output):
    url = f"https://github.com/envoyproxy/envoy/releases/download/v{version}/{asset['name']}"
    digest = hashlib.sha256()
    size = 0
    with request(url) as response, output.open("wb") as stream:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > asset["size"]:
                fail(f"Asset exceeds expected size: {asset['name']}")
            digest.update(chunk)
            stream.write(chunk)
    if size != asset["size"] or digest.hexdigest() != asset["digest"]:
        fail(f"Checksum or size mismatch for {asset['name']}")
    with output.open("rb") as stream:
        header = stream.read(20)
    if (len(header) != 20 or header[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(header[16:18], "little") not in (2, 3)
            or int.from_bytes(header[18:20], "little") != machine):
        fail(f"Unexpected ELF architecture for {asset['name']}")


def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def build_package(directory, version, timestamp, arch, binary):
    package_root = directory / f"build-{arch}"
    control_dir = package_root / "DEBIAN"
    bin_dir = package_root / "usr" / "bin"
    control_dir.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    installed = bin_dir / "envoy"
    shutil.copyfile(binary, installed)
    installed.chmod(0o755)
    control = control_dir / "control"
    control.write_text(
        f"Package: envoy\nVersion: {version}\nArchitecture: {arch}\n"
        "Maintainer: ICE Inc.\nSection: net\nPriority: optional\n"
        "Description: Envoy cloud-native proxy\n"
        " Envoy is an open source edge and service proxy.\n",
        encoding="utf-8",
    )
    for path in (installed, control, bin_dir, bin_dir.parent, control_dir, package_root):
        os.utime(path, (timestamp, timestamp))
    output = directory / f"envoy_{version}_{arch}.deb"
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(timestamp)
    run(["dpkg-deb", "--root-owner-group", "--build", str(package_root), str(output)], env=env)
    shutil.rmtree(package_root)
    return output


def generate_metadata(snapshot, fingerprint):
    with (snapshot / "Packages").open("wb") as output:
        run(["dpkg-scanpackages", "--multiversion", ".", "/dev/null"], cwd=snapshot, stdout=output)
    with (snapshot / "Packages").open("rb") as source, (snapshot / "Packages.gz").open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            shutil.copyfileobj(source, compressed)
    now = datetime.datetime.now(datetime.timezone.utc)
    lines = [
        "Origin: ICE Inc.",
        "Label: ICE Envoy",
        "Suite: stable",
        "Codename: stable",
        "Architectures: amd64 arm64",
        f"Date: {now.strftime('%a, %d %b %Y %H:%M:%S UTC')}",
        "Description: ICE Envoy packages",
        "SHA256:",
    ]
    for name in ("Packages", "Packages.gz"):
        path = snapshot / name
        lines.append(f" {sha256(path)} {path.stat().st_size:16d} {name}")
    (snapshot / "Release").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, options in (("InRelease", ["--clearsign"]), ("Release.gpg", ["--detach-sign", "--armor"])):
        run(["gpg", "--batch", "--yes", "--pinentry-mode", "error", "--local-user", fingerprint,
             "--output", str(snapshot / name),
             *options, str(snapshot / "Release")])


def current_snapshot(root):
    link = root / "envoy"
    if not link.is_symlink():
        if link.exists():
            fail(f"{link} exists and is not the managed repository symlink")
        return None
    target = link.resolve(strict=True)
    snapshots = (root / ".envoy-snapshots").resolve(strict=True)
    if target.parent != snapshots or not target.is_dir():
        fail(f"{link} does not point to a managed snapshot")
    return target


def publish(root, token, fingerprint):
    if not re.fullmatch(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}", fingerprint):
        fail("APT_SIGNING_KEY must be a full OpenPGP fingerprint")
    fingerprint = fingerprint.upper()
    if not root.is_dir():
        fail(f"Repository root does not exist: {root}")
    version, timestamp, assets = get_release(token)
    current = current_snapshot(root)
    if current:
        with (current / "manifest.json").open(encoding="utf-8") as stream:
            previous = json.load(stream)
        if (previous["version"] == version and previous["assets"] == assets
                and previous["signing_key"] == fingerprint
                and set(previous["packages"]) == {f"envoy_{version}_{arch}.deb" for arch in ARCHES}
                and all((current / name).is_file() and sha256(current / name) == digest
                        for name, digest in previous["packages"].items())
                and set(previous["metadata"]) == {"Packages", "Packages.gz", "Release", "InRelease", "Release.gpg"}
                and all((current / name).is_file() and sha256(current / name) == digest
                        for name, digest in previous["metadata"].items())):
            print(f"Envoy {version} is already published")
            return

    snapshots = root / ".envoy-snapshots"
    snapshots.mkdir(mode=0o755, exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix="snapshot-", dir=snapshots))
    snapshot.chmod(0o755)
    switched = False
    link_temp = root / f".envoy-link-{snapshot.name}"
    try:
        if current:
            for package in current.glob("*.deb"):
                os.link(package, snapshot / package.name)
        packages = {}
        with tempfile.TemporaryDirectory(prefix="envoy-build-") as build_dir:
            build_dir = Path(build_dir)
            for arch, (_, machine) in ARCHES.items():
                binary = build_dir / f"envoy-{arch}"
                download_binary(version, assets[arch], machine, binary)
                package = build_package(build_dir, version, timestamp, arch, binary)
                destination = snapshot / package.name
                if destination.exists():
                    if sha256(destination) != sha256(package):
                        fail(f"Refusing to replace an existing package: {package.name}")
                else:
                    shutil.copyfile(package, destination)
                    destination.chmod(0o644)
                packages[package.name] = sha256(destination)
        generate_metadata(snapshot, fingerprint)
        metadata = {name: sha256(snapshot / name)
                    for name in ("Packages", "Packages.gz", "Release", "InRelease", "Release.gpg")}
        (snapshot / "manifest.json").write_text(
            json.dumps({"version": version, "assets": assets, "signing_key": fingerprint,
                        "packages": packages, "metadata": metadata}, sort_keys=True) + "\n", encoding="utf-8")
        os.symlink(os.path.relpath(snapshot, root), link_temp)
        os.replace(link_temp, root / "envoy")
        switched = True
        print(f"Published Envoy {version} for amd64 and arm64")
    finally:
        link_temp.unlink(missing_ok=True)
        if not switched:
            shutil.rmtree(snapshot)


def main():
    if os.environ.get("GNUPGHOME") != "/var/lib/apt-iceinc-solutions/gnupg":
        fail("GNUPGHOME must be the dedicated directory outside the web root")
    if pwd.getpwuid(os.geteuid()).pw_name != "actions":
        fail("The publisher must run as the actions user")
    gnupg_home = Path(os.environ["GNUPGHOME"]).resolve(strict=True)
    if gnupg_home == ROOT.resolve() or ROOT.resolve() in gnupg_home.parents:
        fail("The signing key directory must not be inside the web root")
    publish(ROOT, os.environ.get("GH_TOKEN"), os.environ.get("APT_SIGNING_KEY", ""))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Publishing failed: {error}", file=sys.stderr)
        sys.exit(1)
