#!/usr/bin/env python3
"""Pull challenges from picoCTF picoGym (play.picoctf.org).

picoCTF uses its own platform (not CTFd), so this is a separate script.

USAGE:
  1. Log in to https://play.picoctf.org in your browser
  2. Open DevTools → Application → Cookies → play.picoctf.org
     Copy the value of the 'sessionid' cookie
  3. Run:
       python pull_picoctf.py --session YOUR_SESSION_ID [--output ./pico-challenges]

  Or with username/password:
       python pull_picoctf.py --username you@email.com --password yourpass

Directory layout (same format as pull_challenges.py):
  <output>/<slug>/
      metadata.yml
      distfiles/       (downloaded challenge files)
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

import aiohttp
import yaml

BASE_URL = "https://play.picoctf.org"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ── Auth ──────────────────────────────────────────────────────────────────────

async def login(session: aiohttp.ClientSession, username: str, password: str) -> bool:
    """Log in and populate session cookies."""
    # Get CSRF token first
    async with session.get(f"{BASE_URL}/login", headers={"User-Agent": USER_AGENT}) as r:
        text = await r.text()
        csrf = _extract_csrf(text)
        if not csrf:
            print("ERROR: Could not find CSRF token on login page.", file=sys.stderr)
            print("  Tip: picoCTF may be behind Cloudflare. Try --session instead.", file=sys.stderr)
            return False

    async with session.post(
        f"{BASE_URL}/login",
        data={"username": username, "password": password, "csrfmiddlewaretoken": csrf},
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE_URL}/login",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        allow_redirects=False,
    ) as r:
        if r.status in (301, 302):
            print("Login successful.")
            return True
        print("ERROR: Login failed — bad credentials or Cloudflare block.", file=sys.stderr)
        print("  Try using --session with your browser session cookie instead.", file=sys.stderr)
        return False


def _extract_csrf(html: str) -> str | None:
    m = re.search(r'csrfmiddlewaretoken["\s]+value[="\s]+([a-zA-Z0-9]+)', html)
    if m:
        return m.group(1)
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    return m.group(1) if m else None


# ── Fetch challenges ──────────────────────────────────────────────────────────

async def fetch_challenges(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all available practice challenges."""
    challenges = []
    page = 1
    while True:
        url = f"{BASE_URL}/api/challenges/?format=json&page={page}&limit=50"
        async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
            if r.status == 401:
                print("ERROR: Not authenticated. Check your session cookie.", file=sys.stderr)
                return []
            if r.status != 200:
                print(f"ERROR: /api/challenges/ returned {r.status}", file=sys.stderr)
                return challenges
            data = await r.json()

        # Handle paginated response
        if isinstance(data, dict):
            results = data.get("results") or data.get("data") or []
            has_next = bool(data.get("next"))
        elif isinstance(data, list):
            results = data
            has_next = False
        else:
            break

        challenges.extend(results)
        if not has_next or not results:
            break
        page += 1

    return challenges


async def fetch_challenge_detail(session: aiohttp.ClientSession, challenge_id: int) -> dict:
    """Fetch full detail for a single challenge."""
    url = f"{BASE_URL}/api/challenges/{challenge_id}/?format=json"
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
        if r.status == 200:
            return await r.json()
    return {}


# ── Save challenges ───────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "challenge"


async def download_file(session: aiohttp.ClientSession, url: str, dest: Path) -> bool:
    if not url.startswith("http"):
        url = BASE_URL + url
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
        if r.status == 200:
            dest.write_bytes(await r.read())
            return True
    return False


async def save_challenge(
    session: aiohttp.ClientSession,
    challenge: dict,
    output_dir: Path,
    category_filter: str = "",
) -> bool:
    name = challenge.get("name") or challenge.get("title") or f"id-{challenge.get('id')}"
    category = challenge.get("category") or challenge.get("category_name") or "misc"

    if category_filter and category_filter.lower() not in category.lower():
        return False

    slug = slugify(name)
    chdir = output_dir / slug
    chdir.mkdir(parents=True, exist_ok=True)

    # Download attached files
    files = challenge.get("files") or challenge.get("file_list") or []
    distfiles_dir = chdir / "distfiles"
    for f in files:
        url = f if isinstance(f, str) else f.get("url") or f.get("download_url") or ""
        if not url:
            continue
        distfiles_dir.mkdir(exist_ok=True)
        fname = url.split("/")[-1].split("?")[0] or "file"
        ok = await download_file(session, url, distfiles_dir / fname)
        if ok:
            print(f"    Downloaded: {fname}")

    # Build metadata.yml
    hints = []
    for h in (challenge.get("hints") or []):
        content = h if isinstance(h, str) else h.get("hint") or h.get("body") or ""
        if content:
            hints.append({"cost": 0, "content": content})

    meta = {
        "version": "beta1",
        "name": name,
        "category": category,
        "description": _strip_html(challenge.get("description") or challenge.get("problem_statement") or ""),
        "value": challenge.get("score") or challenge.get("value") or challenge.get("points") or 0,
        "solves": challenge.get("solves") or challenge.get("solution_count") or 0,
    }
    if hints:
        meta["hints"] = hints

    # Connection info (nc / http)
    conn = challenge.get("connection_info") or ""
    if not conn:
        # Some challenges expose host:port
        host = challenge.get("host") or ""
        port = challenge.get("port") or ""
        if host and port:
            conn = f"nc {host} {port}"
        elif challenge.get("instance_urls"):
            conn = challenge["instance_urls"][0]
    if conn:
        meta["connection_info"] = conn

    (chdir / "metadata.yml").write_text(
        yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return True


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(
    output: str,
    session_id: str = "",
    username: str = "",
    password: str = "",
    category: str = "",
    limit: int = 0,
):
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    cookies = {}
    if session_id:
        cookies["sessionid"] = session_id

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector, cookies=cookies) as session:
        # Auth
        if not session_id:
            if not username:
                print("ERROR: Provide --session or --username/--password", file=sys.stderr)
                sys.exit(1)
            ok = await login(session, username, password)
            if not ok:
                sys.exit(1)

        # Fetch challenge list
        print(f"Fetching challenges from {BASE_URL}...")
        challenges = await fetch_challenges(session)

        if not challenges:
            print("\nNo challenges returned. Possible causes:")
            print("  1. Session cookie expired — log in again and copy a fresh sessionid")
            print("  2. Cloudflare blocked the request — try from your own machine")
            print("  3. picoCTF API changed — check play.picoctf.org/api/challenges/")
            sys.exit(1)

        print(f"Found {len(challenges)} challenges\n")

        if limit:
            challenges = challenges[:limit]

        count = 0
        for ch in challenges:
            name = ch.get("name") or ch.get("title") or str(ch.get("id"))
            cat = ch.get("category") or ch.get("category_name") or "?"
            pts = ch.get("score") or ch.get("value") or ch.get("points") or 0

            if category and category.lower() not in cat.lower():
                continue

            print(f"  [{cat}] {name} ({pts} pts)")

            # Fetch detail if needed
            detail = ch
            if not ch.get("description") and ch.get("id"):
                detail = await fetch_challenge_detail(session, ch["id"]) or ch

            saved = await save_challenge(session, detail, output_dir, category)
            if saved:
                count += 1

        print(f"\nDone. Pulled {count} challenge(s) → {output_dir.resolve()}")
        print("\nNow run:")
        print(f"  uv run ctf-solve \\")
        print(f"    --ctfd-url {BASE_URL} \\")
        print(f"    --ctfd-token DUMMY \\")
        print(f"    --challenges-dir {output_dir} \\")
        print(f"    --no-submit \\")
        print(f"    --models ollama/qwen2.5-coder:7b \\")
        print(f"    --coordinator claude")
        print()
        print("Use --no-submit since picoCTF submission goes through their own UI.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pull challenges from picoCTF picoGym")
    parser.add_argument("--output", default="./pico-challenges", help="Output dir (default: ./pico-challenges)")

    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--session", help="sessionid cookie value from your browser")
    auth.add_argument("--username", help="picoCTF username or email")

    parser.add_argument("--password", help="picoCTF password (with --username)")
    parser.add_argument("--category", default="", help="Filter by category (web, crypto, pwn, rev, forensics, misc)")
    parser.add_argument("--limit", type=int, default=0, help="Max challenges to pull (0 = all)")

    args = parser.parse_args()

    if args.username and not args.password:
        parser.error("--password required with --username")

    asyncio.run(main(
        output=args.output,
        session_id=args.session or "",
        username=args.username or "",
        password=args.password or "",
        category=args.category,
        limit=args.limit,
    ))
