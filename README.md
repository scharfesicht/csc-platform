# CSC Integration Platform — Public Bootstrap

**Publisher:** ScharfeSicht  
**Public repository:** `https://github.com/scharfesicht/csc-platform` (publish manually)  
**Bootstrap release:** `1.0.0-rc1`  
**Platform:** Ubuntu 24.04 LTS, `linux/amd64`

This repository distributes **only the public, reviewable bootstrap**. It does **not** contain the application, ABP/.NET source, Docker image builds, credentials, license-signing keys, customer configuration or the actual installer.

## Delivery separation

| Public on GitHub | Private on Docker Hub |
| --- | --- |
| `scharfesicht/csc-platform` — bootstrap, checksum, instructions | `scharfesicht/csc-platform-installer` — pinned installer image |
| No product source or credentials | API, worker, migrator, frontend, PostgreSQL, Valkey images |

The bootstrap is pinned to this **already-published** installer image digest:

`docker.io/scharfesicht/csc-platform-installer@sha256:02c9f5fd519175be5a7cb7951f9552cbb62e57604ca7daf8938d7243b1f362f5`

The private images require a customer-specific **read-only Docker Hub account/token**. Registry access is distinct from the separately enforced ScharfeSicht product license. Publishing this bootstrap does not authorize a third party to redistribute or operate the application.

## Verify the bootstrap

```bash
(cd bootstrap && sha256sum -c csc-bootstrap.sh.sha256)
```

Do **not** run arbitrary content directly through `curl | bash` as root. Review the script and verify its checksum. For production, pin a reviewed GitHub commit or protected release tag, and verify the expected checksum from a trusted independent channel.

## First customer/test installation

On a fresh Ubuntu 24.04 x86_64 system, make the bootstrap and checksum available from a trusted release of this repository. For example, **after this repository has actually been published**, use a pinned, approved tag:

```bash
BASE=https://raw.githubusercontent.com/scharfesicht/csc-platform/v1.0.0-rc1/bootstrap
curl -fL --proto '=https' --tlsv1.2 -o csc-bootstrap.sh "$BASE/csc-bootstrap.sh"
curl -fL --proto '=https' --tlsv1.2 -o csc-bootstrap.sh.sha256 "$BASE/csc-bootstrap.sh.sha256"
sha256sum -c csc-bootstrap.sh.sha256
sudo bash csc-bootstrap.sh check
```

Then install using a **customer-scoped read-only registry identity** and real TLS files:

```bash
sudo bash csc-bootstrap.sh install \
  --hostname csc-test.example.internal \
  --admin-email administrator@example.internal \
  --tls-cert /secure/fullchain.pem \
  --tls-key /secure/privkey.pem \
  --registry-user CUSTOMER_READONLY_DOCKERHUB_USER \
  --install-docker
```

Omit `--install-docker` when Docker Engine and Compose are already working. The script prompts on the terminal for the registry token and initial administrator password. Never put either secret in GitHub, shell history, a URL, or a command argument. Use `--stage-only` to avoid starting services during commissioning.

After deployment:

```bash
sudo bash csc-bootstrap.sh status
```

**This release remains a test candidate** until live Ubuntu deployment, license activation, migrations, first login, BioStar/Time365 mapping, CSC acknowledgments and recovery procedures pass acceptance checks. Synchronization is paused by default.

## Publishing rules

Only the allowlisted files in this repository may be made public: `bootstrap/csc-bootstrap.sh`, `bootstrap/csc-bootstrap.sh.sha256`, `.gitignore`, `README.md`, `SECURITY.md`, and `LICENSE.md`. Do not place the private product solution or supplier signing material in this repository. The `.gitignore` is deny-by-default, but **review `git status` and the staged file list before every public push**.

This repository is **bootstrap-only**. Upgrades to an existing customer deployment are a separate audited workflow; never overwrite an existing `/opt/csc-platform` installation with the bootstrap.
