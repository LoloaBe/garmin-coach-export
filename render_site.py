#!/usr/bin/env python3
"""Render the triathlon dashboard as a standalone static site page.

Reads dashboard_template.html (Artifact-ready body content: title/style/body/script,
no doctype/html/head/body of its own) and garmin_data/dashboard_data.json, and writes
a complete, self-contained HTML document to site/index.html.

Run this after garmin_export.py + build_dashboard_data.py have refreshed
garmin_data/dashboard_data.json. No network access required, no credentials involved.

Usage:
    python3 render_site.py
    python3 render_site.py --template dashboard_template.html --data garmin_data/dashboard_data.json --out site/index.html
"""
import argparse
import pathlib
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    here = pathlib.Path(__file__).resolve().parent
    parser.add_argument("--template", default=here / "dashboard_template.html")
    parser.add_argument("--data", default=here / "garmin_data" / "dashboard_data.json")
    parser.add_argument("--out", default=here / "site" / "index.html")
    args = parser.parse_args()

    template_path = pathlib.Path(args.template)
    data_path = pathlib.Path(args.data)
    out_path = pathlib.Path(args.out)

    if not template_path.exists():
        sys.exit(f"Template not found: {template_path}")
    if not data_path.exists():
        sys.exit(f"Data file not found: {data_path} (run garmin_export.py + build_dashboard_data.py first)")

    body = template_path.read_text(encoding="utf-8")
    data = data_path.read_text(encoding="utf-8")

    # Same escaping the Claude-side injection uses, so a value containing "</script"
    # or an HTML comment opener can't break out of the embedded <script type="application/json"> block.
    data_escaped = data.replace("</script", "<\\/script").replace("<!--", "<\\!--")

    if "__DASHBOARD_DATA__" not in body:
        sys.exit("Template is missing the __DASHBOARD_DATA__ placeholder — is this the right template file?")
    body = body.replace("__DASHBOARD_DATA__", data_escaped)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Split at the dashboard's root wrapper div: everything before it is head material
    # (title/link/style), everything from there on is body content.
    marker = '<div class="wrap">'
    idx = body.find(marker)
    if idx == -1:
        sys.exit("Could not find the dashboard's root <div class=\"wrap\"> — template format may have changed.")
    head_material = body[:idx]
    body_material = body[idx:]

    page = (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        + head_material
        + "\n</head>\n<body>\n"
        + body_material
        + "\n</body>\n</html>\n"
    )

    out_path.write_text(page, encoding="utf-8")
    print(f"Wrote {out_path} ({len(page):,} bytes)")


if __name__ == "__main__":
    main()
