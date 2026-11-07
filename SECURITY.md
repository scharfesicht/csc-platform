# Security and trust boundary

This is a **public distribution repository for a small bootstrap**, not a repository of the product source or private installer.

- Never publish Docker Hub read/write tokens, supplier signing keys, customer credentials, `.env` files, customer licenses, TLS private keys, or ABP commercial source/packages.
- The bootstrap downloads **digest-pinned** images from `docker.io/scharfesicht`; users must authenticate to private repositories with a **read-only** customer token. **Do not use the supplier's publishing token on customer hosts.**
- The bootstrap rejects unsupported Ubuntu/CPU versions and refuses to overwrite existing installations. Review and test its installation logic before running it as root.
- The SHA-256 file detects accidental content changes, but a malicious actor controlling both GitHub files could change both. Pin an audited commit or protected signed tag and independently verify its published digest.
- Public visibility of this bootstrap does **not** grant rights to deploy, copy, sublicense or redistribute the private application. The product runtime enforces its own signed licensing restrictions; offline clone resistance is not absolute.
- Report vulnerabilities privately to the ScharfeSicht security contact through the organization's established channels; avoid disclosing secrets in public GitHub issues.
