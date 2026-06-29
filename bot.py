import json
import requests
import base64
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
import asyncio
import os
from io import BytesIO
from flask import Flask, request
from threading import Thread

print("TOKEN EXISTS:", bool(os.getenv("BOT_TOKEN")))
app = Flask(__name__)

@app.route("/")
def home():
    return "ACHIEVER 8.0 Bot Running"

def run_web():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )

TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()

DATA_FILE = "data.json"   # kept for reference; live publishing target — see "Draft -> Publish" section below
DRAFT_FILE = "draft.json"  # everything you edit lives here until /publish


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string, e.g. 2026-06-20T10:30:00Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Used only to backfill created_at on folders that existed before this
# feature was added (see backfill_created_at below). Not a "real" creation
# date — just a fixed starting point so legacy siblings still sort sensibly
# relative to each other.
MIGRATION_EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc)

# Multi-admin support: ADMIN_IDS is a comma-separated list of Telegram user IDs.
# Backward compatible: if only the old ADMIN_ID env var is set, it still works.
_admin_ids_env = os.getenv("ADMIN_IDS", "")
_admin_id_single = os.getenv("ADMIN_ID", "8837854952")
if _admin_ids_env:
    ADMIN_IDS = set(int(x.strip()) for x in _admin_ids_env.split(",") if x.strip().isdigit())
else:
    ADMIN_IDS = {int(_admin_id_single)}
# Legacy single ADMIN_ID kept for callback checks that existed before multi-admin
ADMIN_ID = int(_admin_id_single)


def is_admin(message: Message) -> bool:
    return message.from_user.id in ADMIN_IDS


def is_admin_id(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_command_args(message: Message) -> str:
    """
    Returns everything after the command token.
    Splitting on whitespace (instead of replacing an exact "/cmd " string)
    keeps this working even if the command arrives with a bot mention,
    e.g. "/addbutton@MyBot Folder|Video|URL" in a group chat.
    """
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def split_pipe_args(message: Message) -> list:
    """Get command args and split on '|', stripping whitespace from each piece."""
    args = get_command_args(message)
    if not args:
        return []
    return [p.strip() for p in args.split("|")]


# ─── Mobile UX layer (buttons, recent folders, delete-confirm, panel) ───────
#
#   Everything in this section is purely additive: tappable Telegram inline
#   keyboards and small bits of in-memory admin state layered on top of the
#   existing text-command handlers below. No existing command, argument
#   format, or stored data shape changes because of this section — it only
#   ever calls the same add_node/delete_node/rename_node/find_node_by_path/
#   resolve_path/load_data/save_data helpers the text commands already use.
#
#   Path <-> callback_data encoding
#   --------------------------------
#   Telegram callback_data is limited to 64 bytes, and folder names may
#   contain spaces/punctuation, so paths are joined with "\x1f" (a control
#   character that will never appear in an admin-typed folder name) instead
#   of "|" (which folder names are allowed to contain, since "|" is the
#   existing path separator for typed commands).

CB_SEP = "\x1f"


def encode_cb_path(prefix: str, path_parts: list) -> str:
    return prefix + CB_SEP + CB_SEP.join(path_parts)


def decode_cb_path(data: str) -> list:
    parts = data.split(CB_SEP)
    rest = parts[1:]
    return [p for p in rest if p != ""]


def location_footer(path_parts: list) -> str:
    """The '📍 Current: SSC > Math' footer shown after admin actions."""
    where = " > ".join(path_parts) if path_parts else "root"
    return f"\n\n📍 Current: {where}"


# Telegram caps callback_data at 64 bytes. A single folder/child name is
# always safe to put there directly (admin-chosen names are short in
# practice, and even the longest ones in this dataset are well under the
# limit on their own — it's only FULL paths several levels deep that can
# blow past 64 bytes). So:
#   - folder_nav_keyboard only ever encodes ONE child name at a time
#     ("nav\x1fChildName") — the rest of the path is the admin's current
#     cwd, which is already tracked server-side and doesn't need to round
#     -trip through callback_data at all.
#   - /recent and /find can surface paths many levels deep, which DO risk
#     exceeding 64 bytes if spelled out in full. Those instead store the
#     real path in a small per-user lookup table and put only a short
#     numeric token ("navp\x1f3") in callback_data.

def folder_nav_keyboard(folders: list, cwd_path: list, node: dict | None) -> InlineKeyboardMarkup:
    """
    Buttons for every child folder at this node (tap to /cd into it), plus a
    Home/Back row where appropriate (Back hidden at root, Home hidden at
    root since it would be a no-op there).
    """
    rows = []
    children = node.get("children", []) if node is not None else folders
    for child in children:
        rows.append([InlineKeyboardButton(
            text=f"📁 {child['name']}",
            callback_data=encode_cb_path("nav", [child["name"]]),
        )])

    nav_row = []
    if cwd_path:
        nav_row.append(InlineKeyboardButton(text="⬅️ Back", callback_data="navback"))
        nav_row.append(InlineKeyboardButton(text="🏠 Home", callback_data="navhome"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# Per-user short-token registry for full (possibly deep) paths shown by
# /recent and /find, so callback_data only ever has to carry a small index
# instead of the whole path text. Cleared/overwritten each time /recent or
# /find runs, so tokens never grow unbounded.
admin_path_tokens: dict = {}   # user_id -> {token_str: path_list}


def register_path_token(user_id: int, path: list) -> str:
    tokens = admin_path_tokens.setdefault(user_id, {})
    token = str(len(tokens))
    tokens[token] = list(path)
    return token


def resolve_path_token(user_id: int, token: str) -> list | None:
    return admin_path_tokens.get(user_id, {}).get(token)


def reset_path_tokens(user_id: int):
    admin_path_tokens[user_id] = {}


# /recent — last-opened folders per admin, most-recent first, deduplicated.
# In-memory only, same lifetime/reset behaviour as admin_cwd below.
RECENT_LIMIT = 8
admin_recent: dict = {}   # user_id -> list of canonical path lists


def record_recent(user_id: int, path: list):
    if not path:
        return
    lst = admin_recent.setdefault(user_id, [])
    lst[:] = [p for p in lst if p != path]
    lst.insert(0, list(path))
    del lst[RECENT_LIMIT:]


def get_recent(user_id: int) -> list:
    return list(admin_recent.get(user_id, []))


def recent_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    recents = get_recent(user_id)
    if not recents:
        return None
    reset_path_tokens(user_id)
    rows = []
    for p in recents:
        token = register_path_token(user_id, p)
        rows.append([InlineKeyboardButton(
            text=f"📁 {' > '.join(p)}",
            callback_data=encode_cb_path("navp", [token]),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# /find keyword — recursive search across the whole draft tree for folders
# and buttons whose name/title contains the keyword (case-insensitive).
def search_tree(nodes: list, keyword: str, path: list, results: list):
    keyword = keyword.lower()
    for node in nodes:
        node_path = path + [node["name"]]
        if keyword in node["name"].lower():
            results.append(("folder", node_path, None))
        for b in node.get("buttons", []):
            if keyword in b["title"].lower():
                results.append(("button", node_path, b["title"]))
        children = node.get("children", [])
        if children:
            search_tree(children, keyword, node_path, results)


def find_results_keyboard(user_id: int, results: list) -> InlineKeyboardMarkup | None:
    # Only folder hits are tappable-to-navigate; button hits already show
    # their full path as text, since a button isn't something you "open".
    folder_hits = [r for r in results if r[0] == "folder"]
    if not folder_hits:
        return None
    reset_path_tokens(user_id)
    rows = []
    for _, path, _ in folder_hits[:20]:   # keep the keyboard a sane size
        token = register_path_token(user_id, path)
        rows.append([InlineKeyboardButton(
            text=f"📁 {' > '.join(path)}",
            callback_data=encode_cb_path("navp", [token]),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)



# Delete-confirmation. Holds exactly what the original command would have
# done, so confirming just replays it — the delete commands' own logic
# below is never duplicated.
pending_delete: dict = {}   # user_id -> {"kind": ..., "path": [...], "label": "..."}


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes", callback_data="delyes"),
        InlineKeyboardButton(text="❌ No", callback_data="delno"),
    ]])


async def request_delete_confirmation(message: Message, kind: str, path: list, label: str, label_extra: str = None):
    """
    Stash what /deletefolder, /deletesubfolder or /deletebutton was about
    to do, and ask for confirmation instead of doing it immediately. The
    delyes callback below performs the actual deletion using the exact
    same delete_node() / button-filter logic those commands already had —
    nothing about how a delete is carried out changes, only that it now
    waits for a tap first.
    """
    pending_delete[message.from_user.id] = {
        "kind": kind, "path": path, "label_extra": label_extra,
    }
    await message.answer(f"⚠️ Confirm Delete?\n\nDelete {label}?", reply_markup=confirm_keyboard())


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 Folders", callback_data="panel_folders"),
         InlineKeyboardButton(text="📝 Drafts", callback_data="panel_drafts")],
        [InlineKeyboardButton(text="➕ Add Button", callback_data="panel_addbutton"),
         InlineKeyboardButton(text="🔗 Set Link", callback_data="panel_setlink")],
        [InlineKeyboardButton(text="📊 Sort Order", callback_data="panel_sortorder"),
         InlineKeyboardButton(text="🔍 Search", callback_data="panel_search")],
        [InlineKeyboardButton(text="📦 Backup", callback_data="panel_backup"),
         InlineKeyboardButton(text="🧾 Backups List", callback_data="panel_backups_list")],
        [InlineKeyboardButton(text="⏪ Rollback", callback_data="panel_rollback"),
         InlineKeyboardButton(text="🆚 Diff", callback_data="panel_diff")],
        [InlineKeyboardButton(text="📈 Stats", callback_data="panel_stats"),
         InlineKeyboardButton(text="⚙ Settings", callback_data="panel_settings")],
    ])


def count_buttons(nodes: list) -> int:
    """Recursively count every button (piece of content) in a tree — used
    for the Published/Drafts counts shown by /panel."""
    total = 0
    for node in nodes:
        total += len(node.get("buttons", []))
        total += count_buttons(node.get("children", []))
    return total


# ─── GitHub sync ──────────────────────────────────────────────────────────────
#
#   Draft -> Publish workflow:
#   - draft.json  = working copy. EVERY edit command (add/delete/rename
#                   folder, addbutton, setlink, etc.) reads and writes this
#                   file by default — nothing they do touches data.json.
#   - data.json   = the LIVE file the public website actually fetches.
#                   It only changes when /publish is run.
#
#   load_data()/save_data() both default to "draft.json" so none of the
#   existing command handlers below needed to change at all — they already
#   call load_data() / save_data(data) with no filename, and now that
#   default quietly points at the draft instead of the live file.

def load_data(filename=DRAFT_FILE):
    github_token = os.getenv("GITHUB_TOKEN")
    github_user  = os.getenv("GITHUB_USERNAME")
    github_repo  = os.getenv("GITHUB_REPO")

    url = f"https://api.github.com/repos/{github_user}/{github_repo}/contents/{filename}"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json"
    }

    response = requests.get(url, headers=headers).json()
    if "content" not in response:
        raise RuntimeError(
            f"Couldn't read '{filename}' from GitHub: {response.get('message', response)}. "
            f"If this is draft.json, run /initdraft first."
        )
    content = base64.b64decode(response["content"]).decode("utf-8")
    data    = json.loads(content)

    # Every read is auto-upgraded (new button structure + created_at
    # backfill) in memory. The next time anything calls save_data() on this
    # same filename (any admin command, /publish, or /migrate), the
    # upgraded shape gets written back to GitHub.
    migrate_data(data)
    return data


def save_data(data, filename=DRAFT_FILE):
    github_token = os.getenv("GITHUB_TOKEN")
    github_user  = os.getenv("GITHUB_USERNAME")
    github_repo  = os.getenv("GITHUB_REPO")

    url = f"https://api.github.com/repos/{github_user}/{github_repo}/contents/{filename}"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json"
    }

    current = requests.get(url, headers=headers).json()
    sha     = current.get("sha")  # None if the file doesn't exist yet -> GitHub creates it

    content = json.dumps(data, indent=2)
    encoded = base64.b64encode(content.encode()).decode()

    payload = {
        "message": f"Update {filename} from bot",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=headers, json=payload)
    if not r.ok:
        raise RuntimeError(f"GitHub write to '{filename}' failed: {r.text}")


# ─── Recursive tree helpers (single source of truth for the whole tree) ──────

def find_node_by_path(folders, path_parts):
    """
    Walk a list of folder dicts following path_parts (case-insensitive names).
    Returns the target node dict, or None if any step is not found.

    path_parts = ["Grammar"]           → returns the Grammar folder node
    path_parts = ["Grammar", "Tense"]  → returns the Tense subfolder node
    """
    node_list = folders
    node = None

    for part in path_parts:
        found = None
        for item in node_list:
            if item["name"].lower() == part.lower():
                found = item
                break
        if found is None:
            return None
        node = found
        node_list = node.get("children", [])

    return node


def ensure_node_fields(node):
    """Make sure a folder node has both 'children' and 'buttons' keys (backward compatibility)."""
    if "children" not in node:
        node["children"] = []
    if "buttons" not in node:
        node["buttons"] = []


def get_node_list(folders, parent_path):
    """
    Returns the list a child node lives in for a given parent path.
    An EMPTY parent_path means "the root folders list" — this is what lets
    root-level folders and subfolders at any depth share one add/delete/
    rename implementation instead of two separate, duplicated code paths.

    Returns None if parent_path doesn't resolve to an existing node.
    """
    if not parent_path:
        return folders
    parent = find_node_by_path(folders, parent_path)
    if parent is None:
        return None
    ensure_node_fields(parent)
    return parent["children"]


def add_node(folders, parent_path, new_name):
    """
    Add a folder/subfolder at any depth, including the root (parent_path = []).
    Rejects case-insensitive duplicate names at the same level — without this,
    a second "Maths" folder next to an existing "Maths" would silently become
    permanently unreachable, since find_node_by_path only ever returns the
    first name match.
    """
    node_list = get_node_list(folders, parent_path)
    if node_list is None:
        return False, f"Path not found: {' > '.join(parent_path)}"
    if any(n["name"].lower() == new_name.lower() for n in node_list):
        return False, f"'{new_name}' already exists there"
    node_list.append({
        "name": new_name,
        "children": [],
        "buttons": [],
        "created_at": now_iso(),
    })
    return True, None


def delete_node(folders, full_path):
    """
    Delete a folder/subfolder at any depth. full_path includes the target
    name as its last element; everything before it is the parent path
    (empty parent path = deleting a root-level folder).
    """
    parent_path = full_path[:-1]
    target_name = full_path[-1]

    node_list = get_node_list(folders, parent_path)
    if node_list is None:
        return False, f"Path not found: {' > '.join(parent_path)}"

    new_list = [n for n in node_list if n["name"].lower() != target_name.lower()]
    if len(new_list) == len(node_list):
        return False, f"'{target_name}' not found"

    if parent_path:
        parent = find_node_by_path(folders, parent_path)
        parent["children"] = new_list
    else:
        folders[:] = new_list

    return True, None


def rename_node(folders, full_path, new_name):
    """
    Rename a folder/subfolder at any depth. full_path includes the current
    name as its last element (empty parent path = renaming a root folder).
    """
    parent_path = full_path[:-1]
    old_name = full_path[-1]

    node_list = get_node_list(folders, parent_path)
    if node_list is None:
        return False, f"Path not found: {' > '.join(parent_path)}"

    target = None
    for n in node_list:
        if n["name"].lower() == old_name.lower():
            target = n
            break
    if target is None:
        return False, f"'{old_name}' not found"

    if any(n is not target and n["name"].lower() == new_name.lower() for n in node_list):
        return False, f"'{new_name}' already exists there"

    target["name"] = new_name
    return True, None


# ─── Legacy → dynamic-button migration ───────────────────────────────────────
#
# Old lecture nodes may carry fixed fields like "video", "notes", "quiz",
# "pptPdf" directly on the node instead of a buttons list, or an older
# "links" array (used before "buttons" existed). This walks the whole tree
# and converts anything it finds into proper {"title", "url"} button
# entries, removing the old fields. Matching is case-insensitive so
# "video", "Video", "engNotes", "eng_notes" etc. are all recognized.
# Idempotent — running it on already-migrated data changes nothing.

LEGACY_BUTTON_FIELDS = {
    "video":       "Video",
    "engnotes":    "Eng Notes",
    "eng_notes":   "Eng Notes",
    "hindinotes":  "Hindi Notes",
    "hindi_notes": "Hindi Notes",
    "notes":       "Notes",
    "quiz":        "Quiz",
    "pptpdf":      "PPT/PDF",
    "ppt_pdf":     "PPT/PDF",
    "ppt":         "PPT",
    "pdf":         "PDF",
}


def backfill_created_at(node, sibling_index):
    """
    If a folder predates the timestamp feature, give it a synthetic
    created_at so newest-first ordering still makes sense. Synthetic times
    increase with the node's existing position in its sibling list (folders
    were always appended on creation, so array order already approximates
    creation order) — so sorting newest-first naturally puts the
    last-added legacy folder first, same as it would for a folder that
    has a real timestamp. Folders created via /addfolder or /addsubfolder
    already get a true created_at in add_node() and are never touched here.
    Idempotent: does nothing if created_at is already set.
    """
    if not node.get("created_at"):
        synthetic = MIGRATION_EPOCH + timedelta(seconds=sibling_index * 60)
        node["created_at"] = synthetic.strftime("%Y-%m-%dT%H:%M:%SZ")


def migrate_node(node, sibling_index=0):
    backfill_created_at(node, sibling_index)

    # old "links" array → "buttons"
    if "buttons" not in node:
        node["buttons"] = node.pop("links", [])
    elif "links" in node:
        node["buttons"].extend(node.pop("links"))

    # old fixed fields (video/notes/quiz/pptPdf/...) → button entries
    for key in list(node.keys()):
        label = LEGACY_BUTTON_FIELDS.get(key.lower())
        if label is None:
            continue
        url = node.pop(key)
        if url:
            node["buttons"].append({"title": label, "url": url})

    node.setdefault("children", [])
    for i, child in enumerate(node["children"]):
        migrate_node(child, i)


def migrate_data(data):
    data.setdefault("folders", [])
    for i, folder in enumerate(data["folders"]):
        migrate_node(folder, i)
    return data


# ─── Folder Navigation session (/cd, /ls, /pwd, /back, /exit) ───────────────
#
#   Lets the admin "enter" a folder once with /cd and then drop the
#   "Folder|Sub|..." prefix on every command that follows, instead of
#   retyping the full path on every single /addbutton, /setlink,
#   /deletebutton, /addsubfolder, etc.
#
#   This is purely an admin-session convenience layered on TOP of the
#   existing path-based commands — none of those were rewritten to work
#   differently; they still receive a plain path list exactly like before.
#   The only thing that changes is *which* path list they're handed:
#
#     - No folder selected (nobody has run /cd yet, or /exit / /back to
#       root was used) -> resolve_path() hands back exactly what the admin
#       typed. Every old command behaves 100% the same as before this
#       feature existed.
#     - A folder IS selected -> resolve_path() tries "current folder +
#       whatever the admin typed" first. If that resolves to a real node,
#       it's used. If not (e.g. the admin typed a full old-style path out
#       of habit while still inside a folder), it falls back to treating
#       what was typed as a complete path on its own — so old, fully
#       qualified commands keep working unchanged even while a folder is
#       selected.
#
#   Session state lives in a dict keyed by Telegram user id (rather than a
#   single bare variable) purely so this can't get confused if ADMIN_ID is
#   ever more than one person in future.
#
#   PERSISTENCE: Render's free tier wipes the local filesystem on every
#   sleep/wake cycle and redeploy — so a plain in-memory dict (or even a
#   local JSON file) loses the admin's current folder with zero warning,
#   and the very next path-relative command (e.g. /addbutton Label|URL)
#   then gets silently misread as if no folder were selected. To survive
#   that, the cwd is mirrored to a tiny CWD_STATE_FILE in the same GitHub
#   repo draft.json already lives in, via dedicated lightweight read/write
#   helpers below (deliberately not load_data()/save_data(), which assume
#   the {"folders": [...]} tree shape). It's loaded once at startup and
#   only written back to GitHub on actual /cd / /back / /exit / folder-tap
#   changes (not on every single command), so this doesn't add a GitHub
#   round trip to /addbutton, /setlink, etc. — only to the handful of
#   commands that actually change which folder is selected.

CWD_STATE_FILE = "cwd_state.json"

admin_cwd: dict = {}   # user_id -> list of folder names (the path), [] = root


def _github_headers():
    github_token = os.getenv("GITHUB_TOKEN")
    return {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json",
    }


def _github_url(filename):
    github_user = os.getenv("GITHUB_USERNAME")
    github_repo = os.getenv("GITHUB_REPO")
    return f"https://api.github.com/repos/{github_user}/{github_repo}/contents/{filename}"


def _save_compact_json_to_github(filename, data, commit_message):
    """
    Like save_data(), but writes minified JSON (no indent) instead of
    pretty-printed. Used only for the website's lazy-load shell/chunk
    files (see build_lazy_payload below) — those are pure machine-read
    performance artifacts, never meant to be read by a human in git
    history, so there's no reason to ship the extra whitespace bytes to
    every visitor's phone.
    """
    url = _github_url(filename)
    headers = _github_headers()
    current = requests.get(url, headers=headers).json()
    sha = current.get("sha")

    content = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    encoded = base64.b64encode(content.encode("utf-8")).decode()

    payload = {
        "message": commit_message,
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=headers, json=payload)
    if not r.ok:
        raise RuntimeError(f"GitHub write to '{filename}' failed: {r.text}")


def _load_cwd_state_from_github():
    """
    Dedicated GitHub read for CWD_STATE_FILE — deliberately NOT reusing
    load_data(), since that runs migrate_data() (which assumes a
    {"folders": [...]} tree shape and would inject a bogus "folders" key
    into this unrelated {user_id: [path]} payload on every load).
    """
    try:
        response = requests.get(_github_url(CWD_STATE_FILE), headers=_github_headers()).json()
        if "content" not in response:
            # File doesn't exist yet (first run ever), or repo unreachable
            # at startup — fall back to an empty session table, same as
            # the original in-memory-only behaviour. The file is created
            # automatically the first time set_cwd()/clear_cwd() runs.
            return {}
        content = base64.b64decode(response["content"]).decode("utf-8")
        raw = json.loads(content)
    except Exception:
        return {}

    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[int(k)] = list(v)
        except (TypeError, ValueError):
            continue
    return out


def _save_cwd_state_to_github():
    try:
        url = _github_url(CWD_STATE_FILE)
        headers = _github_headers()
        current = requests.get(url, headers=headers).json()
        sha = current.get("sha")

        payload_data = {str(k): v for k, v in admin_cwd.items()}
        content = json.dumps(payload_data)
        encoded = base64.b64encode(content.encode()).decode()

        payload = {
            "message": "Update cwd_state.json from bot",
            "content": encoded,
        }
        if sha:
            payload["sha"] = sha

        requests.put(url, headers=headers, json=payload)
    except Exception:
        # Best-effort — if this particular write fails (rate limit, network
        # hiccup), the admin's session just stays whatever it was in memory
        # for the rest of this process's life; it'll resync on next restart.
        pass


admin_cwd.update(_load_cwd_state_from_github())


def get_cwd(user_id: int) -> list:
    return list(admin_cwd.get(user_id, []))


def set_cwd(user_id: int, path: list):
    admin_cwd[user_id] = list(path)
    _save_cwd_state_to_github()


def clear_cwd(user_id: int):
    admin_cwd.pop(user_id, None)
    _save_cwd_state_to_github()


def get_canonical_path(folders, path_parts):
    """
    Same walk as find_node_by_path, but returns the path spelled with each
    node's real stored name/casing instead of whatever the admin typed —
    so /pwd and "Current Folder" confirmations always show the true name.
    Returns None if any part of the path doesn't exist.
    """
    node_list = folders
    canonical = []
    for part in path_parts:
        found = None
        for item in node_list:
            if item["name"].lower() == part.lower():
                found = item
                break
        if found is None:
            return None
        canonical.append(found["name"])
        node_list = found.get("children", [])
    return canonical


def resolve_path(folders, user_id: int, typed_path_parts: list) -> list:
    """
    Decide which folder path a path-taking command should actually act on,
    given what the admin typed and their currently selected folder (if
    any). See the section comment above for the full rationale.
    """
    cwd = get_cwd(user_id)
    if not cwd:
        return typed_path_parts

    relative = cwd + typed_path_parts
    if find_node_by_path(folders, relative) is not None:
        return relative

    if typed_path_parts and find_node_by_path(folders, typed_path_parts) is not None:
        return typed_path_parts

    # Neither resolves to an existing node yet — e.g. /addsubfolder
    # creating something brand new under the current folder. Default to
    # the shorthand (cwd-relative) interpretation, since a folder is
    # currently selected.
    return relative


# ─── /cd, /ls, /pwd, /back, /exit ────────────────────────────────────────────
#
#   Short aliases added per the mobile-UX request: /c /l /p /b /e.
#   Stacking a second @dp.message(Command(...)) decorator on the same
#   handler (the same pattern already used below for /addbutton+/addlink)
#   registers both names against the identical function — no behaviour
#   forked, nothing duplicated.

async def render_ls(message_or_cb, user_id: int, edit: bool = False):
    """
    Shared by /ls (and its alias /l) and the 'nav'/'navhome'/'navback'
    button callbacks, so tapping a folder button shows exactly the same
    view typing /ls there would have shown — including the tappable
    sub-folder keyboard and Home/Back row.
    """
    data = load_data()
    cwd = get_cwd(user_id)

    if cwd:
        node = find_node_by_path(data["folders"], cwd)
        if node is None:
            clear_cwd(user_id)
            text = "⚠️ Current folder no longer exists — moved back to root."
            if edit:
                await message_or_cb.message.edit_text(text)
            else:
                await message_or_cb.answer(text)
            return
        ensure_node_fields(node)
        children = node.get("children", [])
        buttons  = node.get("buttons", [])
        link     = node.get("link")
        header   = f"📂 {' > '.join(cwd)}"
    else:
        node     = None
        children = data["folders"]
        buttons  = []
        link     = None
        header   = "📂 root"

    lines = [header]
    if link:
        lines.append(f"\n🔗 Direct link: {link}")
    if children:
        lines.append("\nFolders (tap to open 👇):")
    if buttons:
        lines.append("\nButtons:")
        lines.extend(f"  🔘 {b['title']} → {b['url']}" for b in buttons)
    if not children and not buttons and not link:
        lines.append("\n(empty)")

    text = "\n".join(lines)
    kb = folder_nav_keyboard(data["folders"], cwd, node)

    if edit:
        await message_or_cb.message.edit_text(text, reply_markup=kb)
    else:
        await message_or_cb.answer(text, reply_markup=kb)


@dp.message(Command("cd"))
@dp.message(Command("c"))
async def cd_cmd(message: Message):
    if not is_admin(message):
        return

    args = split_pipe_args(message)
    if not args:
        await message.answer(
            "Usage:\n"
            "/cd FolderName  (alias: /c)\n"
            "/cd Folder|Sub|...\n\n"
            "Use /back (/b) to go up one level, /exit (/e) to return to root."
        )
        return

    data = load_data()
    cwd = get_cwd(message.from_user.id)
    candidate = cwd + args

    canonical = get_canonical_path(data["folders"], candidate)
    if canonical is None:
        await message.answer(f"❌ Folder not found: {' > '.join(candidate)}")
        return

    set_cwd(message.from_user.id, canonical)
    record_recent(message.from_user.id, canonical)
    node = find_node_by_path(data["folders"], canonical)
    await message.answer(
        f"📂 Current Folder:\n{' > '.join(canonical)}",
        reply_markup=folder_nav_keyboard(data["folders"], canonical, node),
    )


@dp.message(Command("ls"))
@dp.message(Command("l"))
async def ls_cmd(message: Message):
    if not is_admin(message):
        return
    await render_ls(message, message.from_user.id)


@dp.message(Command("pwd"))
@dp.message(Command("p"))
async def pwd_cmd(message: Message):
    if not is_admin(message):
        return

    cwd = get_cwd(message.from_user.id)
    if cwd:
        await message.answer(f"📍 Current Folder:\n{' > '.join(cwd)}")
    else:
        await message.answer("📍 Current Folder: root")


@dp.message(Command("back"))
@dp.message(Command("b"))
async def back_cmd(message: Message):
    if not is_admin(message):
        return

    user_id = message.from_user.id
    cwd = get_cwd(user_id)
    if not cwd:
        await message.answer("Already at root — nowhere to go back to.")
        return

    new_cwd = cwd[:-1]
    set_cwd(user_id, new_cwd)
    if new_cwd:
        await message.answer(f"⬅️ Current Folder:\n{' > '.join(new_cwd)}")
    else:
        await message.answer("⬅️ Back to root/main menu.")


@dp.message(Command("exit"))
@dp.message(Command("e"))
async def exit_cmd(message: Message):
    if not is_admin(message):
        return

    clear_cwd(message.from_user.id)
    await message.answer("🏠 Returned to root/main menu.")


# ─── Folder-button navigation callbacks (tap instead of /cd) ────────────────
#
#   "nav\x1fChildName" -> /cd into that one child of the current folder.
#   "navp\x1fTOKEN"     -> /cd to a deeper path registered by /recent or
#                          /find (see register_path_token above).
#   "navhome"           -> same as /exit.
#   "navback"           -> same as /back.
#   These reuse the exact same admin_cwd state /cd, /back, /exit already
#   read and write, so mixing taps and typed commands in the same session
#   works seamlessly either way.

@dp.callback_query(F.data.startswith("nav" + CB_SEP))
async def nav_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    # folder_nav_keyboard encodes only the immediate child's name — the
    # rest of the path is whatever folder the admin is currently in.
    child_name = decode_cb_path(callback.data)
    user_id = callback.from_user.id
    cwd = get_cwd(user_id)
    candidate = cwd + child_name

    data = load_data()
    canonical = get_canonical_path(data["folders"], candidate)
    if canonical is None:
        await callback.answer("❌ Folder not found (it may have been deleted).", show_alert=True)
        return

    set_cwd(user_id, canonical)
    record_recent(user_id, canonical)
    await render_ls(callback, user_id, edit=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("navp" + CB_SEP))
async def navp_callback(callback: CallbackQuery):
    """Navigate using a short token registered by /recent or /find, which
    may point at a folder several levels deep that wouldn't fit directly
    in callback_data."""
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    user_id = callback.from_user.id
    token = decode_cb_path(callback.data)
    token = token[0] if token else None
    path = resolve_path_token(user_id, token) if token is not None else None
    if path is None:
        await callback.answer("❌ That result expired — run the search again.", show_alert=True)
        return

    data = load_data()
    canonical = get_canonical_path(data["folders"], path)
    if canonical is None:
        await callback.answer("❌ Folder not found (it may have been deleted).", show_alert=True)
        return

    set_cwd(user_id, canonical)
    record_recent(user_id, canonical)
    await render_ls(callback, user_id, edit=True)
    await callback.answer()


@dp.callback_query(F.data == "navhome")
async def navhome_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    clear_cwd(callback.from_user.id)
    await render_ls(callback, callback.from_user.id, edit=True)
    await callback.answer("🏠 Home")


@dp.callback_query(F.data == "navback")
async def navback_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    user_id = callback.from_user.id
    cwd = get_cwd(user_id)
    set_cwd(user_id, cwd[:-1])
    await render_ls(callback, user_id, edit=True)
    await callback.answer("⬅️ Back")


# ─── Folder-picker navigation for the Sort Order panel flow ─────────────────
#
#   Mirrors nav/navp/navhome/navback above exactly, except every step lands
#   back on render_sort_order (the 📊 Sort Order screen) instead of
#   render_ls — so picking a folder from /panel → Sort Order takes you
#   straight to setting that folder's order, no extra taps needed.

@dp.callback_query(F.data.startswith("srtnav" + CB_SEP))
async def navsort_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    child_name = decode_cb_path(callback.data)
    user_id = callback.from_user.id
    cwd = get_cwd(user_id)
    candidate = cwd + child_name

    data = load_data()
    canonical = get_canonical_path(data["folders"], candidate)
    if canonical is None:
        await callback.answer("❌ Folder not found (it may have been deleted).", show_alert=True)
        return

    set_cwd(user_id, canonical)
    record_recent(user_id, canonical)
    await render_sort_order(callback)
    await callback.answer()


@dp.callback_query(F.data == "navsorthome")
async def navsorthome_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    clear_cwd(callback.from_user.id)
    await render_sort_order(callback)
    await callback.answer("🏠 Home")


@dp.callback_query(F.data == "navsortback")
async def navsortback_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    user_id = callback.from_user.id
    cwd = get_cwd(user_id)
    set_cwd(user_id, cwd[:-1])
    await render_sort_order(callback)
    await callback.answer("⬅️ Back")


# ─── /find, /recent ───────────────────────────────────────────────────────────

@dp.message(Command("find"))
@dp.message(Command("f"))
async def find_cmd(message: Message):
    if not is_admin(message):
        return

    keyword = get_command_args(message)
    if not keyword:
        await message.answer("Usage:\n/find keyword  (alias: /f)\n\nExample:\n/find mock")
        return

    data = load_data()
    results = []
    search_tree(data["folders"], keyword, [], results)

    if not results:
        await message.answer(f"🔍 No matches for '{keyword}'.")
        return

    lines = [f"🔍 Results for '{keyword}':"]
    for kind, path, label in results[:40]:
        if kind == "folder":
            lines.append(f"📁 {' > '.join(path)}")
        else:
            lines.append(f"🔘 {label}  —  {' > '.join(path)}")
    if len(results) > 40:
        lines.append(f"\n…and {len(results) - 40} more matches.")

    await message.answer("\n".join(lines), reply_markup=find_results_keyboard(message.from_user.id, results))


@dp.message(Command("recent"))
@dp.message(Command("r"))
async def recent_cmd(message: Message):
    if not is_admin(message):
        return

    kb = recent_keyboard(message.from_user.id)
    if kb is None:
        await message.answer("No recently opened folders yet — use /cd or tap a folder button first.")
        return

    await message.answer("🕘 Recent folders (tap to open):", reply_markup=kb)


# ─── Delete-confirmation callbacks ───────────────────────────────────────────
#
#   Performs whichever delete /deletefolder, /deletesubfolder or
#   /deletebutton stashed via request_delete_confirmation(), using the
#   exact same tree-mutation helpers those commands always used
#   (delete_node, or the buttons-list filter for a button delete).

@dp.callback_query(F.data == "delyes")
async def delyes_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    pending = pending_delete.pop(callback.from_user.id, None)
    if pending is None:
        await callback.message.edit_text("Nothing pending to confirm.", reply_markup=None)
        await callback.answer()
        return

    kind = pending["kind"]
    path = pending["path"]
    data = load_data()

    if kind in ("folder", "subfolder"):
        ok, error = delete_node(data["folders"], path)
        if not ok:
            await callback.message.edit_text(f"❌ {error}", reply_markup=None)
            await callback.answer()
            return
        save_data(data)
        user_id = callback.from_user.id
        cwd = get_cwd(user_id)
        if cwd[:len(path)] == path:
            # The deleted folder was the current folder (or an ancestor of
            # it) — move the session back to its parent so /ls etc. don't
            # point at something that no longer exists.
            set_cwd(user_id, path[:-1])
            cwd = get_cwd(user_id)
        await callback.message.edit_text(
            f"🗑 Deleted '{path[-1]}' from '{' > '.join(path[:-1]) or 'root'}'" + location_footer(cwd),
            reply_markup=None,
        )

    elif kind == "button":
        node = find_node_by_path(data["folders"], path)
        title = pending["label_extra"]
        if node is None:
            await callback.message.edit_text(f"❌ Path not found: {' > '.join(path)}", reply_markup=None)
            await callback.answer()
            return
        buttons     = node.get("buttons", [])
        new_buttons = [b for b in buttons if b["title"].lower() != title.lower()]
        if len(new_buttons) == len(buttons):
            await callback.message.edit_text(f"❌ Button '{title}' not found", reply_markup=None)
            await callback.answer()
            return
        node["buttons"] = new_buttons
        save_data(data)
        await callback.message.edit_text(
            f"🗑 Deleted button '{title}' from '{' > '.join(path)}'"
            + location_footer(get_cwd(callback.from_user.id)),
            reply_markup=None,
        )

    elif kind == "link":
        node = find_node_by_path(data["folders"], path)
        if node is None or "link" not in node:
            await callback.message.edit_text(f"❌ No link set on '{' > '.join(path)}'", reply_markup=None)
            await callback.answer()
            return
        del node["link"]
        save_data(data)
        await callback.message.edit_text(
            f"🗑 Removed link from '{' > '.join(path)}'"
            + location_footer(get_cwd(callback.from_user.id)),
            reply_markup=None,
        )

    await callback.answer()


@dp.callback_query(F.data == "delno")
async def delno_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    pending_delete.pop(callback.from_user.id, None)
    await callback.message.edit_text("❌ Cancelled — nothing was deleted.", reply_markup=None)
    await callback.answer("Cancelled")


# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "ACHIEVER 8.0 Admin Bot\n\n"
        "── Quick access ──\n"
        "/panel   tappable dashboard for everything below\n\n"
        "── Draft -> Publish ──\n"
        "All edits below happen in DRAFT only — the live site is never\n"
        "touched until you run /publish.\n"
        "/initdraft   (one-time: create draft.json from live data.json)\n"
        "/syncdraft   (reset draft to match live, discarding unpublished edits)\n"
        "/drafttree   (preview the draft structure — not live yet)\n"
        "/publish     (push draft.json live as data.json)\n\n"
        "── Folder Navigation (saves typing) ──\n"
        "/cd FolderName        (enter a folder/subfolder)        alias: /c\n"
        "/cd Folder|Sub|...    (jump several levels at once)\n"
        "/ls                   (show folders/buttons here, tappable)  alias: /l\n"
        "/pwd                  (show current folder)              alias: /p\n"
        "/back                 (go up one level)                  alias: /b\n"
        "/exit                 (return to root/main menu)         alias: /e\n"
        "/find keyword         (search all folders & buttons)     alias: /f\n"
        "/recent               (recently opened folders)          alias: /r\n"
        "Folders shown by /ls, /cd, /find and /recent are tappable buttons —\n"
        "tap to enter instead of typing /cd. A 🏠 Home / ⬅️ Back row appears\n"
        "wherever you're not already at root.\n"
        "Once a folder is selected, drop its path from the commands\n"
        "below — e.g. inside SSC > Math, just:\n"
        "  /addbutton Mock Test|https://example.com\n"
        "  /setlink https://example.com\n"
        "  /deletebutton Mock Test\n"
        "Full old-style paths still work too, with or without /cd.\n\n"
        "── Folders (any depth) ──\n"
        "/listfolders\n"
        "/addfolder FolderName            (always root-level)\n"
        "/addsubfolder Folder|Sub\n"
        "/addsubfolder Folder|Sub1|Sub2|...\n"
        "/deletefolder FolderName         (always root-level, asks to confirm)\n"
        "/deletesubfolder Folder|Sub1|...|TargetSub   (asks to confirm)\n"
        "/renamefolder OldFolder|NewFolder   (always root-level)\n"
        "/renamesubfolder Folder|Sub1|...|OldName|NewName\n\n"
        "── Buttons (any level, unlimited per lecture) ──\n"
        "/addbutton Folder|Label|URL\n"
        "/addbutton Folder|Sub|Label|URL\n"
        "/addbutton Folder|Sub1|Sub2|...|Label|URL\n"
        "/deletebutton Folder|...|Label   (asks to confirm)\n"
        "/renamebutton Folder|...|OldLabel|NewLabel\n"
        "/listbuttons Folder\n"
        "/listbuttons Folder|Sub|...\n\n"
        "── Backup ──\n"
        "/backup    send current draft.json as a file\n"
        "/restore   reply to a backed-up .json file with /restore to load it back into draft\n"
        "/rollback  undo the last /publish (restores the previous live data.json)\n"
        "/backups   list recent automatic backups\n\n"
        "── Other ──\n"
        "/tree\n"
        "/setlink Folder|...|URL   (folder opens this URL directly, no buttons/navigation)\n"
        "/removelink Folder|...   (asks to confirm)\n"
        "/setsort Folder|...|newest   (or |oldest — sets that folder's default order on the website)\n"
        "/clearsort Folder|...   (resets that folder back to the default, newest-first)\n"
        "/migrate   (one-time: push old draft data into the new button format)\n"
        "/seticontype Folder|...|Label|type   (set explicit icon type on a button)\n"
        "  types: video, eng, hindi, quiz, pdf, extra\n\n"
        "── Analytics & Scheduling ──\n"
        "/stats   (top most-opened folders from visitor analytics)\n"
        "/publishat 2026-06-25T09:00:00Z   (schedule draft to go live at that UTC time)\n"
        "/diff   (compare draft vs live before publishing)"
    )


# ─── /panel — admin dashboard ────────────────────────────────────────────────
#
#   A tappable hub for the actions admins reach for most. Folders/Drafts/
#   Search/Backup are answered directly from here; Add Button/Set Link
#   just remind the admin of the exact command + current folder (since
#   those need typed text — a label and a URL — that can't be supplied by
#   a button tap), so they can copy-paste-edit instead of recalling syntax.

@dp.message(Command("panel"))
async def panel_cmd(message: Message):
    if not is_admin(message):
        return
    await message.answer("⚙ Admin Dashboard", reply_markup=panel_keyboard())


@dp.callback_query(F.data == "panel_folders")
async def panel_folders_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await render_ls(callback, callback.from_user.id, edit=True)
    await callback.answer()


@dp.callback_query(F.data == "panel_drafts")
async def panel_drafts_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    draft = load_data(DRAFT_FILE)
    try:
        live = load_data(DATA_FILE)
        published = count_buttons(live["folders"])
    except Exception:
        published = 0
    drafted = count_buttons(draft["folders"])

    await callback.message.edit_text(
        f"📦 Published: {published}\n📝 Drafts: {drafted}\n\n"
        "Counts are total buttons/content items across all folders.\n"
        "Run /publish to make the draft count match published.",
        reply_markup=panel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "panel_addbutton")
async def panel_addbutton_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    cwd = get_cwd(callback.from_user.id)
    hint = (
        "/addbutton Label|URL" if cwd else
        "/cd into a folder first, then:\n/addbutton Label|URL\n\n"
        "or directly:\n/addbutton Folder|Label|URL"
    )
    await callback.message.edit_text(
        f"➕ Add Button\n\n{hint}" + location_footer(cwd),
        reply_markup=panel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "panel_setlink")
async def panel_setlink_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    cwd = get_cwd(callback.from_user.id)
    hint = (
        "/setlink URL" if cwd else
        "/cd into a folder first, then:\n/setlink URL\n\n"
        "or directly:\n/setlink Folder|URL"
    )
    await callback.message.edit_text(
        f"🔗 Set Link\n\n{hint}" + location_footer(cwd),
        reply_markup=panel_keyboard(),
    )
    await callback.answer()


# ─── Panel → Sort Order ──────────────────────────────────────────────────────
#
#   Shows the CURRENT folder's (cwd) sort default and lets the admin tap
#   Newest / Oldest / Reset to default, instead of typing /setsort by
#   hand. The same screen also lists subfolders (tap to drill in, via the
#   srtnav callback prefix) so the admin can navigate to any depth and set
#   each folder's order without leaving this flow. At root (no cwd), only
#   the subfolder list is shown — there's no "root order" to set.

def sort_order_keyboard(folders: list, cwd: list, node: dict | None) -> InlineKeyboardMarkup:
    rows = []
    children = node.get("children", []) if node is not None else folders
    for child in children:
        rows.append([InlineKeyboardButton(
            text=f"📁 {child['name']}",
            callback_data=encode_cb_path("srtnav", [child["name"]]),
        )])

    if cwd:
        current = node.get("sort_order") if node else None
        newest_label = "✅ 🆕 Newest First" if (current or "newest") == "newest" else "🆕 Newest First"
        oldest_label = "✅ ⏳ Oldest First" if current == "oldest" else "⏳ Oldest First"
        rows.append([
            InlineKeyboardButton(text=newest_label, callback_data="sortorder_set" + CB_SEP + "newest"),
            InlineKeyboardButton(text=oldest_label, callback_data="sortorder_set" + CB_SEP + "oldest"),
        ])
        if current:
            rows.append([InlineKeyboardButton(text="↩️ Reset to default", callback_data="sortorder_clear")])
        rows.append([
            InlineKeyboardButton(text="⬅️ Back", callback_data="navsortback"),
            InlineKeyboardButton(text="🏠 Home", callback_data="navsorthome"),
        ])

    rows.append([InlineKeyboardButton(text="⬅️ Back to Panel", callback_data="panel_settings_noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_sort_order(callback: CallbackQuery):
    user_id = callback.from_user.id
    cwd = get_cwd(user_id)
    data = load_data()

    if not cwd:
        await callback.message.edit_text(
            "📊 Sort Order\n\n"
            "Pick a folder to set its default order on the website 👇",
            reply_markup=sort_order_keyboard(data["folders"], cwd, None),
        )
        return

    node = find_node_by_path(data["folders"], cwd)
    if node is None:
        clear_cwd(user_id)
        await callback.message.edit_text("⚠️ Current folder no longer exists — moved back to root.")
        return

    current = node.get("sort_order")
    status = f"currently: {current}-first" if current else "currently: default (newest-first)"
    await callback.message.edit_text(
        f"📊 Sort Order\n\n📂 {' > '.join(cwd)}\n{status}\n\n"
        "This sets the order visitors see by default on the website for this "
        "folder. They can still flip it themselves there, but it resets to "
        "this on their next visit. Tap a subfolder below to set its order "
        "instead.",
        reply_markup=sort_order_keyboard(data["folders"], cwd, node),
    )


@dp.callback_query(F.data == "panel_sortorder")
async def panel_sortorder_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await render_sort_order(callback)
    await callback.answer()


@dp.callback_query(F.data.startswith("sortorder_set" + CB_SEP))
async def sortorder_set_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    user_id = callback.from_user.id
    cwd = get_cwd(user_id)
    if not cwd:
        await callback.answer("❌ No folder selected.", show_alert=True)
        return

    order = decode_cb_path(callback.data)
    order = order[0] if order else None
    if order not in SORT_ORDERS:
        await callback.answer()
        return

    data = load_data()
    node = find_node_by_path(data["folders"], cwd)
    if node is None:
        clear_cwd(user_id)
        await callback.message.edit_text("⚠️ Current folder no longer exists — moved back to root.")
        await callback.answer()
        return

    node["sort_order"] = order
    save_data(data)
    await render_sort_order(callback)
    await callback.answer(f"Set to {order}-first")


@dp.callback_query(F.data == "sortorder_clear")
async def sortorder_clear_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return

    user_id = callback.from_user.id
    cwd = get_cwd(user_id)
    if not cwd:
        await callback.answer("❌ No folder selected.", show_alert=True)
        return

    data = load_data()
    node = find_node_by_path(data["folders"], cwd)
    if node is None:
        clear_cwd(user_id)
        await callback.message.edit_text("⚠️ Current folder no longer exists — moved back to root.")
        await callback.answer()
        return

    node.pop("sort_order", None)
    save_data(data)
    await render_sort_order(callback)
    await callback.answer("Reset to default")


@dp.callback_query(F.data == "panel_settings_noop")
async def panel_settings_noop_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text("⚙ Admin Dashboard", reply_markup=panel_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "panel_search")
async def panel_search_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "🔍 Search\n\nSend /find keyword to search every folder and button.\nExample: /find mock",
        reply_markup=panel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "panel_backup")
async def panel_backup_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await send_backup(callback.message)
    await callback.answer("📦 Backup sent")


@dp.callback_query(F.data == "panel_backups_list")
async def panel_backups_list_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await do_list_backups(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "panel_rollback")
async def panel_rollback_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await do_rollback(callback.message)
    await callback.answer("⏪ Rollback done")


@dp.callback_query(F.data == "panel_diff")
async def panel_diff_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    # Reuse diff_cmd logic by synthesizing a message-like call
    await callback.answer()
    try:
        draft = load_data(DRAFT_FILE)
        live = load_data(DATA_FILE)
    except Exception as e:
        await callback.message.answer(f"❌ Diff error: {e}")
        return
    added, removed, changed = _diff_trees(draft, live)
    if not added and not removed and not changed:
        await callback.message.answer("✅ Draft and live are identical — nothing to publish.")
        return
    lines = ["📋 Draft vs Live diff:\n"]
    if added:
        lines.append(f"➕ Added ({len(added)}): " + ", ".join(added[:8]))
    if removed:
        lines.append(f"➖ Removed ({len(removed)}): " + ", ".join(removed[:8]))
    if changed:
        lines.append(f"✏️ Changed ({len(changed)}): " + ", ".join(k for k, _ in changed[:8]))
    lines.append("\nRun /publish when ready.")
    await callback.message.answer("\n".join(lines))


@dp.callback_query(F.data == "panel_stats")
async def panel_stats_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    counts = _load_analytics()
    if not counts:
        await callback.message.answer("📊 No analytics data yet.")
        return
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = ["📊 Top opened folders:"]
    for i, (p, c) in enumerate(top, 1):
        lines.append(f"{i}. {p} — {c}")
    await callback.message.answer("\n".join(lines))


@dp.callback_query(F.data == "panel_settings")
async def panel_settings_callback(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        f"⚙ Settings\n\nAdmin ID: {ADMIN_ID}\n"
        "Draft/Publish, backup & restore are managed with /initdraft, /syncdraft, "
        "/publish, /backup and /restore.\n"
        "A snapshot of the live site is taken automatically on every /publish — "
        "use /rollback to undo the last publish, or /backups to see what's available.",
        reply_markup=panel_keyboard(),
    )
    await callback.answer()


# ─── Draft -> Publish workflow ───────────────────────────────────────────────
#
#   data.json  = what the live website actually fetches and shows to users.
#   draft.json = the working copy every edit command (add/delete/rename
#                folder, addbutton, setlink, etc.) reads and writes.
#
#   Nothing you do with those commands is visible on the live site until
#   you explicitly run /publish. This lets you make as many changes as you
#   want in one sitting and check them with /drafttree before they ever go
#   live.

@dp.message(Command("initdraft"))
async def initdraft_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data(DATA_FILE)     # also runs migration in memory (button format + created_at backfill)
    save_data(data, DRAFT_FILE)     # creates draft.json if it doesn't exist yet, or overwrites it
    await message.answer(
        "✅ draft.json created from live data.json.\n"
        "Edit freely with the usual commands — the live site is untouched until /publish."
    )


@dp.message(Command("syncdraft"))
async def syncdraft_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data(DATA_FILE)
    save_data(data, DRAFT_FILE)
    await message.answer(
        "🔄 draft.json reset to match live data.json.\n"
        "Any unpublished draft edits were discarded."
    )


@dp.message(Command("drafttree"))
async def drafttree_cmd(message: Message):
    if not is_admin(message):
        return

    data    = load_data(DRAFT_FILE)
    folders = data["folders"]

    if not folders:
        await message.answer("Draft is empty. Run /initdraft if you haven't yet.")
        return

    text = "\n".join(render_tree(folders))
    await message.answer(f"📂 DRAFT tree (NOT live yet):\n{text}")


@dp.message(Command("publish"))
async def publish_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data(DRAFT_FILE)

    # Snapshot whatever is currently live BEFORE overwriting it, so
    # /rollback always has something to restore. Best-effort: if there's
    # no live data.json yet (e.g. very first publish ever), there's just
    # nothing to back up, which is fine.
    try:
        previous_live = load_data(DATA_FILE)
        backup_name = _write_backup("data", previous_live)
    except Exception:
        backup_name = None

    save_data(data, DATA_FILE)

    lazy_ok, lazy_err = publish_lazy_files(data)

    # Trigger Vercel deploy
    vercel_hook = os.getenv("VERCEL_DEPLOY_HOOK")
    vercel_ok = True
    if vercel_hook:
        try:
            r = requests.post(vercel_hook, timeout=10)
            vercel_ok = r.ok
        except Exception:
            vercel_ok = False

    lines = [
        "🚀 Published! draft.json is now live as data.json.",
        "The website will update automatically once Vercel finishes redeploying (usually under a minute).",
    ]
    if backup_name:
        lines.append("🧷 Previous live version backed up — run /rollback to undo this publish if needed.")
    if not lazy_ok:
        lines.append(f"⚠️ Lazy-load shell/chunks update failed ({lazy_err}) — site still works fine, it just falls back to the full data.json.")
    if not vercel_ok:
        lines.append("⚠️ Vercel deploy trigger failed — redeploy manually from Vercel dashboard.")
    await message.answer("\n".join(lines))


# ─── Lazy-load shell/chunks (performance) ────────────────────────────────────
#
#   The site's data.json keeps growing as more content gets added, so
#   index.html no longer downloads it whole on every visit. Instead, on
#   every /publish we ALSO derive and push:
#     - shell.json   — every top-level folder, but with each one's full
#                       subtree stripped out and replaced by a "_chunk"
#                       pointer + "_count" (so the badge number is still
#                       right before that folder is ever opened).
#     - chunks/N.json — that folder's real subtree (N = its index in
#                       data.json's top-level "folders" array).
#   index.html fetches shell.json first (small), then a chunk only once a
#   visitor actually opens that top-level folder.
#
#   This is purely an extra artifact for the website — data.json/draft.json
#   and every existing bot command are completely unaffected, and the site
#   still works even if this step is skipped or fails (it just falls back
#   to fetching the full data.json, exactly like before this feature).
#
#   Note: top-level folders that get removed/reordered leave their old
#   chunks/N.json file behind, unreferenced by the new shell.json. That's
#   harmless (the site never fetches a chunk nothing points to) — just a
#   few unused files sitting in the repo, not worth the extra GitHub API
#   calls it'd take to clean them up here.

SHELL_FILE = "shell.json"
CHUNKS_DIR = "chunks"


def build_lazy_payload(data):
    """
    Pure function — does not touch the network or mutate `data`.
    Returns (shell: dict, chunks: {filename: payload}).
    """
    folders = data.get("folders", [])
    shell_folders = []
    chunks = {}

    for i, folder in enumerate(folders):
        is_direct_link = bool(folder.get("link") and str(folder["link"]).strip())
        children = folder.get("children") or []

        if is_direct_link or not children:
            # Nothing heavy to defer — direct-link folders never show a
            # children list, and a folder with no children is already as
            # light as it'll get.
            shell_folders.append(folder)
            continue

        shell_entry = dict(folder)
        shell_entry["children"] = []
        shell_entry["_chunk"] = f"{CHUNKS_DIR}/{i}.json"
        shell_entry["_count"] = len(children)
        shell_folders.append(shell_entry)
        chunks[f"{CHUNKS_DIR}/{i}.json"] = {"children": children}

    return {"folders": shell_folders, "generated_at": now_iso()}, chunks


def publish_lazy_files(data):
    """
    Best-effort: write shell.json + chunks/*.json for the website to
    lazy-load in a SINGLE GitHub commit (using the Git Tree API).
    This prevents multiple Vercel deployments from being triggered.
    Never raises. Returns (ok: bool, error: str | None).
    """
    try:
        shell, chunks = build_lazy_payload(data)

        github_token = os.getenv("GITHUB_TOKEN")
        github_user  = os.getenv("GITHUB_USERNAME")
        github_repo  = os.getenv("GITHUB_REPO")
        headers = _github_headers()
        repo_api = f"https://api.github.com/repos/{github_user}/{github_repo}"

        # --- Build all file contents ---
        all_files = {SHELL_FILE: shell}
        all_files.update(chunks)

        # --- Get current branch HEAD commit SHA ---
        branch = "main"
        ref_resp = requests.get(f"{repo_api}/git/ref/heads/{branch}", headers=headers)
        ref_resp.raise_for_status()
        base_commit_sha = ref_resp.json()["object"]["sha"]

        # --- Get the base tree SHA ---
        commit_resp = requests.get(f"{repo_api}/git/commits/{base_commit_sha}", headers=headers)
        commit_resp.raise_for_status()
        base_tree_sha = commit_resp.json()["tree"]["sha"]

        # --- Create new tree with all files ---
        tree_items = []
        for fname, fdata in all_files.items():
            content_str = json.dumps(fdata, separators=(",", ":"), ensure_ascii=False)
            tree_items.append({
                "path": fname,
                "mode": "100644",
                "type": "blob",
                "content": content_str,
            })

        tree_resp = requests.post(
            f"{repo_api}/git/trees",
            headers=headers,
            json={"base_tree": base_tree_sha, "tree": tree_items},
        )
        tree_resp.raise_for_status()
        new_tree_sha = tree_resp.json()["sha"]

        # --- Create commit ---
        commit_resp2 = requests.post(
            f"{repo_api}/git/commits",
            headers=headers,
            json={
                "message": "Update shell.json + chunks (lazy-load) from bot",
                "tree": new_tree_sha,
                "parents": [base_commit_sha],
            },
        )
        commit_resp2.raise_for_status()
        new_commit_sha = commit_resp2.json()["sha"]

        # --- Update branch ref ---
        patch_resp = requests.patch(
            f"{repo_api}/git/refs/heads/{branch}",
            headers=headers,
            json={"sha": new_commit_sha},
        )
        patch_resp.raise_for_status()

        return True, None
    except Exception as e:
        return False, str(e)


# ─── /addfolder (root level) ───────────────────────────────────────────────────

@dp.message(Command("addfolder"))
async def add_folder(message: Message):
    if not is_admin(message):
        return

    folder_name = get_command_args(message)
    if not folder_name:
        await message.answer("Usage:\n/addfolder FolderName")
        return

    data = load_data()
    ok, error = add_node(data["folders"], [], folder_name)
    if not ok:
        await message.answer(f"❌ {error}")
        return

    save_data(data)
    await message.answer(f"✅ Folder added: {folder_name}")


# ─── /listfolders ─────────────────────────────────────────────────────────────

@dp.message(Command("listfolders"))
async def list_folders(message: Message):
    if not is_admin(message):
        return

    data    = load_data()
    folders = data["folders"]

    if not folders:
        await message.answer("No folders found.")
        return

    text = "\n".join(f"{i+1}. {f['name']}" for i, f in enumerate(folders))
    await message.answer(text)


# ─── /deletefolder (root level) ────────────────────────────────────────────────

@dp.message(Command("deletefolder"))
async def delete_folder(message: Message):
    if not is_admin(message):
        return

    folder_name = get_command_args(message)
    if not folder_name:
        await message.answer("Usage:\n/deletefolder FolderName")
        return

    data = load_data()
    node = find_node_by_path(data["folders"], [folder_name])
    if node is None:
        await message.answer(f"❌ '{folder_name}' not found")
        return

    await request_delete_confirmation(
        message, kind="folder", path=[folder_name],
        label=f"folder '{folder_name}'",
    )


# ─── /renamefolder (root level) ────────────────────────────────────────────────

@dp.message(Command("renamefolder"))
async def rename_folder(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    if len(parts) < 2:
        await message.answer("Usage:\n/renamefolder OldFolder|NewFolder")
        return

    old_name, new_name = parts[0], parts[1]
    data = load_data()
    ok, error = rename_node(data["folders"], [old_name], new_name)
    if not ok:
        await message.answer(f"❌ {error}")
        return

    save_data(data)
    await message.answer(f"✅ Renamed '{old_name}' to '{new_name}'")


# ─── /addsubfolder (unlimited depth) ───────────────────────────────────────────
#
#   /addsubfolder Maths|Algebra
#   /addsubfolder Maths|Algebra|Quadratic
#   /addsubfolder Maths|Algebra|Quadratic|Chapter1
#
#   All parts except the last form the path to the parent node.
#   The last part is the new subfolder name.

@dp.message(Command("addsubfolder"))
async def add_subfolder(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/addsubfolder ParentFolder|SubFolder\n"
            "/addsubfolder Folder|Sub1|Sub2|NewSub\n\n"
            "Or /cd into a folder first, then just:\n"
            "/addsubfolder NewSub"
        )
        return
    if not parts:
        await message.answer("Usage:\n/addsubfolder NewSub")
        return

    typed_parent_path = parts[:-1]
    new_name          = parts[-1]

    data = load_data()
    parent_path = resolve_path(data["folders"], user_id, typed_parent_path)

    ok, error = add_node(data["folders"], parent_path, new_name)
    if not ok:
        await message.answer(f"❌ {error}")
        return

    save_data(data)
    await message.answer(
        f"✅ Added '{new_name}' inside '{' > '.join(parent_path)}'"
        + location_footer(get_cwd(user_id))
    )


# ─── /deletesubfolder (unlimited depth) ────────────────────────────────────────
#
#   /deletesubfolder Maths|Algebra
#   /deletesubfolder Maths|Algebra|Quadratic

@dp.message(Command("deletesubfolder"))
async def delete_subfolder(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/deletesubfolder ParentFolder|SubFolder\n"
            "/deletesubfolder Folder|Sub1|Sub2|TargetSub\n\n"
            "Or /cd into the parent folder first, then just:\n"
            "/deletesubfolder TargetSub"
        )
        return
    if not parts:
        await message.answer("Usage:\n/deletesubfolder TargetSub")
        return

    data = load_data()
    full_path = resolve_path(data["folders"], user_id, parts)

    node = find_node_by_path(data["folders"], full_path)
    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(full_path)}")
        return

    await request_delete_confirmation(
        message, kind="subfolder", path=full_path,
        label=f"'{full_path[-1]}' from '{' > '.join(full_path[:-1])}'",
    )


# ─── /renamesubfolder (unlimited depth) ────────────────────────────────────────
#
#   /renamesubfolder Grammar|Tenses|All Tenses
#   /renamesubfolder Maths|Algebra|Quadratic|Chapter1|Unit One
#
#   All parts except the last two form the path to the parent.
#   Second-to-last = old name, last = new name.

@dp.message(Command("renamesubfolder"))
async def rename_subfolder(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and len(parts) < 3:
        await message.answer(
            "Usage:\n"
            "/renamesubfolder ParentFolder|OldSubfolder|NewSubfolder\n"
            "/renamesubfolder Folder|Sub1|...|OldName|NewName\n\n"
            "Or /cd into the parent folder first, then just:\n"
            "/renamesubfolder OldName|NewName"
        )
        return
    if len(parts) < 2:
        await message.answer("Usage:\n/renamesubfolder OldName|NewName")
        return

    typed_full_path = parts[:-1]   # ends in the current name being renamed
    new_name        = parts[-1]

    data = load_data()
    full_path = resolve_path(data["folders"], user_id, typed_full_path)

    ok, error = rename_node(data["folders"], full_path, new_name)
    if not ok:
        await message.answer(f"❌ {error}")
        return

    save_data(data)
    await message.answer(
        f"✅ Renamed '{full_path[-1]}' to '{new_name}' inside '{' > '.join(full_path[:-1])}'"
    )


# ─── /addbutton (any depth, unlimited per node) ─────────────────────────────
#
#   Formats accepted (same shape the old /addlink used):
#     /addbutton Folder|URL                     (2 parts → label = URL)
#     /addbutton Folder|Label|URL               (3 parts)
#     /addbutton Folder|Sub|Label|URL           (4 parts)
#     /addbutton Folder|Sub1|Sub2|...|Label|URL (N parts, last 2 = label+url)
#
#   /addlink is kept as an alias so anything you already had saved keeps working.

@dp.message(Command("addbutton"))
@dp.message(Command("addlink"))
async def add_button(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not parts or (not cwd and len(parts) < 2):
        await message.answer(
            "Usage:\n"
            "/addbutton Folder|URL\n"
            "/addbutton Folder|Label|URL\n"
            "/addbutton Folder|Sub|Label|URL\n"
            "/addbutton Folder|Sub1|Sub2|...|Label|URL\n\n"
            "Or /cd into a folder first, then just:\n"
            "/addbutton URL\n"
            "/addbutton Label|URL"
        )
        return

    if cwd:
        # While a folder is selected, a single part is just the URL (label
        # = URL), and 2+ parts are always Label|URL relative to that
        # folder — the old "2 parts = Folder|URL" shorthand below only
        # applies when no folder is selected, since the folder is implied.
        if len(parts) == 1:
            typed_path, title, url = [], parts[0], parts[0]
        else:
            typed_path, title, url = parts[:-2], parts[-2], parts[-1]
    else:
        if len(parts) == 2:
            # /addbutton Folder|URL — add directly to a root folder, label = URL
            node_path = [parts[0]]
            title     = parts[1]
            url       = parts[1]
        else:
            # last part = URL, second-to-last = label, everything before = path
            node_path = parts[:-2]
            title     = parts[-2]
            url       = parts[-1]

    data = load_data()
    if cwd:
        node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    ensure_node_fields(node)

    # Without this, two buttons named "Video" on the same lecture would make
    # /deletebutton and /renamebutton ambiguous about which one to target.
    if any(b["title"].lower() == title.lower() for b in node["buttons"]):
        await message.answer(f"❌ A button called '{title}' already exists there")
        return

    node["buttons"].append({"title": title, "url": url})
    save_data(data)
    await message.answer(
        f"✅ Added button '{title}' to '{' > '.join(node_path)}'"
        + location_footer(cwd)
    )


# ─── /deletebutton (any depth) ───────────────────────────────────────────────
#
#   /deletebutton Folder|Label
#   /deletebutton Folder|Sub|Label
#   /deletebutton Folder|Sub1|Sub2|...|Label
#
#   All parts except the last form the path; last part = button label.
#   /deletelink is kept as an alias.

@dp.message(Command("deletebutton"))
@dp.message(Command("deletelink"))
async def delete_button(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/deletebutton Folder|Label\n"
            "/deletebutton Folder|Sub|Label\n"
            "/deletebutton Folder|Sub1|Sub2|...|Label\n\n"
            "Or /cd into a folder first, then just:\n"
            "/deletebutton Label"
        )
        return
    if not parts:
        await message.answer("Usage:\n/deletebutton Label")
        return

    typed_path = parts[:-1]
    title      = parts[-1]

    data = load_data()
    node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    buttons = node.get("buttons", [])
    if not any(b["title"].lower() == title.lower() for b in buttons):
        await message.answer(f"❌ Button '{title}' not found")
        return

    await request_delete_confirmation(
        message, kind="button", path=node_path, label_extra=title,
        label=f"button '{title}' from '{' > '.join(node_path)}'",
    )


# ─── /renamebutton (any depth) — new command, no old equivalent existed ─────
#
#   /renamebutton Folder|OldLabel|NewLabel
#   /renamebutton Folder|Sub|...|OldLabel|NewLabel
#
#   All parts except the last two form the path; second-to-last = old
#   label, last = new label.

@dp.message(Command("renamebutton"))
async def rename_button(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and len(parts) < 3:
        await message.answer(
            "Usage:\n"
            "/renamebutton Folder|OldLabel|NewLabel\n"
            "/renamebutton Folder|Sub|...|OldLabel|NewLabel\n\n"
            "Or /cd into a folder first, then just:\n"
            "/renamebutton OldLabel|NewLabel"
        )
        return
    if len(parts) < 2:
        await message.answer("Usage:\n/renamebutton OldLabel|NewLabel")
        return

    typed_path = parts[:-2]
    old_title, new_title = parts[-2], parts[-1]

    data = load_data()
    node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    buttons = node.get("buttons", [])
    target  = next((b for b in buttons if b["title"].lower() == old_title.lower()), None)

    if target is None:
        await message.answer(f"❌ Button '{old_title}' not found")
        return

    if any(b is not target and b["title"].lower() == new_title.lower() for b in buttons):
        await message.answer(f"❌ A button called '{new_title}' already exists there")
        return

    target["title"] = new_title
    save_data(data)

    await message.answer(
        f"✏️ Renamed button '{old_title}' → '{new_title}' in '{' > '.join(node_path)}'"
    )


# ─── /listbuttons (any depth, read-only) ─────────────────────────────────────
#
#   /listbuttons Folder
#   /listbuttons Folder|Sub|...
#
#   Shows every button currently attached to the target folder/subfolder.
#   Referenced in the /start help menu but had no handler — added here.

@dp.message(Command("listbuttons"))
async def list_buttons(message: Message):
    if not is_admin(message):
        return

    typed_path = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and not typed_path:
        await message.answer(
            "Usage:\n"
            "/listbuttons Folder\n"
            "/listbuttons Folder|Sub|...\n\n"
            "Or /cd into a folder first, then just:\n"
            "/listbuttons"
        )
        return

    data = load_data()
    node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    buttons = node.get("buttons", [])
    if not buttons:
        await message.answer(f"No buttons found in '{' > '.join(node_path)}'")
        return

    text = "\n".join(f"{i+1}. {b['title']} → {b['url']}" for i, b in enumerate(buttons))
    await message.answer(f"Buttons in '{' > '.join(node_path)}':\n{text}")


# ─── /tree (read-only) ────────────────────────────────────────────────────────
#
#   Prints the entire folder structure, nested by indentation, showing how
#   many buttons each node has (or that it has a direct link via /setlink).
#   Referenced in the /start help menu but had no handler — added here.

def render_tree(nodes, depth=0):
    lines = []
    for node in nodes:
        prefix = "  " * depth + "- "
        if node.get("link"):
            suffix = " 🔗 (direct link)"
        else:
            button_count = len(node.get("buttons", []))
            suffix = f" ({button_count} button{'s' if button_count != 1 else ''})" if button_count else ""
        lines.append(f"{prefix}{node['name']}{suffix}")
        children = node.get("children", [])
        if children:
            lines.extend(render_tree(children, depth + 1))
    return lines


@dp.message(Command("tree"))
async def tree_cmd(message: Message):
    if not is_admin(message):
        return

    data    = load_data()
    folders = data["folders"]

    if not folders:
        await message.answer("No folders found.")
        return

    text = "\n".join(render_tree(folders))
    await message.answer(f"📂 Folder Tree:\n{text}")


# ─── /setlink (any depth) ────────────────────────────────────────────────────
#
#   /setlink Folder|URL
#   /setlink Folder|Sub|...|URL
#
#   Marks a folder/subfolder as a direct link: instead of navigating into
#   its buttons/children, it should open this URL directly. This is
#   storage-only — actually enforcing that behaviour is the job of the
#   user-facing bot that reads data.json, not this admin bot.
#   Referenced in the /start help menu but had no handler — added here.

@dp.message(Command("setlink"))
async def set_link(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/setlink Folder|URL\n"
            "/setlink Folder|Sub|...|URL\n\n"
            "Or /cd into a folder first, then just:\n"
            "/setlink URL"
        )
        return
    if not parts:
        await message.answer("Usage:\n/setlink URL")
        return

    typed_path = parts[:-1]
    url        = parts[-1]

    data = load_data()
    node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    node["link"] = url
    save_data(data)
    await message.answer(
        f"🔗 Link set on '{' > '.join(node_path)}': {url}"
        + location_footer(get_cwd(user_id))
    )


# ─── /removelink (any depth) ─────────────────────────────────────────────────
#
#   /removelink Folder
#   /removelink Folder|Sub|...
#
#   Removes a direct link set by /setlink, restoring normal buttons/
#   navigation behaviour for that folder/subfolder.
#   Referenced in the /start help menu but had no handler — added here.

@dp.message(Command("removelink"))
async def remove_link(message: Message):
    if not is_admin(message):
        return

    typed_path = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and not typed_path:
        await message.answer(
            "Usage:\n"
            "/removelink Folder\n"
            "/removelink Folder|Sub|...\n\n"
            "Or /cd into a folder first, then just:\n"
            "/removelink"
        )
        return

    data = load_data()
    node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    if "link" not in node:
        await message.answer(f"❌ No link set on '{' > '.join(node_path)}'")
        return

    await request_delete_confirmation(
        message, kind="link", path=node_path,
        label=f"link on '{' > '.join(node_path)}' ({node['link']})",
    )


# ─── /setsort & /clearsort (any depth) ───────────────────────────────────────
#
#   /setsort Folder|newest
#   /setsort Folder|Sub|...|oldest
#
#   Sets which order (newest-first or oldest-first) the website shows that
#   folder's contents in BY DEFAULT. This is just the starting point a
#   visitor sees — the sort toggle button on the website (shown from the
#   3rd level down) still lets a visitor flip it for their own viewing;
#   it just snaps back to whatever's set here on their next visit/reload.
#   Folders with no sort_order set behave exactly as before: newest-first.

SORT_ORDERS = ("newest", "oldest")


@dp.message(Command("setsort"))
async def set_sort(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/setsort Folder|newest\n"
            "/setsort Folder|Sub|...|oldest\n\n"
            "Or /cd into a folder first, then just:\n"
            "/setsort newest"
        )
        return
    if not parts:
        await message.answer("Usage:\n/setsort newest   (or oldest)")
        return

    typed_path = parts[:-1]
    order      = parts[-1].strip().lower()

    if order not in SORT_ORDERS:
        await message.answer("❌ Order must be 'newest' or 'oldest'.")
        return

    data = load_data()
    node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    node["sort_order"] = order
    save_data(data)
    icon = "🆕" if order == "newest" else "⏳"
    await message.answer(
        f"{icon} Default sort on '{' > '.join(node_path)}' set to {order}-first."
        + location_footer(get_cwd(user_id))
    )


@dp.message(Command("clearsort"))
async def clear_sort(message: Message):
    if not is_admin(message):
        return

    typed_path = split_pipe_args(message)
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if not cwd and not typed_path:
        await message.answer(
            "Usage:\n"
            "/clearsort Folder\n"
            "/clearsort Folder|Sub|...\n\n"
            "Or /cd into a folder first, then just:\n"
            "/clearsort"
        )
        return

    data = load_data()
    node_path = resolve_path(data["folders"], user_id, typed_path)
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    if "sort_order" not in node:
        await message.answer(f"❌ No sort order set on '{' > '.join(node_path)}' (already default: newest).")
        return

    node.pop("sort_order", None)
    save_data(data)
    await message.answer(
        f"🆕 Sort order on '{' > '.join(node_path)}' reset to default (newest-first)."
        + location_footer(get_cwd(user_id))
    )


# ─── /migrate (one-time push; also happens automatically on every load) ─────
#
#   load_data() already upgrades old fields/links into the new "buttons"
#   format in memory every time it's called (see migrate_data above). This
#   command just forces that upgraded shape to be written back to GitHub
#   immediately, instead of waiting for the next admin command that happens
#   to call save_data().
#   Referenced in the /start help menu but had no handler — added here.

@dp.message(Command("migrate"))
async def migrate_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data()
    save_data(data)
    await message.answer("✅ Migration complete — draft.json pushed to GitHub in the new button format. Run /publish when ready to make it live.")


# ─── /backup, /restore ───────────────────────────────────────────────────────
#
#   /backup sends draft.json (the working copy) as a downloadable file —
#   this never touches the live data.json.
#   /restore reads a .json file the admin replies to (or attaches in the
#   same message) and writes it into draft.json via the normal save_data()
#   path, so it goes through the same GitHub-write logic as every other
#   edit command. The live site is still untouched until /publish.

async def send_backup(message: Message):
    data = load_data(DRAFT_FILE)
    content = json.dumps(data, indent=2).encode("utf-8")
    filename = f"draft_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    await message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption="📦 Draft backup. Reply to this file with /restore to load it back in.",
    )


@dp.message(Command("backup"))
async def backup_cmd(message: Message):
    if not is_admin(message):
        return
    await send_backup(message)


@dp.message(Command("restore"))
async def restore_cmd(message: Message):
    if not is_admin(message):
        return

    doc = None
    if message.document:
        doc = message.document
    elif message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document

    if doc is None:
        await message.answer(
            "Usage: send a .json backup file with the caption /restore, "
            "or reply to a backup file with /restore."
        )
        return

    if not doc.file_name.lower().endswith(".json"):
        await message.answer("❌ That doesn't look like a .json backup file.")
        return

    file = await bot.get_file(doc.file_id)
    buf = BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    try:
        data = json.loads(buf.getvalue().decode("utf-8"))
    except Exception as e:
        await message.answer(f"❌ Couldn't parse that file as JSON: {e}")
        return

    if "folders" not in data:
        await message.answer("❌ That file doesn't look like a valid backup (missing 'folders').")
        return

    migrate_data(data)
    save_data(data, DRAFT_FILE)
    await message.answer(
        "✅ Restored into draft.json. Check it with /drafttree, then /publish when ready."
    )


# ─── Backup snapshots (used by /publish, /rollback, and the periodic loop) ──
#
#   Every snapshot lands in backups/<prefix>_<timestamp>.json on GitHub.
#   "data_*"      — taken automatically right before every /publish and
#                   /rollback overwrites the live data.json. This is what
#                   /rollback restores from.
#   "draft_auto_*" — taken by the best-effort periodic loop below, as an
#                   extra safety net for draft edits between publishes.

BACKUPS_DIR = "backups"


def _backup_filename(prefix):
    return f"{BACKUPS_DIR}/{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"


def _write_backup(prefix, data):
    """Best-effort snapshot write — swallows errors so a failed backup
    never blocks the action that triggered it (e.g. /publish)."""
    try:
        fname = _backup_filename(prefix)
        save_data(data, fname)
        return fname
    except Exception:
        return None


def _list_backups(prefix=None, limit=20):
    """List backup filenames (not full paths) under backups/, oldest→newest."""
    try:
        resp = requests.get(_github_url(BACKUPS_DIR), headers=_github_headers()).json()
    except Exception:
        return []
    if not isinstance(resp, list):
        return []  # backups/ doesn't exist yet, or the repo is unreachable
    names = [it["name"] for it in resp if isinstance(it, dict) and it.get("name", "").endswith(".json")]
    if prefix:
        names = [n for n in names if n.startswith(prefix + "_")]
    names.sort()  # zero-padded timestamps in the filename sort chronologically as strings
    return names[-limit:]


# ─── /rollback, /backups ─────────────────────────────────────────────────────
#
#   /rollback undoes the most recent /publish by restoring data.json from
#   its automatic pre-publish backup. It also snapshots whatever's live
#   right before overwriting it — so running /rollback a second time in a
#   row swaps right back to what you just rolled back from (a simple
#   undo/redo, without needing separate logic for it).

async def do_rollback(message: Message):
    backups = _list_backups(prefix="data")
    if not backups:
        await message.answer(
            "❌ No backups found to roll back to yet — this becomes available "
            "after your next /publish."
        )
        return

    latest = backups[-1]
    try:
        restore_data = load_data(f"{BACKUPS_DIR}/{latest}")
    except Exception as e:
        await message.answer(f"❌ Couldn't read backup '{latest}': {e}")
        return

    try:
        current_live = load_data(DATA_FILE)
        _write_backup("data", current_live)
    except Exception:
        pass  # nothing live yet to snapshot — fine, proceed with the restore anyway

    save_data(restore_data, DATA_FILE)
    lazy_ok, lazy_err = publish_lazy_files(restore_data)

    lines = [
        f"⏪ Rolled back data.json to the backup from {latest}.",
        "The live site will update once Vercel redeploys.",
    ]
    if not lazy_ok:
        lines.append(f"⚠️ Lazy-load shell/chunks update failed ({lazy_err}) — site still works via the full data.json fallback.")
    await message.answer("\n".join(lines))


@dp.message(Command("rollback"))
async def rollback_cmd(message: Message):
    if not is_admin(message):
        return
    await do_rollback(message)


async def do_list_backups(message: Message):
    names = _list_backups(prefix="data", limit=10)
    if not names:
        await message.answer("No backups yet — they're created automatically on every /publish or /rollback.")
        return
    lines = ["🧷 Last live backups (newest last):"]
    lines += [f"• {n}" for n in names]
    lines.append("\nRun /rollback to restore the most recent one.")
    await message.answer("\n".join(lines))


@dp.message(Command("backups"))
async def backups_cmd(message: Message):
    if not is_admin(message):
        return
    await do_list_backups(message)


# ─── Best-effort periodic backup loop ────────────────────────────────────────
#
#   Backs up draft.json automatically every BACKUP_INTERVAL_HOURS while this
#   process is running, on top of the always-reliable, action-triggered
#   backup taken on every /publish above.
#
#   Honest caveat: on Render's free tier, a "Web Service" (this bot,
#   including this very loop) gets fully suspended after ~15 minutes with
#   no inbound HTTP request to its port, and only resumes once something
#   hits that port again — so this is "best-effort", not a guaranteed
#   wall-clock cron. It self-heals (it checks how overdue it is and runs
#   immediately if so, every time the process happens to be awake), but for
#   truly on-schedule backups, point a free uptime pinger (e.g. UptimeRobot,
#   cron-job.org) at this bot's "/" URL every 5–10 minutes — that also keeps
#   the bot itself responsive instead of asleep when an admin messages it.

BACKUP_INTERVAL_HOURS = 12
BACKUP_STATE_FILE = "backup_state.json"


def _load_backup_state():
    """Dedicated raw GitHub read for BACKUP_STATE_FILE — deliberately NOT
    load_data(), which assumes a {"folders": [...]} tree shape (same reason
    cwd_state.json has its own read/write below)."""
    try:
        response = requests.get(_github_url(BACKUP_STATE_FILE), headers=_github_headers()).json()
        if "content" not in response:
            return {}
        content = base64.b64decode(response["content"]).decode("utf-8")
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_backup_state(state):
    try:
        url = _github_url(BACKUP_STATE_FILE)
        headers = _github_headers()
        current = requests.get(url, headers=headers).json()
        sha = current.get("sha")
        content = json.dumps(state)
        encoded = base64.b64encode(content.encode()).decode()
        payload = {"message": "Update backup_state.json from bot", "content": encoded}
        if sha:
            payload["sha"] = sha
        requests.put(url, headers=headers, json=payload)
    except Exception:
        pass  # best-effort — worst case we just back up again next check


async def periodic_backup_loop():
    while True:
        try:
            state = _load_backup_state()
            last_str = state.get("last_backup_at")
            last_dt = None
            if last_str:
                try:
                    last_dt = datetime.strptime(last_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except Exception:
                    last_dt = None

            overdue = (last_dt is None) or (
                datetime.now(timezone.utc) - last_dt >= timedelta(hours=BACKUP_INTERVAL_HOURS)
            )
            if overdue:
                try:
                    draft = load_data(DRAFT_FILE)
                    _write_backup("draft_auto", draft)
                except Exception:
                    pass  # e.g. draft.json doesn't exist yet — nothing to back up
                _save_backup_state({"last_backup_at": now_iso()})
 
