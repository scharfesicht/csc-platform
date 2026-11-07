# CSC Integration Platform - public bootstrap

This public repository contains only deployment coordination, not the private application or actual installer.
The actual installer and six runtime images come from **private ScharfeSicht Docker Hub repositories**.

**Bootstrap workflow:** v1.0.0-rc5. The approved application release is recorded in `bootstrap/release.json`.
The initially bundled pointer is the already-published **1.0.0-rc2** installer.

## Install or resume

On Ubuntu 24.04/26.04 LTS, x86-64, with Python 3.12+, OpenSSL and curl:

```bash
git clone https://github.com/scharfesicht/csc-platform.git
cd csc-platform
sudo bash install.sh
```

The guided command detects fresh installation, files awaiting initialization, or configured startup awaiting recovery.
It reuses existing customer configuration. **Do not delete `/opt/csc-platform` or PostgreSQL volumes.**
On the current `app-test-server`, the same command resumes using the new approved rc2 release; it does not ask you to recreate the administrator account.

For a new site it requests the management hostname, administrator email, HTTPS files, administrator password and a customer **read-only** Docker Hub login.
A self-signed certificate is offered only as an explicitly selected isolated-lab option. Production needs a trusted certificate and a real DNS name.
Docker installation is offered on a clean host only and requires consent. Existing Docker/containerd installations are never replaced.

Passwords are validated and retried before installation state is committed. The current product policy remains 16 characters with uppercase/lowercase/number/symbol; this workflow does not silently change ABP identity policy.

Registry credentials can be remembered **only with explicit consent**. They are then held in a root-only Docker configuration outside the application. Docker's config auth is base64, not encryption. Never use a supplier publishing token on a customer server. Tokens still need renewal when expired or revoked.

## Maintenance

```bash
sudo bash status.sh
sudo bash verify.sh
sudo bash backup.sh
sudo bash upgrade.sh
```

`upgrade.sh` reads only the public approved-release metadata from ScharfeSicht GitHub over HTTPS. It does not download/execute new program code. It then pulls the pinned private release, obtains confirmation, creates an encrypted backup, preserves configuration and runs migration/startup through the private controller.
For an intentionally pinned checkout use `sudo bash upgrade.sh --use-local-release`.

The first backup/upgrade asks for an **age public recipient**. Keep its recovery private key with your approved backup custodian, not in this public repository or alongside customer images. The recipient is remembered; no private recovery key is required for making backups. The encrypted backup includes PostgreSQL, installation secrets, key storage, controller files and configuration. Copy backups off-server and perform a restore acceptance test.

First-start recovery (no successful running-release marker) preserves the existing database and secrets without requiring a production backup-enrollment step. Established installations require an encrypted backup before upgrading. An upgrade is never treated as a fresh install. PostgreSQL major-version changes and signing-trust changes are refused automatically. No automatic rollback of database migrations is attempted.

For a failed start, rerun `install.sh` after the release has been corrected. Interrupted upgrades retain a journal and resume the same target; a changed target or changed secrets is rejected for review.

Run `sudo bash install.sh --help` for optional input arguments such as `--hostname`, `--registry-user`, `--no-cache`, `--subnet`, and `--install-dir`.
Detailed root-only logs and recovery snapshots are under `/var/lib/csc-platform-bootstrap/`; encrypted backups are under `/var/backups/csc-platform/`.

## Release trust and commissioning

No customer is asked to copy a SHA-256 image reference or call `cscctl.py` manually. Checksums, pinned digests, platform checks and product-license gates remain enforced internally.
Checksums in the same repository detect accidental corruption, not a malicious change to the repository itself. Review/protect supplier commits and tags through your normal release controls.

A healthy deployment is not permission to send production attendance. Commission the product license, employee/Civil ID mappings, TNA mappings, CSC credentials and limited live acceptance before enabling synchronization. This bootstrap never enables synchronization by itself.

## Local tests

```bash
python3 -m unittest discover -s tests -v
```

These are offline orchestration tests. Live Ubuntu/Docker/database/browser/CSC acceptance is still required.
