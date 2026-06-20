import json
import requests
import base64
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import asyncio
import os
from flask import Flask
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

# Admin ID can now be overridden via env var (ADMIN_ID). Falls back to the
# original hardcoded value so existing deployments keep working unchanged.
ADMIN_ID = int(os.getenv("ADMIN_ID", "8837854952"))


def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


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
#   This is purely an in-memory admin-session convenience layered on TOP of
#   the existing path-based commands — none of those were rewritten to work
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
#   ever more than one person in future. It resets whenever the bot process
#   restarts — that's expected, the same way Telegram's own chat state does
#   for a fresh bot session.

admin_cwd: dict = {}   # user_id -> list of folder names (the path), [] = root


def get_cwd(user_id: int) -> list:
    return list(admin_cwd.get(user_id, []))


def set_cwd(user_id: int, path: list):
    admin_cwd[user_id] = list(path)


def clear_cwd(user_id: int):
    admin_cwd.pop(user_id, None)


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

@dp.message(Command("cd"))
async def cd_cmd(message: Message):
    if not is_admin(message):
        return

    args = split_pipe_args(message)
    if not args:
        await message.answer(
            "Usage:\n"
            "/cd FolderName\n"
            "/cd Folder|Sub|...\n\n"
            "Use /back to go up one level, /exit to return to root."
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
    await message.answer(f"📂 Current Folder:\n{' > '.join(canonical)}")


@dp.message(Command("ls"))
async def ls_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data()
    user_id = message.from_user.id
    cwd = get_cwd(user_id)

    if cwd:
        node = find_node_by_path(data["folders"], cwd)
        if node is None:
            # The selected folder was deleted/renamed via some other path
            # since /cd was last run — fall back to root instead of erroring.
            clear_cwd(user_id)
            await message.answer("⚠️ Current folder no longer exists — moved back to root.")
            return
        ensure_node_fields(node)
        children = node.get("children", [])
        buttons  = node.get("buttons", [])
        link     = node.get("link")
        header   = f"📂 {' > '.join(cwd)}"
    else:
        children = data["folders"]
        buttons  = []
        link     = None
        header   = "📂 root"

    lines = [header]
    if link:
        lines.append(f"\n🔗 Direct link: {link}")
    if children:
        lines.append("\nFolders:")
        lines.extend(f"  📁 {c['name']}" for c in children)
    if buttons:
        lines.append("\nButtons:")
        lines.extend(f"  🔘 {b['title']} → {b['url']}" for b in buttons)
    if not children and not buttons and not link:
        lines.append("\n(empty)")

    await message.answer("\n".join(lines))


@dp.message(Command("pwd"))
async def pwd_cmd(message: Message):
    if not is_admin(message):
        return

    cwd = get_cwd(message.from_user.id)
    if cwd:
        await message.answer(f"📍 Current Folder:\n{' > '.join(cwd)}")
    else:
        await message.answer("📍 Current Folder: root")


@dp.message(Command("back"))
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
async def exit_cmd(message: Message):
    if not is_admin(message):
        return

    clear_cwd(message.from_user.id)
    await message.answer("🏠 Returned to root/main menu.")


# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "ACHIEVER 8.0 Admin Bot\n\n"
        "── Draft -> Publish ──\n"
        "All edits below happen in DRAFT only — the live site is never\n"
        "touched until you run /publish.\n"
        "/initdraft   (one-time: create draft.json from live data.json)\n"
        "/syncdraft   (reset draft to match live, discarding unpublished edits)\n"
        "/drafttree   (preview the draft structure — not live yet)\n"
        "/publish     (push draft.json live as data.json)\n\n"
        "── Folder Navigation (saves typing) ──\n"
        "/cd FolderName        (enter a folder/subfolder)\n"
        "/cd Folder|Sub|...    (jump several levels at once)\n"
        "/ls                   (show folders/buttons here)\n"
        "/pwd                  (show current folder)\n"
        "/back                 (go up one level)\n"
        "/exit                 (return to root/main menu)\n"
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
        "/deletefolder FolderName         (always root-level)\n"
        "/deletesubfolder Folder|Sub1|...|TargetSub\n"
        "/renamefolder OldFolder|NewFolder   (always root-level)\n"
        "/renamesubfolder Folder|Sub1|...|OldName|NewName\n\n"
        "── Buttons (any level, unlimited per lecture) ──\n"
        "/addbutton Folder|Label|URL\n"
        "/addbutton Folder|Sub|Label|URL\n"
        "/addbutton Folder|Sub1|Sub2|...|Label|URL\n"
        "/deletebutton Folder|...|Label\n"
        "/renamebutton Folder|...|OldLabel|NewLabel\n"
        "/listbuttons Folder\n"
        "/listbuttons Folder|Sub|...\n\n"
        "── Other ──\n"
        "/tree\n"
        "/setlink Folder|...|URL   (folder opens this URL directly, no buttons/navigation)\n"
        "/removelink Folder|...\n"
        "/migrate   (one-time: push old draft data into the new button format)"
    )


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
    save_data(data, DATA_FILE)
    await message.answer(
        "🚀 Published! draft.json is now live as data.json.\n"
        "The website will update automatically once Vercel finishes redeploying (usually under a minute)."
    )


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
    ok, error = delete_node(data["folders"], [folder_name])
    if not ok:
        await message.answer(f"❌ {error}")
        return

    save_data(data)
    await message.answer(f"🗑 Deleted: {folder_name}")


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
    await message.answer(f"✅ Added '{new_name}' inside '{' > '.join(parent_path)}'")


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

    ok, error = delete_node(data["folders"], full_path)
    if not ok:
        await message.answer(f"❌ {error}")
        return

    save_data(data)
    await message.answer(f"🗑 Deleted '{full_path[-1]}' from '{' > '.join(full_path[:-1])}'")


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
    await message.answer(f"✅ Added button '{title}' to '{' > '.join(node_path)}'")


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

    buttons     = node.get("buttons", [])
    new_buttons = [b for b in buttons if b["title"].lower() != title.lower()]

    if len(new_buttons) == len(buttons):
        await message.answer(f"❌ Button '{title}' not found")
        return

    node["buttons"] = new_buttons
    save_data(data)
    await message.answer(f"🗑 Deleted button '{title}' from '{' > '.join(node_path)}'")


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
    await message.answer(f"🔗 Link set on '{' > '.join(node_path)}': {url}")


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

    del node["link"]
    save_data(data)
    await message.answer(f"🗑 Removed link from '{' > '.join(node_path)}'")


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


async def main():
    print("Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    Thread(target=run_web).start()
    asyncio.run(main())
    
