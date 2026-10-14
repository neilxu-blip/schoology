# Schoology Sync

Schoology Sync downloads file attachments from Schoology while preserving the
student, course, and Schoology folder hierarchy. It keeps persistent browser
sessions and download state so later runs normally download only new files.

The project can run one student at a time or run the configured Hanna and
Matthew profiles together and send email summaries.

> [!IMPORTANT]
> This project stores authenticated browser data and replayable Schoology QR
> login data locally. Treat `.schoology_sync/`, browser profiles, debug output,
> QR images, and copied state files as private data. Never commit or share them.

## The two Python programs

### `schoology_sync.py`: one-profile sync engine

Use this program when you want to work with one Schoology profile, inspect
courses, debug login or scraping, choose an output directory, or limit a run to
specific courses.

It performs the full Schoology workflow:

1. Opens a persistent Playwright Chromium profile.
2. Reuses an existing login or attempts the locally learned QR-auth request.
3. Opens the QR login page for a manual scan when automatic login fails.
4. Discovers courses and expands their Schoology material folders.
5. Inspects material pages and downloads their attachments.
6. Records paths and SHA-256 hashes in a per-profile state file.
7. Optionally writes a JSON summary for another program to consume.

Basic examples:

```bash
.venv/bin/python schoology_sync.py hanna
.venv/bin/python schoology_sync.py matthew
.venv/bin/python schoology_sync.py hanna --output "/path/to/Schoology Archive"
```

### `run_schoology_all.py`: two-profile runner and email notifier

Use this program for the normal scheduled job. It runs `schoology_sync.py`
sequentially for the hardcoded profiles `hanna` and `matthew`, reads each
profile's JSON run summary, and sends tailored notifications.

- The parent receives one combined summary when files are new or a sync fails.
- Hanna and Matthew receive only their own new-file summaries.
- A child receives no message when that child has no new files.
- Children do not receive sync error or debug details.
- No message is sent when both syncs succeed with no new files.

Run both profiles and send configured email notifications:

```bash
.venv/bin/python run_schoology_all.py
```

Run both profiles without email:

```bash
.venv/bin/python run_schoology_all.py --no-email
```

The wrapper does not currently accept `--output`, `--course-id`, or the other
single-profile options. It uses the defaults defined by `schoology_sync.py`.

## Requirements

- Python 3.10 or newer; Python 3.12 is recommended.
- macOS for the email wrapper's Keychain integration.
- A graphical desktop session: Chromium is currently launched with
  `headless=False`.
- A Schoology account/profile for each student.
- A physical or otherwise camera-readable Schoology QR login code for initial
  login and later reauthentication.
- For email notifications, a Gmail account with 2-Step Verification and an App
  Password.
- Enough disk or cloud-drive space for the downloaded course files.

Python packages:

- `playwright`
- `beautifulsoup4`
- `requests`

The repository does not currently include a dependency lockfile. The local
development environment used Python 3.12 with Playwright 1.62.0,
Beautiful Soup 4.15.0, and Requests 2.34.2.

## Environment setup

### 1. Create and activate a virtual environment

From the project directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

If `python3.12` is not available, use another Python executable that is version
3.10 or newer.

### 2. Install Python dependencies

```bash
python -m pip install playwright beautifulsoup4 requests
```

### 3. Install the Playwright Chromium browser

```bash
python -m playwright install chromium
```

### 4. Verify the command-line programs

These checks do not log in or download Schoology files:

```bash
python schoology_sync.py --help
python run_schoology_all.py --help
```

### 5. Choose the download location

The current default archive is hardcoded as:

```text
/Users/lingyunxu/Library/CloudStorage/GoogleDrive-neilxu@gmail.com/My Drive/Schoology Basis 2026-27
```

For a one-profile run, override it with `--output`:

```bash
python schoology_sync.py hanna --output "/path/to/Schoology Basis 2026-27"
```

For scheduled use through `run_schoology_all.py`, the destination must either
exist at the current default path or the default in `schoology_sync.py` must be
updated. The wrapper does not forward an output option.

### 6. Perform the first Schoology login

Start with course discovery for one profile:

```bash
python schoology_sync.py hanna --list-courses
```

A visible Chromium window opens. If there is no valid saved login, the program
opens Schoology's QR login page and waits for a scan. After successful login it
saves the browser session and a small set of candidate authentication requests
under `.schoology_sync/`.

Repeat for the other profile:

```bash
python schoology_sync.py matthew --list-courses
```

Each profile has independent cookies and authentication data. Do not point two
simultaneous processes at the same profile directory.

## Gmail notification setup

Email sending is implemented for Gmail SMTP and stores the App Password in the
current macOS user's Keychain. The password is not written to the repository.

### 1. Create a Gmail App Password

In the Google account that will send notifications:

1. Open Google Account security settings.
2. Enable 2-Step Verification if it is not already enabled.
3. Open **App passwords**.
4. Create an App Password for this Schoology sync installation.
5. Copy the generated password for the next step.

The App Password option may be unavailable for some managed Google Workspace
accounts or when an administrator/security policy disables it. Use an account
whose policy permits SMTP App Passwords.

Do not enter the normal Google account password. Use the generated App Password.

### 2. Save sender and recipient settings

```bash
python run_schoology_all.py --setup-email
```

The setup asks for:

- Gmail sender address
- parent recipient address
- optional Hanna recipient address
- optional Matthew recipient address
- Gmail App Password

Recipient fields may contain comma-separated addresses. The non-secret settings
are stored in:

```text
.schoology_sync/email_config.json
```

The password is stored in macOS Keychain under service:

```text
schoology-sync-gmail
```

Run setup as the same macOS user that will run the scheduled job, because
Keychain items are user-specific.

### 3. Test email without syncing Schoology

```bash
python run_schoology_all.py --test-email
```

This sends a parent test message and, when configured, a separate test message
to each child. It does not start the Schoology sync.

If the test fails, check:

- the sender address exactly matches the account that created the App Password;
- the scheduled job runs as the same macOS user used during setup;
- the App Password was entered instead of the normal account password;
- Gmail SMTP is reachable on port 465;
- `email_config.json` contains the intended addresses and SMTP settings.

To replace a sender, recipient, or App Password, run `--setup-email` again. If a
previously configured child recipient must be disabled, edit that child's value
in `email_config.json` to an empty string; blank input during setup currently
keeps an existing value.

## Normal operation

Recommended first complete run without mail:

```bash
python run_schoology_all.py --no-email
```

After verifying the downloads and state paths printed by both profiles, run:

```bash
python run_schoology_all.py
```

The scripts intentionally pace Schoology requests. A full initial run can take
significantly longer than later incremental runs.

### Output and state layout

If the archive argument is `/data/Schoology Basis 2026-27`, the layout is:

```text
/data/
├── Schoology Basis 2026-27/
│   ├── Hanna/
│   │   └── <Course>/<Schoology folders>/<files>
│   └── Matthew/
│       └── <Course>/<Schoology folders>/<files>
└── Schoology Sync State/
    ├── hanna.json
    └── matthew.json
```

The state directory is deliberately a sibling of the archive, not a child of
it. This matters when backing up or migrating the installation. Losing the
state does not delete downloaded files, but it can cause files to be downloaded
again under duplicate names.

Machine-local runtime data is stored under the project directory:

```text
.schoology_sync/
├── profiles/       # persistent Chromium profiles and cookies
├── qr_auth/        # replayable QR authentication requests
├── debug/          # diagnostic HTML and screenshots
├── run_summaries/  # latest wrapper JSON summaries
└── email_config.json
```

## Debugging and diagnostic options

### List discovered courses

```bash
python schoology_sync.py hanna --list-courses
```

This verifies browser launch, authentication, and course discovery without
syncing course attachments. It can still create local profile/state directories.

### Inspect what would be downloaded

```bash
python schoology_sync.py hanna --dry-run --output "/path/to/archive"
```

This discovers and inspects materials but does not write attachment contents.
It is not completely side-effect-free: it can create directories, update browser
session data, save diagnostics, and rewrite the state JSON.

### Force a fresh QR login-learning scan

```bash
python schoology_sync.py hanna --manual-qr
```

Use this when the saved login or learned QR request no longer authenticates.

### Slow down visible browser actions

```bash
python schoology_sync.py hanna --slow-mo 500
```

`--slow-mo` is in milliseconds and is useful when watching Playwright behavior.

### Limit a run to selected courses

By numeric course ID:

```bash
python schoology_sync.py hanna --course-id 123456789
```

Repeat `--course-id` to select multiple courses. A direct Materials URL can also
be supplied:

```bash
python schoology_sync.py hanna \
  --course-url "https://app.schoology.com/course/123456789/materials"
```

### Recheck previously downloaded attachments

```bash
python schoology_sync.py hanna --refresh
```

`--refresh` downloads known attachments to temporary files, compares SHA-256
hashes, and replaces the local copy only when content changed. It is much more
network-intensive than a normal incremental run.

### Adjust request pacing

```bash
python schoology_sync.py hanna \
  --click-delay 3 \
  --request-delay 4 \
  --download-delay 2 \
  --jitter 1
```

Increase these values if Schoology returns HTTP 429 or the folder interface does
not finish loading. Avoid setting them to zero for large runs.

### Write a standalone JSON summary

```bash
python schoology_sync.py hanna \
  --summary-json "/tmp/hanna-schoology-summary.json"
```

The summary contains totals, newly downloaded files, completion time, profile,
and output path. The two-profile wrapper uses this interface internally.

### Test email only

```bash
python run_schoology_all.py --test-email
```

### Run sync while suppressing all email

```bash
python run_schoology_all.py --no-email
```

### Debug snapshots

When material extraction fails or finds no useful links, the sync may save the
current page HTML and a full-page screenshot under:

```text
.schoology_sync/debug/<profile>/
```

These files may include student names, course content, URLs, and page data. Keep
them private and remove old snapshots when they are no longer needed.

### Exit codes

`schoology_sync.py` commonly returns:

- `0`: sync completed with no recorded download errors
- `1`: fatal error or one or more course/download errors
- `2`: no courses were discovered
- `130`: interrupted with Ctrl+C

`run_schoology_all.py` commonly returns:

- `0`: both syncs succeeded and requested email operations succeeded
- `1`: at least one profile sync failed
- `3`: an email operation failed

## Complete server migration guide

The safest migration is a controlled cutover: stop the old job, copy the data,
rebuild dependencies on the new machine, establish fresh credentials, verify a
no-email run, and only then enable scheduling.

### What must be migrated

Inventory all of these before starting:

1. **Repository:** both Python programs and this README.
2. **Downloaded archive:** the complete `Schoology Basis 2026-27` directory, or
   whichever directory is passed with `--output`.
3. **Shared state:** the sibling `Schoology Sync State` directory.
4. **Local runtime data, if reusing it:** `.schoology_sync/`, especially
   `profiles/` and `qr_auth/`.
5. **Email configuration:** `.schoology_sync/email_config.json`.
6. **Email secret:** the Keychain App Password cannot be restored merely by
   copying the repository; configure it for the destination user.
7. **Scheduler and logs:** launchd, cron, or another scheduler configuration is
   outside this repository and must be migrated separately.

Do not migrate `.venv/`. Rebuild it for the new operating system and Python
installation.

### Phase 1: prepare the old server

1. Disable the scheduled job so it cannot start during migration.
2. Confirm that no `schoology_sync.py`, `run_schoology_all.py`, Chromium, or
   Playwright process is using the profiles or state files.
3. Record the exact project path, archive path, state path, macOS username, and
   scheduler command.
4. Run one final normal sync if desired, then stop all further old-server runs.
5. Back up the archive and `Schoology Sync State` together from the same point
   in time.
6. Back up `.schoology_sync/` only through an encrypted/private transfer.

Do not place `.schoology_sync/` in Git. It contains authentication material.

### Phase 2: transfer code and data

Transfer the repository to the destination using Git or a file copy. Transfer
the archive and state separately while preserving filenames and directory
structure.

If Google Drive already contains the archive and state:

1. Configure Google Drive for the destination user.
2. Wait until both `Schoology Basis 2026-27` and `Schoology Sync State` are fully
   available locally.
3. Do not run the sync while either directory is still partially downloading or
   while the old server can still modify it.

If using a direct copy, exclude the machine-local virtual environment and
private runtime data from the code copy. A typical macOS/Linux pattern is:

```bash
rsync -a \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.schoology_sync/' \
  --exclude '.DS_Store' \
  "/old/project/schoology/" "/new/project/schoology/"
rsync -a "/old/data/Schoology Basis 2026-27/" \
  "/new/data/Schoology Basis 2026-27/"
rsync -a "/old/data/Schoology Sync State/" \
  "/new/data/Schoology Sync State/"
```

Adapt the paths to the actual machines. Use an encrypted channel because the
runtime directory and downloaded course files are private.

### Phase 3: decide whether to copy browser authentication

There are two approaches:

**Recommended: establish fresh Schoology sessions.**

- Do not copy `.schoology_sync/profiles/` or `qr_auth/`.
- Run `--list-courses` or `--manual-qr` for each profile on the destination.
- This avoids transferring reusable login material and works across different
  operating-system users or machines.

**Optional: attempt session continuity on a trusted replacement Mac.**

- Copy `.schoology_sync/profiles/` and `.schoology_sync/qr_auth/` securely.
- Preserve ownership and restrict the directory to the destination account.
- Chromium cookies may be protected by machine/user-specific OS facilities, so
  copied profiles are not guaranteed to work.
- Be ready to run `--manual-qr` if authentication fails.

Debug snapshots and old run summaries are not required for operation. Copy them
only when they are needed for troubleshooting or audit history.

### Phase 4: rebuild the environment

On the destination:

```bash
cd "/new/project/schoology"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install playwright beautifulsoup4 requests
python -m playwright install chromium
python schoology_sync.py --help
python run_schoology_all.py --help
```

For reproducibility, use the same Python and package versions as the old
installation until the migration is verified.

### Phase 5: restore permissions and email configuration

On macOS, restrict the private runtime directory:

```bash
mkdir -p .schoology_sync
chmod 700 .schoology_sync
chmod 600 .schoology_sync/email_config.json 2>/dev/null || true
chmod 600 .schoology_sync/qr_auth/*.json 2>/dev/null || true
```

Then configure the Gmail password for the destination macOS user:

```bash
python run_schoology_all.py --setup-email
python run_schoology_all.py --test-email
```

Creating a new App Password for the new server is preferable. After cutover,
revoke the old server's App Password in the Google account if it is no longer
needed.

### Phase 6: validate paths and authentication

Verify each profile independently:

```bash
python schoology_sync.py hanna --list-courses \
  --output "/new/data/Schoology Basis 2026-27"
python schoology_sync.py matthew --list-courses \
  --output "/new/data/Schoology Basis 2026-27"
```

Check the `Output`, `State`, and `Browser` paths printed near the start. The
state path must resolve to the migrated `Schoology Sync State` directory.

Next, use a dry run for each profile:

```bash
python schoology_sync.py hanna --dry-run \
  --output "/new/data/Schoology Basis 2026-27"
python schoology_sync.py matthew --dry-run \
  --output "/new/data/Schoology Basis 2026-27"
```

If most previously downloaded files appear as `WOULD DOWNLOAD`, stop and verify
that the correct state directory and archive were migrated. Proceeding with
missing or mismatched state can create duplicates.

### Phase 7: validate the complete wrapper

The wrapper cannot receive the new output path on the command line. Before this
step, either make the new path match the configured default or update the
default path in `schoology_sync.py`.

Run:

```bash
python run_schoology_all.py --no-email
```

Confirm:

- both profiles use the expected archive and state locations;
- previously downloaded files are reported as already present;
- no large unexpected duplicate download occurs;
- both profile exit codes are successful.

Then verify email again:

```bash
python run_schoology_all.py --test-email
```

Finally run the normal command once manually:

```bash
python run_schoology_all.py
```

### Phase 8: migrate scheduling and cut over

Recreate the old scheduler using absolute paths. The scheduled command should
set the project as its working directory and use the virtual environment's
Python executable, for example:

```bash
cd "/new/project/schoology" && \
  "/new/project/schoology/.venv/bin/python" run_schoology_all.py
```

Also migrate any log rotation, notification, environment, or working-directory
settings from the old scheduler. Ensure the scheduled process:

- runs as the macOS user whose Keychain contains the Gmail App Password;
- has access to the graphical login session needed by headed Chromium;
- has read/write access to the archive and state directories;
- has access to the Google Drive mount if cloud storage is used;
- does not overlap a previous run.

Enable the destination schedule only after the manual full run succeeds. Keep
the old schedule disabled. Monitor the first scheduled run and inspect its exit
code, log, output, state timestamps, and email notification.

### Moving to Linux or a headless server

The current project is not ready for a drop-in headless Linux migration:

- email password retrieval calls the macOS `security` program;
- Chromium is explicitly launched with `headless=False`;
- initial QR login expects a visible browser/camera workflow;
- the default archive path is macOS- and user-specific.

A Linux/headless migration requires code changes for a different secret store,
headless-compatible authentication, configurable paths, and browser launch
behavior. Validate those changes separately before retiring the Mac installation.

### Rollback

If destination validation fails:

1. Disable the destination schedule.
2. Do not run both servers against the same shared state.
3. Restore the pre-migration archive/state backup if a faulty run created
   duplicates or changed state unexpectedly.
4. Re-enable the old server only after confirming it has the latest consistent
   archive and state.

## Security notes

- `.schoology_sync/qr_auth/*.json` can contain replayable Schoology login data.
- `.schoology_sync/profiles/` contains authenticated browser state.
- debug HTML/screenshots can contain student and course information.
- download-state JSON contains local paths and Schoology URLs.
- Gmail App Passwords belong in Keychain, never source files or shell scripts.
- The tracked `qr/` images are not used by the current code. If they encode live
  login credentials, remove them from the repository and Git history and rotate
  the corresponding credentials.
- The current `.gitignore` entry is `.qr/`, which does not protect `qr/`.

## Current testing status

The repository does not yet include an automated test suite. Before relying on
a migration, perform the manual staged checks above: help output, course listing,
dry runs, a no-email wrapper run, an email-only test, and one observed scheduled
run.
