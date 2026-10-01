# Aquaservice API client

Download your Aquaservice Spain invoices and check your next delivery date without browser automation. This unofficial, read-only client uses the customer portal's API and Python's standard library.

## Requirements

- Python 3.10 or newer; no third-party runtime dependencies.
- An Aquaservice account and an authenticated session in the customer website.
- A private `credentials.json` file (see path defaults below).

## Installation

Install this checkout into a virtual environment:

```sh
cd /path/to/aquaservice-api
uv venv
uv pip install .
.venv/bin/aquaservice-api --help
```

With an activated virtual environment, `python -m pip install .` also works.
The distribution is named **`aquaservice-api`**; the importable module is
**`aquaservice`**. The installed command is `aquaservice-api`.
Direct execution with `python3 aquaservice.py` remains supported without installation.

Once the packaging changes are published to GitHub, another project's
`pyproject.toml` can declare this dependency without requiring a separate clone:

```toml
dependencies = [
    "aquaservice-api @ git+https://github.com/bart-gander/aquaservice-api.git@<full-commit-sha>",
]
```

Replace `<full-commit-sha>` with a published commit containing `pyproject.toml`;
it is a placeholder, not a usable revision. `uv sync` (or pip installing that
project) fetches and installs the dependency automatically. Git must be available
for VCS installation; PyPI publication is not required.

## Python library

Normal imports reuse the same code as the CLI; importing the module does not read
credentials, call the API, or run the CLI:

```python
from pathlib import Path
from aquaservice import ClientError, get_next_delivery_date, load_credentials

try:
    credentials = load_credentials(Path("/private/path/credentials.json"))
    delivery_date = get_next_delivery_date(credentials)
except (ClientError, OSError, ValueError):
    # Do not log credentials or raw exception/response contents.
    print("Unable to retrieve the next delivery date.")
else:
    print(delivery_date.isoformat() if delivery_date is not None else "No scheduled delivery")
```

Available functions include:

| Function | Result / behavior |
|---|---|
| `load_credentials(path)` | Validated credential dictionary; requires a private regular file. |
| `get_next_delivery_date(credentials)` | `datetime.date` or `None`; malformed/missing API fields raise `ClientError`. |
| `list_invoices(credentials)` | `(raw_response, newest_first_records)`; both contain private account data. |
| `get_pdf(credentials, invoice)` | Validated PDF `bytes`; no files written. |
| `sync(credentials, response, records, destination)` | Writes private snapshots/PDFs/reports, prints progress, and returns an exit status (`0` on success). `destination` is a `pathlib.Path`. |

Library calls are synchronous. Credential and local-file errors may raise
`OSError` or `ValueError`; expected API failures raise `ClientError`. Keep credential
files and returned account data outside shared logs and package artifacts.

## Export credentials from the website

1. Log in normally at **https://clientes.aquaservice.com/**.
2. Select the contract/account you want to export, open **Facturas**, and wait until the page has loaded. If you have multiple contracts, repeat this process separately for each one.
3. Open your browser's developer tools and select **Console**. In Safari, enable web developer features in Settings → Advanced if the Develop menu is not visible, then choose Develop → Show JavaScript Console.
4. Review and run the snippet below in the **customer website's top-level page context**, not an iframe or another site.
5. The browser downloads `credentials.json`. It does not send the credentials anywhere or print them to the console.

Only run console snippets you understand and trust. This one reads the site's existing localStorage, validates the selected contract, and creates a local JSON download. It does not log in, request an SMS, change the PIN, or modify browser storage.

```javascript
(() => {
  if (location.origin !== "https://clientes.aquaservice.com") {
    throw new Error("Run this on https://clientes.aquaservice.com after logging in.");
  }

  let selected;
  try {
    selected = JSON.parse(localStorage.getItem("contract") || "null");
  } catch {
    throw new Error("Stored contract is invalid. Reload the portal and select your contract again.");
  }
  if (!selected || typeof selected !== "object" || Array.isArray(selected)) {
    throw new Error("No selected contract. Log in, select a contract, and open Facturas first.");
  }

  const credentials = {
    application: "CLWEB",
    token: localStorage.getItem("token"),
    pin: localStorage.getItem("pin"),
    contract: localStorage.getItem("contractId"),
    delegation: localStorage.getItem("delegation"),
    accountFilter: typeof selected.account === "string"
      ? selected.account.toUpperCase()
      : null,
  };

  const missing = Object.entries(credentials)
    .filter(([, value]) => typeof value !== "string" || !value.trim()
      || value === "null" || value === "undefined")
    .map(([key]) => key);
  if (missing.length) {
    throw new Error(`Missing session fields: ${missing.join(", ")}. Log in and select your contract again.`);
  }

  // The portal stores contractNumber as "delegation||contractId".
  if (typeof selected.contractNumber !== "string"
      || selected.contractNumber !== `${credentials.delegation}||${credentials.contract}`) {
    throw new Error("Stored contract identifiers disagree. Reload and reselect your contract before exporting.");
  }

  // PIN is already encoded by the website: preserve it exactly.
  const blob = new Blob([JSON.stringify(credentials, null, 2) + "\n"], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "credentials.json";
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
  console.info("Credential download requested. Keep the file private and do not commit it.");
})();
```

### Install the downloaded file

For direct source execution, place the downloaded file next to `aquaservice.py`, then restrict permissions:

```sh
cd /path/to/aquaservice-api
chmod 600 credentials.json
python3 aquaservice.py invoices list --limit 1
```

For **initial setup only**, this copies from Downloads without replacing an existing credential file:

```sh
cd /path/to/aquaservice-api
(umask 077; cp -n "$HOME/Downloads/credentials.json" ./credentials.json)
chmod 600 credentials.json
```

Check the actual download filename: browsers may save repeated exports as `credentials (1).json` or similar. If refreshing an existing configuration, deliberately replace the correct file, then apply `chmod 600` again. Remove unneeded credential copies from Downloads and its cloud/backups as appropriate; the browser snippet cannot set filesystem permissions.

The snippet validates the storage structure, **not the server-side validity of the token**. The `invoices list --limit 1` command is the actual authentication check. If fields are missing, do not invent them: log in again, choose the contract, and let the portal finish loading.

### Credential fields

`credentials.json.example` contains the required shape, without real credentials:

| Field | Source / purpose |
|---|---|
| `application` | Constant `"CLWEB"` |
| `token` | `localStorage.token`: authentication token |
| `pin` | `localStorage.pin`: already Base64-encoded PIN; do not encode it again |
| `contract` | `localStorage.contractId`: selected contract identifier |
| `delegation` | `localStorage.delegation`: selected Aquaservice delegation |
| `accountFilter` | `JSON.parse(localStorage.contract).account`, uppercased exactly as the portal does |

All values must be non-empty JSON strings. Base64 is reversible encoding, not encryption: treat both the token and encoded PIN as secrets. Cookies, NIF, phone number, and browser tracing headers are not needed for the verified invoice-read flow.

If the website changes its storage layout, inspect the successful **Fetch/XHR** request to `https://appclient.aquaservice.com:2053/api/v4_9/invoice/` in DevTools Network and use its request payload as the source of truth. Do not copy the `/facturas` document request: it loads the application shell, not the invoices.

## Usage

After installation, use the same subcommands with `aquaservice-api` (or
`python -m aquaservice` in that environment):

```sh
aquaservice-api --credentials /private/path/credentials.json next-delivery
aquaservice-api --credentials /private/path/credentials.json invoices list --limit 1
```

Source checkout equivalents:

```sh
# List all invoices currently returned for the configured account.
python3 aquaservice.py invoices list

# Download missing PDFs; safely skip existing valid PDFs.
python3 aquaservice.py invoices sync

# Newest five invoices, or invoices on/after a date.
python3 aquaservice.py invoices list --limit 5
python3 aquaservice.py invoices sync --since 2026-01-01

# Custom destination. Existing output directories must have mode 700.
python3 aquaservice.py invoices sync --output /private/path/to/invoices

# Global --credentials option goes BEFORE invoices or next-delivery.
python3 aquaservice.py --credentials /private/path/credentials.json invoices sync

# Print the next delivery date (YYYY-MM-DD); no files are written.
python3 aquaservice.py next-delivery
python3 aquaservice.py --credentials /private/path/credentials.json next-delivery
```

Invoice operations are now grouped under `invoices`: use `invoices list` and `invoices sync` instead of the old top-level `list`/`sync` commands. `--credentials` remains a shared global option; `--since`, `--limit` and `--output` stay with their invoice operation.

`next-delivery` uses the same credential file and reads the API's `success.delivery_date`. It prints the reported date as `YYYY-MM-DD`, without calculating or inferring a schedule. An explicitly empty/null date prints a no-date message; a missing or malformed field is an error. The command does not place, reschedule or change a delivery, and it does not download invoices.

`--since` is inclusive. `--limit` selects the newest N records after date filtering.
For the installed `aquaservice-api` command and `python -m aquaservice`, the default
`credentials.json` and `invoices/` paths are relative to your working directory,
never the installation's `site-packages`. Direct `python3 /path/to/aquaservice.py`
execution preserves the original script-relative defaults. An explicitly supplied
relative path is always relative to the working directory. Use absolute
`--credentials` and `--output` paths for scheduled jobs.

## Output and repeat runs

A sync creates:

- PDF files named with invoice date, API number and a scoped identity hash.
- `snapshot-*.private.json`: the **complete** account listing response, including personal billing data. Filters limit PDF downloads, not the saved listing snapshot.
- `run-*.private.json`: counts, file identities, byte sizes and SHA-256 checksums.

Directories are created with mode 700 and files with mode 600. Existing PDFs are checked for a PDF header and EOF marker before being skipped. Writes never overwrite existing files. Each sync creates a new snapshot/report even if there are no new PDFs.

The client stops at the first failure and returns a nonzero exit status rather than reporting a partial run as complete. If an existing PDF is invalid, move it aside deliberately before retrying. Files of type `DEVOLUCION`, for which the portal does not offer a PDF, produce an explicit error.

The original end-to-end verification retrieved 55 distinct invoices, downloaded all 55 PDFs, and skipped all 55 on the second run. Separate PDF-parser checks matched invoice numbers, dates and customer IDs. Those were observations for one account/session, not hardcoded counts or a guarantee about available history.

## API and session limitations

The client uses JSON POSTs to the same read-only operations as the website:

- `https://appclient.aquaservice.com:2053/api/v4_9/delivery/next-delivery` — reported next delivery date.
- `https://appclient.aquaservice.com:2053/api/v4_9/invoice/` — listing.
- `https://appclient.aquaservice.com:2053/api/v4_9/invoice/<number>/` — PDF URL.
- A separate HTTPS GET downloads that PDF without forwarding API credentials. The observed, explicitly allowed file host is `fadhuavad.aquaservice.com`.

No browser needs to remain open after credential export. A browser-style User-Agent is set because the tested Python default received HTTP 403.

This is an undocumented private API. Routes, response shapes and permitted PDF hosts may change. No automatic token renewal is implemented, and token lifetime is unknown. If authentication stops working, log in normally, export fresh credentials, and validate them with `invoices list --limit 1`. A 403 may also be an upstream protection response; it does not prove the token is expired.

## Security and repository hygiene

- Never commit or share `credentials.json`, raw snapshots, invoices or private PDF URLs.
- `.gitignore` excludes the default credentials file, default output directories, private exports and Python caches; the example remains trackable.
- Alternate credential filenames/output directories are not necessarily covered by `.gitignore`. Keep them outside the repository or add appropriate rules.
- Ignoring a path does not remove a file that was already committed or staged. Check `git status` before committing.
- Browser localStorage exports can be stale. Validate against the API rather than treating successful export as a successful login.
- The script does not make payments, change account details, or perform PIN/SMS setup.
- Build with `uv build`. Wheel and source-distribution contents use explicit file
  allowlists; real credentials, downloaded invoices, tests, and research are not
  packaged. Only the placeholder credential example is included in the source
  distribution. Do not replace its placeholders with account data.
