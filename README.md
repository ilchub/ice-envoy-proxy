# Envoy APT packages

The workflow publishes the latest stable Envoy GitHub release as `envoy` Debian
packages for `amd64` and `arm64`. It runs daily at 03:23 UTC and can be started
manually from `main`. It does not run on pushes or pull requests. The upstream
GitHub release asset SHA-256 digests and ELF architecture are checked before
packaging; these digests are not an independent verification of Envoy's
release signature. The binaries are never executed on the publisher.

## Publisher setup

1. Register a Linux self-hosted runner with the label `apt-iceinc-solutions`,
   running as `actions`. Install Python 3.8 or newer, `dpkg-deb`,
   `dpkg-scanpackages` (from `dpkg-dev`), and GnuPG.
2. Create `/var/lib/apt-iceinc-solutions/gnupg`, owned by `actions` and mode
   `0700`. Provision an unattended APT signing key there. Set the repository
   Actions variable `APT_SIGNING_KEY` to its full primary key fingerprint.
   The workflow must be able to sign without interactive input.
3. Ensure `actions` can write to `/var/www/apt.iceinc.solutions`. The workflow
   manages only `envoy` (a symlink) and `.envoy-snapshots` beneath that
   directory. It refuses to replace an existing non-symlink `envoy` path.
   Snapshots and old packages are retained; monitor disk usage.
4. Configure the web server to serve `https://apt.iceinc.solutions/envoy/`
   and follow the managed symlink. Restrict its document root or access rules:
   the runner user's home is the web directory, so runner files, dotfiles,
   workspaces, credentials and other non-public paths **must not** be exposed.
   The signing key must stay outside the web root.

The repository is a flat signed APT repository, not a component of any existing
repository at the web root. Each complete snapshot is built and signed before
the public symlink is switched. A failed build leaves the previous snapshot
available. Old package files remain in subsequent snapshots so an APT client
using an older index can still download them.

## Client setup

Distribute the signing public key through a trusted channel, and verify its
fingerprint independently before installing it on clients. For example, after
placing the verified binary keyring at `/usr/share/keyrings/ice-envoy.gpg`:

```text
deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/ice-envoy.gpg] https://apt.iceinc.solutions/envoy/ ./
```

Add that line to an APT sources list, then run `apt update` and
`apt install envoy`. This installs the official core binary at
`/usr/bin/envoy`; it does not install a service or configuration.

## Runner security

The job checks the event and `main` ref and checks out only that workflow
revision. It accepts no workflow inputs and runs no third-party action other
than a SHA-pinned checkout. These controls do **not** prevent another workflow
in a public-repository PR or a writable branch from targeting a
repository-level self-hosted runner. GitHub recommends against using
self-hosted runners with public repositories. Restrict who can approve fork
workflows and push branches, protect `main`, and review every workflow before
approval; these are operational mitigations, not a complete isolation
boundary. A private publisher or an organization runner group restricted to a
trusted workflow is needed for stronger isolation.
