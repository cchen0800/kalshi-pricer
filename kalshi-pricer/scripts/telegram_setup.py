"""One-shot: detect your Telegram chat ID and write it to .env.

Workflow:
  1. Create a bot with @BotFather, get the bot token.
  2. Open your bot in Telegram and send it any message (e.g. '/start').
  3. Set TELEGRAM_BOT_TOKEN in .env (or pass as argv).
  4. Run this script — it queries Telegram's getUpdates and finds your chat ID.

If no chat is found, the script tells you what to do.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

DOTENV = Path(__file__).resolve().parent.parent / ".env"


def update_env(updates: dict[str, str]) -> None:
    """Write/replace the given keys in .env without clobbering other lines."""
    existing = DOTENV.read_text() if DOTENV.exists() else ""
    lines = existing.splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                lines[i] = f"{key}={updates[key]}"
                seen.add(key)
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}")
    DOTENV.write_text("\n".join(lines) + "\n")


def main() -> int:
    load_dotenv()
    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("error: pass token as argv[1] or set TELEGRAM_BOT_TOKEN in .env", file=sys.stderr)
        return 2

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        resp = httpx.get(url, timeout=10.0)
    except httpx.RequestError as e:
        print(f"error: failed to reach Telegram: {e}", file=sys.stderr)
        return 3
    if resp.status_code != 200:
        print(f"error: getUpdates {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return 3

    data = resp.json()
    if not data.get("ok"):
        print(f"error: Telegram returned not-ok: {data}", file=sys.stderr)
        return 3

    updates = data.get("result", [])
    chats: dict[str, str] = {}  # chat_id -> first_name or title
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            chats[str(chat["id"])] = label

    if not chats:
        print("No chats found yet. To register your chat:")
        print(f"  1. Open your bot in Telegram")
        print(f"  2. Send it any message (e.g. /start or 'hi')")
        print(f"  3. Re-run this script")
        return 4

    if len(chats) == 1:
        chat_id, label = next(iter(chats.items()))
        print(f"Found chat: id={chat_id}  ({label})")
    else:
        print("Multiple chats found:")
        for cid, lab in chats.items():
            print(f"  id={cid}  ({lab})")
        chat_id = sys.argv[2] if len(sys.argv) > 2 else next(iter(chats))
        print(f"Using chat_id={chat_id} (pass argv[2] to choose explicitly)")

    update_env({
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID": chat_id,
    })
    print(f"Wrote TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to {DOTENV}")

    # Send a confirmation message.
    send_url = f"https://api.telegram.org/bot{token}/sendMessage"
    httpx.post(send_url, json={"chat_id": chat_id, "text": "kalshi-pricer: Telegram setup OK"}, timeout=5.0)
    print("Sent confirmation message — check Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
