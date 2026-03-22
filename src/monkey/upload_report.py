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
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

UPLOAD_URL = "https://rtreit.com/api/upload-report"
VIEW_BASE = "https://rtreit.com/reports"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)


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
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201, 202):
                report_url = f"{VIEW_BASE}/{filename}"
                body = resp.read()
                if body:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                        if isinstance(payload, dict) and isinstance(payload.get("url"), str):
                            report_url = payload["url"]
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                print(f"Uploaded: {report_url}")
                return report_url
            else:
                print(
                    f"error: upload returned {resp.status}",
                    file=sys.stderr,
                )
    except urllib.error.HTTPError as exc:
        details = ""
        try:
            details = exc.read(200).decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        if details:
            print(
                f"error: upload failed ({exc.code}): {exc.reason} — {details}",
                file=sys.stderr,
            )
        else:
            print(f"error: upload failed ({exc.code}): {exc.reason}", file=sys.stderr)
    except Exception as exc:
        print(f"error: upload failed: {exc}", file=sys.stderr)

    return None


DELETE_URL = "https://rtreit.com/api/delete-report"


def delete(filename: str) -> bool:
    """Delete a report from rtreit.com.

    Returns True on success, False on failure.
    """
    api_key = _load_api_key()
    if not api_key:
        print(
            "error: RTREIT_REPORTS_API_KEY not set.",
            file=sys.stderr,
        )
        return False

    url = f"{DELETE_URL}?filename={urllib.request.quote(filename)}"

    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "User-Agent": USER_AGENT,
        },
        method="DELETE",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 204):
                print(f"Deleted: {filename}")
                return True
    except urllib.error.HTTPError as exc:
        print(f"error: delete failed ({exc.code}): {exc.reason}", file=sys.stderr)
    except Exception as exc:
        print(f"error: delete failed: {exc}", file=sys.stderr)

    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload or delete HTML crash reports on rtreit.com.",
    )
    sub = parser.add_subparsers(dest="command")

    up = sub.add_parser("upload", help="Upload a report")
    up.add_argument("file", help="Path to the HTML report file to upload.")
    up.add_argument("--name", default=None, help="Remote filename.")

    dl = sub.add_parser("delete", help="Delete a report")
    dl.add_argument("filename", help="Remote filename to delete.")

    ls = sub.add_parser("list", help="List all reports")

    # Backwards compat: if first arg is a file path, treat as upload
    args = parser.parse_args()

    if args.command == "delete":
        ok = delete(args.filename)
        sys.exit(0 if ok else 1)
    elif args.command == "list":
        try:
            req = urllib.request.Request(
                "https://rtreit.com/api/list-reports",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for r in data.get("reports", []):
                    size_kb = r.get("size", 0) // 1024
                    print(f"  {r['name']}  ({size_kb} KB)")
                print(f"\n{data.get('count', 0)} report(s)")
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "upload":
        result = upload(args.file, filename=args.name)
        sys.exit(0 if result else 1)
    else:
        # No subcommand — check if positional arg looks like a file (backwards compat)
        import sys as _sys
        if len(_sys.argv) > 1 and not _sys.argv[1].startswith("-"):
            result = upload(_sys.argv[1], filename=None)
            sys.exit(0 if result else 1)
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
