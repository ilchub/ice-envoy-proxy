import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import publish_envoy as publisher

download_binary = publisher.download_binary


def elf(machine):
    return b"\x7fELF\x02\x01" + bytes(10) + (3).to_bytes(2, "little") + machine.to_bytes(2, "little")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = "A" * 40
        self.binaries = {arch: elf(machine) for arch, (_, machine) in publisher.ARCHES.items()}
        self.assets = {
            arch: {"name": f"envoy-1.2.3-linux-{upstream}", "size": len(self.binaries[arch]),
                   "digest": hashlib.sha256(self.binaries[arch]).hexdigest()}
            for arch, (upstream, _) in publisher.ARCHES.items()
        }

    def fake_download(self, version, asset, machine, output):
        data = next(self.binaries[arch] for arch, (_, expected) in publisher.ARCHES.items() if expected == machine)
        with mock.patch.object(publisher, "request", return_value=io.BytesIO(data)):
            download_binary(version, asset, machine, output)

    def fake_build(self, directory, version, timestamp, arch, binary):
        output = directory / f"envoy_{version}_{arch}.deb"
        output.write_bytes(binary.read_bytes())
        return output

    def fake_metadata(self, snapshot, fingerprint):
        for name in ("Packages", "Packages.gz", "Release", "InRelease", "Release.gpg"):
            (snapshot / name).write_text(name, encoding="utf-8")

    def publish(self):
        with mock.patch.object(publisher, "get_release", return_value=("1.2.3", 1, self.assets)), \
                mock.patch.object(publisher, "download_binary", side_effect=self.fake_download), \
                mock.patch.object(publisher, "build_package", side_effect=self.fake_build), \
                mock.patch.object(publisher, "generate_metadata", side_effect=self.fake_metadata):
            publisher.publish(self.root, None, self.key)

    def test_publishes_both_architectures_and_skips_unchanged(self):
        self.publish()
        first = publisher.current_snapshot(self.root)
        self.assertEqual({p.name for p in first.glob("*.deb")},
                         {"envoy_1.2.3_amd64.deb", "envoy_1.2.3_arm64.deb"})
        self.publish()
        self.assertEqual(first, publisher.current_snapshot(self.root))

    def test_metadata_damage_rebuilds(self):
        self.publish()
        first = publisher.current_snapshot(self.root)
        (first / "Packages").unlink()
        self.publish()
        self.assertNotEqual(first, publisher.current_snapshot(self.root))
        self.assertTrue((publisher.current_snapshot(self.root) / "Packages").exists())

    def test_new_release_retains_old_packages(self):
        self.publish()
        assets = {
            arch: dict(asset, name=asset["name"].replace("1.2.3", "1.2.4"))
            for arch, asset in self.assets.items()
        }
        with mock.patch.object(publisher, "get_release", return_value=("1.2.4", 2, assets)), \
                mock.patch.object(publisher, "download_binary", side_effect=self.fake_download), \
                mock.patch.object(publisher, "build_package", side_effect=self.fake_build), \
                mock.patch.object(publisher, "generate_metadata", side_effect=self.fake_metadata):
            publisher.publish(self.root, None, self.key)
        self.assertEqual({p.name for p in publisher.current_snapshot(self.root).glob("*.deb")},
                         {f"envoy_{version}_{arch}.deb"
                          for version in ("1.2.3", "1.2.4") for arch in publisher.ARCHES})

    def test_failed_build_preserves_previous_snapshot(self):
        self.publish()
        first = publisher.current_snapshot(self.root)
        (first / "Packages").unlink()
        with mock.patch.object(publisher, "get_release", return_value=("1.2.3", 1, self.assets)), \
                mock.patch.object(publisher, "download_binary", side_effect=RuntimeError("bad download")):
            with self.assertRaisesRegex(RuntimeError, "bad download"):
                publisher.publish(self.root, None, self.key)
        self.assertEqual(first, publisher.current_snapshot(self.root))
        self.assertEqual([first], [p.resolve() for p in (self.root / ".envoy-snapshots").iterdir()])

    def test_digest_and_architecture_validation(self):
        asset = self.assets["amd64"]
        output = self.root / "binary"
        with mock.patch.object(publisher, "request", return_value=io.BytesIO(self.binaries["amd64"])):
            publisher.download_binary("1.2.3", asset, 62, output)
        with mock.patch.object(publisher, "request", return_value=io.BytesIO(self.binaries["amd64"])):
            with self.assertRaisesRegex(RuntimeError, "architecture"):
                publisher.download_binary("1.2.3", asset, 183, output)
        with mock.patch.object(publisher, "request", return_value=io.BytesIO(b"x" * asset["size"])):
            with self.assertRaisesRegex(RuntimeError, "Checksum"):
                publisher.download_binary("1.2.3", asset, 62, output)

    def test_invalid_key_and_existing_directory_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            publisher.publish(self.root, None, "invalid")
        (self.root / "envoy").mkdir()
        with mock.patch.object(publisher, "get_release", return_value=("1.2.3", 1, self.assets)):
            with self.assertRaisesRegex(RuntimeError, "not the managed"):
                publisher.publish(self.root, None, self.key)

    def test_release_asset_selection_rejects_missing_digest(self):
        release = {"draft": False, "prerelease": False, "tag_name": "v1.2.3",
                   "published_at": "2026-01-01T00:00:00Z",
                   "assets": [dict(asset, digest="sha256:" + asset["digest"]) for asset in self.assets.values()]}
        with mock.patch.object(publisher, "request", return_value=io.BytesIO(json.dumps(release).encode())):
            version, _, assets = publisher.get_release(None)
        self.assertEqual(version, "1.2.3")
        self.assertEqual(assets, self.assets)
        release["assets"][0]["digest"] = None
        with mock.patch.object(publisher, "request", return_value=io.BytesIO(json.dumps(release).encode())):
            with self.assertRaisesRegex(RuntimeError, "digest"):
                publisher.get_release(None)

    @unittest.skipUnless(shutil.which("gpg"), "gpg is required")
    def test_release_metadata_is_signed_and_matches_index(self):
        gnupg = self.root / "gnupg"
        gnupg.mkdir(mode=0o700)
        env = os.environ.copy()
        env["GNUPGHOME"] = str(gnupg)
        subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-generate-key",
                        "APT Test <apt-test@example.invalid>", "ed25519", "sign", "0"],
                       env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        listing = subprocess.check_output(["gpg", "--batch", "--with-colons", "--list-secret-keys"],
                                          env=env, text=True, stderr=subprocess.DEVNULL)
        fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        real_run = publisher.run

        def fake_scan(args, **kwargs):
            if args[0] == "dpkg-scanpackages":
                kwargs["stdout"].write(b"Package: envoy\nVersion: 1.2.3\n\n")
            else:
                real_run(args, **kwargs)

        with mock.patch.dict(os.environ, {"GNUPGHOME": str(gnupg)}), \
                mock.patch.object(publisher, "run", side_effect=fake_scan):
            publisher.generate_metadata(snapshot, fingerprint)
        for signature in ("InRelease", "Release.gpg"):
            args = ["gpg", "--batch", "--verify", str(snapshot / signature)]
            if signature == "Release.gpg":
                args.append(str(snapshot / "Release"))
            subprocess.run(args, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        release = (snapshot / "Release").read_text()
        for name in ("Packages", "Packages.gz"):
            self.assertIn(publisher.sha256(snapshot / name), release)


if __name__ == "__main__":
    unittest.main()
