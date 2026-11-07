# Security and trust boundary

This is a public deployment coordinator. The actual installer, ABP runtime, product source, supplier licensing utility and customer data remain private.

Never publish tokens, `.env`, licenses, private keys or commercial framework source here. Images are restricted to `docker.io/scharfesicht/csc-platform-*` with immutable digests and linux/amd64 verification. The public pointer itself is authorized by ScharfeSicht's GitHub publishing process; SHA-256 checksums are integrity checks, not an independent signature.

Customer hosts use read-only image credentials. Supplier publication and Docker Hub management tokens must never be issued to customers. Remembering a read-only customer login is opt-in; its Docker config is root-only, but base64 auth is not encryption. An expired/revoked token requires re-enrollment.

The coordinator refuses symlink-managed paths, unrelated directories, concurrent operations, changed product signing trust, unpinned images, downgrades and automatic PostgreSQL major-version transitions. Existing secrets and volume data are preserved. Upgrades require a fresh encrypted backup; a failed upgrade is not automatically rolled back across database migrations.

Product licensing is independent of registry access. A server owner with full host control can clone software/files; purely offline checks are not absolute anti-copy protection.

Report vulnerabilities through ScharfeSicht's established private security channel. Do not post secrets or customer records in public issues.
