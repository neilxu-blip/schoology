#!/usr/bin/env python3
"""
Run Schoology sync for Hanna + Matthew and send one email summary when needed.

Normal scheduled run:
    python run_schoology_all.py

One-time email setup:
    python run_schoology_all.py --setup-email

Test email without syncing Schoology:
    python run_schoology_all.py --test-email

Email behavior:
- No email when both syncs succeed and there are zero new files.
- One combined email when new files are downloaded.
- One alert email when either sync exits with an error, even if there are no new files.

Secrets:
- Gmail App Password is stored in macOS Keychain.
- Non-secret sender/recipient settings live under .schoology_sync/email_config.json.
- .schoology_sync/ must remain in .gitignore.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import smtplib
import ssl
import subprocess
import sys
from collections import defaultdict
from email.message import EmailMessage
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SYNC_SCRIPT = PROJECT_DIR / "schoology_sync.py"
PRIVATE_DIR = PROJECT_DIR / ".schoology_sync"
CONFIG_PATH = PRIVATE_DIR / "email_config.json"
SUMMARY_DIR = PRIVATE_DIR / "run_summaries"
KEYCHAIN_SERVICE = "schoology-sync-gmail"
PROFILES = ("hanna", "matthew")
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465


def parse_args():
    p = argparse.ArgumentParser(description="Run Hanna + Matthew Schoology sync and email new-file summaries.")
    p.add_argument("--setup-email", action="store_true", help="Configure sender/recipient and store Gmail App Password in macOS Keychain.")
    p.add_argument("--test-email", action="store_true", help="Send a test email and exit without running Schoology.")
    p.add_argument("--no-email", action="store_true", help="Run both syncs but suppress email notifications.")
    return p.parse_args()


def read_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Email is not configured. Run: {sys.executable} {Path(__file__).name} --setup-email"
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def keychain_set(account: str, password: str) -> None:
    subprocess.run(
        [
            "security", "add-generic-password",
            "-U",
            "-s", KEYCHAIN_SERVICE,
            "-a", account,
            "-w", password,
        ],
        check=True,
    )


def keychain_get(account: str) -> str:
    result = subprocess.run(
        [
            "security", "find-generic-password",
            "-s", KEYCHAIN_SERVICE,
            "-a", account,
            "-w",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def setup_email() -> None:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    default_sender = existing.get("sender", "neilxu@gmail.com")
    default_recipient = existing.get("recipient", default_sender)

    sender = input(f"Gmail sender [{default_sender}]: ").strip() or default_sender
    recipient = input(f"Notification recipient [{default_recipient}]: ").strip() or default_recipient

    print("Enter a Google App Password for the sender account.")
    print("The password is stored in macOS Keychain and is not written to the project files.")
    app_password = getpass.getpass("Gmail App Password: ").replace(" ", "").strip()
    if not app_password:
        raise RuntimeError("No App Password entered.")

    keychain_set(sender, app_password)

    config = {
        "sender": sender,
        "recipient": recipient,
        "smtp_host": DEFAULT_SMTP_HOST,
        "smtp_port": DEFAULT_SMTP_PORT,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass

    print(f"Email configuration saved: {CONFIG_PATH}")
    print(f"Gmail App Password saved in macOS Keychain service: {KEYCHAIN_SERVICE}")


def send_email(subject: str, body: str) -> None:
    config = read_config()
    sender = config["sender"]
    recipient = config["recipient"]
    password = keychain_get(sender)

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        config.get("smtp_host", DEFAULT_SMTP_HOST),
        int(config.get("smtp_port", DEFAULT_SMTP_PORT)),
        context=context,
        timeout=30,
    ) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def run_profile(profile: str) -> dict:
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = SUMMARY_DIR / f"{profile}.json"
    summary_path.unlink(missing_ok=True)

    # Work around the macOS temp-directory permission problem seen on the Mac mini.
    tmpdir = Path.home() / "tmp-playwright"
    tmpdir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["TMPDIR"] = str(tmpdir)

    cmd = [
        sys.executable,
        str(SYNC_SCRIPT),
        profile,
        "--summary-json",
        str(summary_path),
    ]

    print()
    print("#" * 76)
    print(f"RUNNING SCHOOLOGY SYNC: {profile.upper()}")
    print("#" * 76)

    completed = subprocess.run(cmd, cwd=PROJECT_DIR, env=env)

    result = {
        "profile": profile,
        "exit_code": completed.returncode,
        "totals": {},
        "new_downloads": [],
    }

    if summary_path.exists():
        try:
            result.update(json.loads(summary_path.read_text(encoding="utf-8")))
            result["exit_code"] = completed.returncode
        except Exception as exc:
            result["summary_error"] = str(exc)

    return result


def human_size(size_bytes: int | float | None) -> str:
    n = float(size_bytes or 0)
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / 1024:.1f} KB"


def build_email(results: list[dict]) -> tuple[str | None, str | None]:
    total_new = sum(len(r.get("new_downloads", [])) for r in results)
    failed = [r for r in results if int(r.get("exit_code", 1)) != 0]

    # No news and no problems: deliberately stay silent.
    if total_new == 0 and not failed:
        return None, None

    counts = {r["profile"].title(): len(r.get("new_downloads", [])) for r in results}

    if failed:
        subject = f"[Schoology] Sync problem — {total_new} new file{'s' if total_new != 1 else ''}"
    else:
        subject = (
            f"[Schoology] {total_new} new file{'s' if total_new != 1 else ''} — "
            f"Hanna {counts.get('Hanna', 0)}, Matthew {counts.get('Matthew', 0)}"
        )

    lines = ["Schoology sync summary", ""]

    for result in results:
        profile = result["profile"].title()
        items = result.get("new_downloads", [])
        exit_code = int(result.get("exit_code", 1))

        lines.append(f"{profile} — {len(items)} new file{'s' if len(items) != 1 else ''}")
        if exit_code != 0:
            lines.append(f"WARNING: sync exited with code {exit_code}")

        if items:
            by_course = defaultdict(list)
            for item in items:
                by_course[item.get("course", "Unknown course")].append(item)

            for course, course_items in by_course.items():
                lines.append("")
                lines.append(course)
                for item in course_items:
                    display = item.get("display_name") or item.get("relative_name") or "Unknown file"
                    lines.append(f"• {display} ({human_size(item.get('size'))})")
        else:
            lines.append("No new files.")

        totals = result.get("totals") or {}
        if totals:
            lines.append("")
            lines.append(
                "Run totals: "
                f"downloaded {totals.get('downloaded', 0)}, "
                f"already had {totals.get('skipped', 0)}, "
                f"errors {totals.get('errors', 0)}"
            )

        lines.append("")
        lines.append("-" * 50)
        lines.append("")

    if failed:
        lines.append("At least one Schoology sync needs attention. Check the scheduled log on the Mac mini.")

    return subject, "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()

    if args.setup_email:
        setup_email()
        return 0

    if args.test_email:
        send_email(
            "[Schoology] Test notification",
            "Schoology email notifications from the Mac mini are configured correctly.\n",
        )
        print("Test email sent.")
        return 0

    results = [run_profile(profile) for profile in PROFILES]

    subject, body = build_email(results)
    if subject is None:
        print("\nNo new files and no sync errors. No email sent.")
        return 0

    if args.no_email:
        print("\nEmail suppressed by --no-email.")
    else:
        try:
            send_email(subject, body)
            print(f"\nEmail sent: {subject}")
        except Exception as exc:
            print(f"\nEMAIL ERROR: {exc}", file=sys.stderr)
            return 3

    return 0 if all(int(r.get("exit_code", 1)) == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
