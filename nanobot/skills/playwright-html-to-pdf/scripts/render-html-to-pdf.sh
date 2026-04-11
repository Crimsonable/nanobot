#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  render-html-to-pdf.sh <input.html|url> <output.pdf> [options]

Options:
  --format <name>             Paper format, default: A4
  --landscape                 Render PDF in landscape orientation
  --media <screen|print>      CSS media mode, default: screen
  --wait-until <state>        load, domcontentloaded, or networkidle; default: load
  --timeout <ms>              Navigation timeout in milliseconds, default: 30000
  --no-background             Disable CSS background rendering
  --no-css-page-size          Disable CSS @page size preference
  -h, --help                  Show this help
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

input="$1"
output="$2"
shift 2

format="A4"
landscape="false"
media="screen"
wait_until="load"
timeout="30000"
print_background="true"
prefer_css_page_size="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --format)
      format="${2:?--format requires a value}"
      shift 2
      ;;
    --landscape)
      landscape="true"
      shift
      ;;
    --media)
      media="${2:?--media requires a value}"
      shift 2
      ;;
    --wait-until)
      wait_until="${2:?--wait-until requires a value}"
      shift 2
      ;;
    --timeout)
      timeout="${2:?--timeout requires a value}"
      shift 2
      ;;
    --no-background)
      print_background="false"
      shift
      ;;
    --no-css-page-size)
      prefer_css_page_size="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$media" in
  screen|print) ;;
  *)
    echo "--media must be screen or print" >&2
    exit 2
    ;;
esac

case "$wait_until" in
  load|domcontentloaded|networkidle) ;;
  *)
    echo "--wait-until must be load, domcontentloaded, or networkidle" >&2
    exit 2
    ;;
esac

python3 - "$input" "$output" "$format" "$landscape" "$media" "$wait_until" "$timeout" "$print_background" "$prefer_css_page_size" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import pathname2url

try:
    from playwright.sync_api import sync_playwright
except Exception:
    print('Unable to load the Python "playwright" package.', file=sys.stderr)
    print("Install it with: python3 -m pip install playwright && python3 -m playwright install chromium", file=sys.stderr)
    sys.exit(1)

(
    input_value,
    output,
    paper_format,
    landscape_raw,
    media,
    wait_until,
    timeout_raw,
    print_background_raw,
    prefer_css_page_size_raw,
) = sys.argv[1:]

parsed = urlparse(input_value)
is_url = parsed.scheme in {"http", "https"}

if is_url:
    input_url = input_value
else:
    input_path = Path(input_value).resolve()
    if not input_path.exists():
        print(f"Input HTML does not exist: {input_path}", file=sys.stderr)
        sys.exit(2)
    input_url = "file://" + pathname2url(str(input_path))

try:
    timeout = float(timeout_raw)
except ValueError:
    print("--timeout must be a positive number", file=sys.stderr)
    sys.exit(2)

if timeout <= 0:
    print("--timeout must be a positive number", file=sys.stderr)
    sys.exit(2)

output_path = Path(output).resolve()
output_path.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.emulate_media(media=media)
        page.goto(input_url, wait_until=wait_until, timeout=timeout)
        page.pdf(
            path=str(output_path),
            format=paper_format,
            landscape=landscape_raw == "true",
            print_background=print_background_raw == "true",
            prefer_css_page_size=prefer_css_page_size_raw == "true",
        )
    finally:
        browser.close()
PY

if [[ ! -s "$output" ]]; then
  echo "PDF was not created or is empty: $output" >&2
  exit 1
fi

echo "PDF written: $output"
