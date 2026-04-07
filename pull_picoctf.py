#!/usr/bin/env python3
"""Pull challenges from picoCTF picoGym (play.picoctf.org).

Run this on YOUR OWN MACHINE — picoCTF is behind Cloudflare which
blocks requests from servers. Your home IP works fine.

USAGE — Username/password:
    python pull_picoctf.py \
        --username KismatKunwar \
        --password yourpass \
        --category web \
        --output ./pico-challenges

USAGE — Specific challenges by name:
    python pull_picoctf.py \
        --username KismatKunwar \
        --password yourpass \
        --pick "format string 0" "buffer overflow 1" "web gauntlet"

USAGE — Session cookie (if login fails):
    # 1. Log in at https://play.picoctf.org in browser
    # 2. DevTools → Application → Cookies → copy 'sessionid' value
    python pull_picoctf.py --session YOUR_SESSION_ID

Directory layout (same as pull_challenges.py — works with ctf-solve):
    <output>/<challenge-slug>/
        metadata.yml
        distfiles/
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

import aiohttp
import yaml
try:
    import cloudscraper  # pip install cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

BASE_URL = "https://play.picoctf.org"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ── Auth ──────────────────────────────────────────────────────────────────────

async def login(session: aiohttp.ClientSession, username: str, password: str) -> bool:
    """Log in with username/password. Works from your home machine (not a server)."""
    # Step 1: GET login page for CSRF token
    async with session.get(
        f"{BASE_URL}/login",
        headers={"User-Agent": USER_AGENT},
    ) as r:
        if r.status == 403:
            print(
                "ERROR: Cloudflare blocked the request (403).\n"
                "  → Run this script on your own machine, not a cloud server.\n"
                "  → Or use --session with your browser cookie instead.",
                file=sys.stderr,
            )
            return False
        text = await r.text()
        csrf = _find_csrf(text)
        if not csrf:
            print(
                "ERROR: Could not find CSRF token on login page.\n"
                "  → Try --session instead (copy sessionid cookie from browser DevTools).",
                file=sys.stderr,
            )
            return False

    # Step 2: POST credentials
    async with session.post(
        f"{BASE_URL}/login",
        data={
            "username": username,
            "password": password,
            "csrfmiddlewaretoken": csrf,
        },
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE_URL}/login",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        allow_redirects=False,
    ) as r:
        if r.status in (301, 302):
            print(f"✓ Logged in as {username}")
            return True
        body = await r.text()
        if "Invalid" in body or "incorrect" in body.lower():
            print("ERROR: Wrong username or password.", file=sys.stderr)
        else:
            print(f"ERROR: Login returned {r.status}. Try --session instead.", file=sys.stderr)
        return False


def _login_cloudscraper(username: str, password: str) -> str:
    """Login using cloudscraper which handles Cloudflare JS challenges."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    # Get CSRF
    r = scraper.get(f"{BASE_URL}/login")
    csrf = _find_csrf(r.text)
    if not csrf:
        print("ERROR: Could not get CSRF token even with cloudscraper.", file=sys.stderr)
        print("  → Use --session instead (copy sessionid from browser DevTools)", file=sys.stderr)
        return ""
    # POST login
    r = scraper.post(
        f"{BASE_URL}/login",
        data={"username": username, "password": password, "csrfmiddlewaretoken": csrf},
        headers={"Referer": f"{BASE_URL}/login"},
        allow_redirects=False,
    )
    if r.status_code in (301, 302):
        return scraper.cookies.get("sessionid", "")
    print("ERROR: Login failed — check username/password.", file=sys.stderr)
    return ""


def _find_csrf(html: str) -> str:
    for pattern in [
        r'name="csrfmiddlewaretoken"\s+value="([^"]+)"',
        r'csrfmiddlewaretoken.*?value="([^"]+)"',
        r'"csrfmiddlewaretoken":\s*"([^"]+)"',
    ]:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return ""


# ── Fetch challenges ──────────────────────────────────────────────────────────

async def fetch_all_challenges(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all available practice challenges via picoCTF API."""
    all_challenges: list[dict] = []
    page = 1
    while True:
        url = f"{BASE_URL}/api/challenges/?format=json&page={page}&limit=100"
        async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
            if r.status == 401:
                print("ERROR: Not authenticated. Re-run with valid --session or credentials.", file=sys.stderr)
                return []
            if r.status != 200:
                break
            data = await r.json()

        if isinstance(data, list):
            all_challenges.extend(data)
            break
        elif isinstance(data, dict):
            results = data.get("results") or data.get("data") or []
            all_challenges.extend(results)
            if not data.get("next") or not results:
                break
        else:
            break
        page += 1

    return all_challenges


async def fetch_challenge_detail(session: aiohttp.ClientSession, cid: int) -> dict:
    url = f"{BASE_URL}/api/challenges/{cid}/?format=json"
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
        if r.status == 200:
            return await r.json()
    return {}


# ── Filter helpers ────────────────────────────────────────────────────────────

def _name_match(challenge: dict, picks: list[str]) -> bool:
    """Check if a challenge matches any of the picked names (fuzzy)."""
    name = (challenge.get("name") or challenge.get("title") or "").lower()
    return any(p.lower() in name or name in p.lower() for p in picks)


# ── Save to disk ──────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-") or "challenge"


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html or "")
    for e, r in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(e, r)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def download_file(session: aiohttp.ClientSession, url: str, dest: Path) -> bool:
    if not url.startswith("http"):
        url = BASE_URL.rstrip("/") + "/" + url.lstrip("/")
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
        if r.status == 200:
            dest.write_bytes(await r.read())
            return True
    return False


async def save_challenge(session: aiohttp.ClientSession, ch: dict, output_dir: Path) -> Path | None:
    name = ch.get("name") or ch.get("title") or f"id-{ch.get('id', '?')}"
    category = ch.get("category") or ch.get("category_name") or "misc"
    pts = ch.get("score") or ch.get("value") or ch.get("points") or 0

    slug = slugify(name)
    chdir = output_dir / slug
    chdir.mkdir(parents=True, exist_ok=True)

    # Download attached files
    files = ch.get("files") or ch.get("file_list") or []
    distfiles_dir = chdir / "distfiles"
    for f in files:
        url = (f if isinstance(f, str) else f.get("url") or f.get("download_url") or "")
        if not url:
            continue
        distfiles_dir.mkdir(exist_ok=True)
        fname = url.split("/")[-1].split("?")[0] or "file"
        ok = await download_file(session, url, distfiles_dir / fname)
        if ok:
            print(f"    ↓ {fname}")

    # Connection info
    conn = ch.get("connection_info") or ""
    if not conn:
        host = ch.get("host") or ""
        port = ch.get("port") or ""
        if host and port:
            conn = f"nc {host} {port}"

    # Hints
    hints = []
    for h in (ch.get("hints") or []):
        text = h if isinstance(h, str) else (h.get("hint") or h.get("body") or h.get("content") or "")
        if text:
            hints.append({"cost": 0, "content": _strip_html(text)})

    meta: dict = {
        "version": "beta1",
        "name": name,
        "category": category,
        "description": _strip_html(ch.get("description") or ch.get("problem_statement") or ""),
        "value": pts,
        "solves": ch.get("solves") or ch.get("solution_count") or 0,
    }
    if hints:
        meta["hints"] = hints
    if conn:
        meta["connection_info"] = conn

    (chdir / "metadata.yml").write_text(
        yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return chdir


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(
    output: str,
    username: str = "",
    password: str = "",
    session_id: str = "",
    category: str = "",
    picks: list[str] | None = None,
    limit: int = 0,
) -> None:
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    cookies = {"sessionid": session_id} if session_id else {}
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(connector=connector, cookies=cookies) as session:
        # Auth
        if not session_id:
            if not username:
                print("ERROR: Provide --username/--password or --session", file=sys.stderr)
                sys.exit(1)
            # Try cloudscraper first (handles Cloudflare)
            if HAS_CLOUDSCRAPER:
                sid = _login_cloudscraper(username, password)
                if sid:
                    # Inject the session cookie
                    session.cookie_jar.update_cookies({"sessionid": sid}, aiohttp.ClientConnectorError)
                    cookies["sessionid"] = sid
                    print(f"✓ Logged in as {username} (via cloudscraper)")
                else:
                    sys.exit(1)
            else:
                if not await login(session, username, password):
                    print("\nTip: pip install cloudscraper  — handles Cloudflare automatically", file=sys.stderr)
                    sys.exit(1)

        # Fetch challenge list
        print("Fetching challenge list...")
        challenges = await fetch_all_challenges(session)
        if not challenges:
            sys.exit(1)
        print(f"Found {len(challenges)} total challenges\n")

        # Filter
        filtered = challenges
        if picks:
            filtered = [c for c in challenges if _name_match(c, picks)]
            print(f"Matched {len(filtered)} challenge(s) from --pick\n")
        elif category:
            filtered = [
                c for c in challenges
                if category.lower() in (c.get("category") or c.get("category_name") or "").lower()
            ]
            print(f"Filtered to {len(filtered)} [{category}] challenges\n")

        if limit:
            filtered = filtered[:limit]

        if not filtered:
            print("No challenges matched. Available categories:")
            cats = sorted(set(
                c.get("category") or c.get("category_name") or "?"
                for c in challenges
            ))
            for cat in cats:
                count = sum(
                    1 for c in challenges
                    if (c.get("category") or c.get("category_name") or "") == cat
                )
                print(f"  {cat}: {count} challenges")
            sys.exit(0)

        # Pull each challenge
        pulled = 0
        for ch in filtered:
            name = ch.get("name") or ch.get("title") or str(ch.get("id"))
            cat = ch.get("category") or ch.get("category_name") or "?"
            pts = ch.get("score") or ch.get("value") or ch.get("points") or 0
            print(f"  [{cat}] {name} ({pts} pts)")

            # Fetch full detail if description missing
            if not ch.get("description") and ch.get("id"):
                ch = await fetch_challenge_detail(session, ch["id"]) or ch

            path = await save_challenge(session, ch, output_dir)
            if path:
                pulled += 1

        print(f"\n✓ Pulled {pulled} challenge(s) → {output_dir.resolve()}")
        print()
        print("Run the agent (use --no-submit since picoCTF flag submission is manual):")
        print(f"  uv run ctf-solve \\")
        print(f"    --ctfd-url {BASE_URL} \\")
        print(f"    --ctfd-token DUMMY \\")
        print(f"    --challenges-dir {output_dir} \\")
        print(f"    --no-submit \\")
        print(f"    --models ollama/qwen2.5-coder:7b \\")
        print(f"    --coordinator claude")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pull picoCTF picoGym challenges for ctf-agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Pull 10 web challenges
  python pull_picoctf.py --username KismatKunwar --password yourpass --category web --limit 10

  # Pull specific challenges by name
  python pull_picoctf.py --username KismatKunwar --password yourpass \\
      --pick "format string 0" "buffer overflow 1" "web gauntlet" "cookie monster"

  # Pull crypto challenges
  python pull_picoctf.py --username KismatKunwar --password yourpass --category crypto --limit 15

  # Use session cookie if login is blocked
  python pull_picoctf.py --session abc123yoursessionid --category misc --limit 5
        """,
    )
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--username", help="picoCTF username")
    auth.add_argument("--session", help="sessionid cookie from browser DevTools")

    parser.add_argument("--password", help="Password (required with --username)")
    parser.add_argument("--output", default="./pico-challenges", help="Output directory")
    parser.add_argument("--category", default="", help="Filter: web, crypto, pwn, rev, forensics, misc, general")
    parser.add_argument("--pick", nargs="+", metavar="NAME", help="Pick specific challenges by name (partial match)")
    parser.add_argument("--limit", type=int, default=0, help="Max challenges (0=all matched)")

    args = parser.parse_args()

    if args.username and not args.password:
        parser.error("--password is required with --username")

    asyncio.run(main(
        output=args.output,
        username=args.username or "",
        password=args.password or "",
        session_id=args.session or "",
        category=args.category,
        picks=args.pick,
        limit=args.limit,
    ))
