# Security Policy

Jupiter Sentinel handles wallet configuration, Jupiter API access, and live-trading code paths. Treat this repository as sensitive software even when you are only experimenting locally.

## Supported Versions

This project does not publish versioned security releases yet. Security fixes should be assumed to land on the latest default branch only.

| Version | Supported |
| --- | --- |
| Latest `main` / default branch | Yes |
| Older commits, forks, and local snapshots | No |

## Reporting a Vulnerability

Please do not open a public issue for a live vulnerability.

Preferred process:

1. Use GitHub private vulnerability reporting / a repository security advisory if it is enabled for this repo.
2. If private reporting is unavailable, contact the maintainers directly and include:
   - a clear description of the issue
   - affected files or modules
   - reproduction steps
   - impact assessment
   - any suggested mitigation or patch
3. Give the maintainers reasonable time to triage and prepare a fix before public disclosure.

When reporting, avoid including real private keys, API keys, or mainnet wallet balances in the report body.

## Security Practices

### Secrets and wallet material

- Never commit Solana keypair files, `.env` files, or API keys.
- Keep the signing key outside the repository and point to it with `SOLANA_PRIVATE_KEY_PATH`.
- The code enforces private-key file permissions on Unix-like systems and will reject files that are group/world readable. Use:

```bash
chmod 600 /path/to/keypair.json
```

- `SOLANA_PUBLIC_KEY` can be used for read-only wallet access when you do not want to expose a signing key.
- `JUP_API_KEY` should be injected through the environment, not hard-coded in source.

### Safe runtime modes

- `python demo.py` is the safest entry point. It runs entirely with mocked Jupiter and Solana RPC responses.
- `python -m src.main` defaults to dry-run mode unless `--live` is passed.
- Only use `python -m src.main --live` after reviewing the exact execution path, wallet configuration, and risk parameters.

### Input validation and request construction

The repo includes validation helpers in `src/validation.py` to reduce malformed or unsafe inputs:

- Solana addresses are validated before being used in quote URLs.
- Numeric request parameters such as amounts, basis points, and ports are range-checked.
- Quote URLs are built from validated parameters instead of string concatenation.
- Host and port validation is used for local dashboard bindings.

If you add new network-facing parameters, validate them before using them in requests.

### Network and API hygiene

- Respect Jupiter rate limits. Quote-heavy features can exhaust the keyless bucket quickly.
- Prefer adding `JUP_API_KEY` for higher limits and consistent auth headers.
- Cache slow-changing metadata such as `/swap/v1/program-id-to-label` where practical.
- Add backoff / retry handling around `429` and transient transport failures before increasing scan frequency.
- Do not rely on demo output as evidence that live endpoints are reachable or safe to use.

### Dependency and supply-chain hygiene

- Keep `requirements.txt` dependencies updated.
- Review dependency changes before upgrading packages used for signing, transaction encoding, or RPC calls.
- Run the test suite after changing quote parsing, execution, validation, or wallet-loading code.
- Prefer mocked-network tests for deterministic coverage of trading logic.

### Logging and artifacts

- Do not log private keys, raw seed phrases, or full secret-bearing environment dumps.
- Treat files written under `data/` and `logs/` as potentially sensitive operational artifacts.
- Scrub wallet addresses, balances, and trade history before sharing screenshots or reports publicly.

## Operational Limits

- This codebase is experimental and has not been independently audited.
- Live trading on Solana mainnet carries real financial risk.
- The repository is not designed for custody, multi-tenant operation, or internet-exposed wallet services.
- If you need production use, add stronger secret management, transport retries, alerting, auditing, and a formal review process before deployment.
