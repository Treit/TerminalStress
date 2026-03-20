"""Upload HTML reports to rtreit.com.

The API key is read from (in order of priority):
  1. The RTREIT_REPORTS_API_KEY environment variable
  2. A .env file in the repo root (RTREIT_REPORTS_API_KEY=...)

Usage from Python:
    from monkey.upload_report import upload
    url = upload("crashdumps/crash-analysis-report.html")

Usage from the command line:
    python src/monkey/upload_report.py crashdumps/crash-analysis-report.html
    python src/monkey/upload_report.py report.html --name "march-20-crashes.html"
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

UPLOAD_URL = "https://rtreit.com/api/upload-report"
VIEW_BASE = "https://rtreit.com/api/reports"


def _load_api_key() -> str | None:
    """Resolve the API key from env var or .env file."""
    key = os.environ.get("RTREIT_REPORTS_API_KEY")
    if key:
        return key.strip()

    try:
        env_file = Path(__file__).resolve().parent.parent.parent / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "RTREIT_REPORTS_API_KEY":
                    return v.strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        pass

    return None


def upload(
    file_path: str | Path,
    *,
    filename: str | None = None,
) -> str | None:
    """Upload an HTML report to rtreit.com.

    Args:
        file_path: Path to the local HTML file.
        filename: Remote filename (default: basename of file_path).

    Returns:
        The public URL of the uploaded report, or None on failure.
    """
    api_key = _load_api_key()
    if not api_key:
        print(
            "error: RTREIT_REPORTS_API_KEY not set. "
            "Set it via environment variable or in .env at the repo root.",
            file=sys.stderr,
        )
        return None

    file_path = Path(file_path)
    if not file_path.is_file():
        print(f"error: file not found: {file_path}", file=sys.stderr)
        return None

    if filename is None:
        filename = file_path.name

    data = file_path.read_bytes()
    url = f"{UPLOAD_URL}?filename={urllib.request.quote(filename)}"

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "text/html",
            "x-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201, 202):
                report_url = f"{VIEW_BASE}/{filename}"
                print(f"Uploaded: {report_url}")
                return report_url
            else:
                print(
                    f"error: upload returned {resp.status}",
                    file=sys.stderr,
                )
    except urllib.error.HTTPError as exc:
        print(f"error: upload failed ({exc.code}): {exc.reason}", file=sys.stderr)
    except Exception as exc:
        print(f"error: upload failed: {exc}", file=sys.stderr)

    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload an HTML crash report to rtreit.com.",
    )
    parser.add_argument(
        "file",
        help="Path to the HTML report file to upload.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Remote filename (default: basename of the input file).",
    )
    args = parser.parse_args()

    result = upload(args.file, filename=args.name)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
