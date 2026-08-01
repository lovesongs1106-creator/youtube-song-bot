#!/usr/bin/env python3
"""
Convert Netscape cookies.txt to JSON format for Cobalt API.

Usage:
  python convert_cookies.py input.txt output.json
"""

import json
import sys
from pathlib import Path


def netscape_to_json(netscape_text: str) -> list[dict]:
    """Convert Netscape cookies.txt to Chrome-compatible JSON format."""
    cookies = []
    for line in netscape_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # Try space-separated fallback
            parts = line.split()
        if len(parts) >= 7:
            # Netscape format:
            # domain  flag  path  secure  expiration  name  value
            domain = parts[0]
            flag = parts[1].lower() == "true"
            path = parts[2]
            secure = parts[3].lower() == "true"
            try:
                expiration = int(float(parts[4]))
            except ValueError:
                expiration = 0
            name = parts[5]
            value = parts[6]
            cookies.append({
                "domain": domain,
                "hostOnly": not domain.startswith("."),
                "httpOnly": False,
                "name": name,
                "path": path,
                "sameSite": "no_restriction",
                "secure": secure,
                "session": expiration == 0,
                "storeId": None,
                "value": value,
            })
    return cookies


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python convert_cookies.py input.txt output.json")
        sys.exit(1)
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    text = input_path.read_text(encoding="utf-8")
    cookies = netscape_to_json(text)
    output_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    print(f"Converted {len(cookies)} cookies -> {output_path}")


if __name__ == "__main__":
    main()
