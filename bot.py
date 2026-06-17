import json
import requests
import base64
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import asyncio
import os

print("TOKEN EXISTS:", bool(os.getenv("BOT_TOKEN")))

TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()

DATA_FILE = "data.json"

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

def load_data():
    github_token = os.getenv("GITHUB_TOKEN")
    github_user  = os.getenv("GITHUB_USERNAME")
    github_repo  = os.getenv("GITHUB_REPO")

    url = f"https://api.github.com/repos/{github_user}/{github_repo}/contents/data.json"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json"
    }

    response = requests.get(url, headers=headers).json()
    content  = base64.b64decode(response["content"]).decode("utf-8")
    data     = json.loads(content)

    # Every read is auto-upgraded to the new button structure in memory.
    # The next time anything calls save_data() (any admin command, or
    # /migrate below), the upgraded shape gets written back to GitHub.
    migrate_data(data)
    return data


def save_data(data):
    github_token = os.getenv("GITHUB_TOKEN")
    github_user  = os.getenv("GITHUB_USERNAME")
    github_repo  = os.getenv("GITHUB_REPO")

    url = f"https://api.github.com/repos/{github_user}/{github_repo}/contents/data.json"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json"
    }

    current = requests.get(url, headers=headers).json()
    sha     = current["sha"]

    content = json.dumps(data, indent=2)
    encoded = base64.b64encode(content.encode()).decode()

    payload = {
        "message": "Update data.json from bot",
        "content": encoded,
        "sha": sha
    }
    requests.put(url, headers=headers, json=payload)


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
    node_list.append({"name": new_name, "children": [], "buttons": []})
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


def migrate_node(node):
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
    for child in node["children"]:
        migrate_node(child)


def migrate_data(data):
    data.setdefault("folders", [])
    for folder in data["folders"]:
        migrate_node(folder)
    return data


# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "ACHIEVER 8.0 Admin Bot\n\n"
        "── Folders (any depth) ──\n"
        "/listfolders\n"
        "/addfolder FolderName\n"
        "/addsubfolder Folder|Sub\n"
        "/addsubfolder Folder|Sub1|Sub2|...\n"
        "/deletefolder FolderName\n"
        "/deletesubfolder Folder|Sub1|...|TargetSub\n"
        "/renamefolder OldFolder|NewFolder\n"
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
        "/migrate   (one-time: push old data into the new button format)"
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
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/addsubfolder ParentFolder|SubFolder\n"
            "/addsubfolder Folder|Sub1|Sub2|NewSub"
        )
        return

    parent_path = parts[:-1]
    new_name    = parts[-1]

    data = load_data()
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
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/deletesubfolder ParentFolder|SubFolder\n"
            "/deletesubfolder Folder|Sub1|Sub2|TargetSub"
        )
        return

    data = load_data()
    ok, error = delete_node(data["folders"], parts)
    if not ok:
        await message.answer(f"❌ {error}")
        return

    save_data(data)
    await message.answer(f"🗑 Deleted '{parts[-1]}' from '{' > '.join(parts[:-1])}'")


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
    if len(parts) < 3:
        await message.answer(
            "Usage:\n"
            "/renamesubfolder ParentFolder|OldSubfolder|NewSubfolder\n"
            "/renamesubfolder Folder|Sub1|...|OldName|NewName"
        )
        return

    full_path = parts[:-1]   # path to the node being renamed (includes its current name)
    new_name  = parts[-1]

    data = load_data()
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
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/addbutton Folder|URL\n"
            "/addbutton Folder|Label|URL\n"
            "/addbutton Folder|Sub|Label|URL\n"
            "/addbutton Folder|Sub1|Sub2|...|Label|URL"
        )
        return

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
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/deletebutton Folder|Label\n"
            "/deletebutton Folder|Sub|Label\n"
            "/deletebutton Folder|Sub1|Sub2|...|Label"
        )
        return

    node_path = parts[:-1]
    title     = parts[-1]

    data = load_data()
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
    if len(parts) < 3:
        await message.answer(
            "Usage:\n"
            "/renamebutton Folder|OldLabel|NewLabel\n"
            "/renamebutton Folder|Sub|...|OldLabel|NewLabel"
        )
        return

    node_path = parts[:-2]
    old_title, new_title = parts[-2], parts[-1]

    data = load_data()
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
        f"✅ Renamed button '{old_title}' to '{new_title}' in '{' > '.join(node_path)}'"
    )


# ─── /listbuttons (any depth) ────────────────────────────────────────────────
#
#   /listbuttons Folder
#   /listbuttons Folder|Sub|...
#
#   /showlinks is kept as an alias.

@dp.message(Command("listbuttons"))
@dp.message(Command("showlinks"))
async def list_buttons(message: Message):
    if not is_admin(message):
        return

    node_path = split_pipe_args(message)
    if not node_path:
        await message.answer(
            "Usage:\n"
            "/listbuttons Folder\n"
            "/listbuttons Folder|Sub|..."
        )
        return

    data = load_data()
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    buttons = node.get("buttons", [])
    if not buttons:
        await message.answer("No buttons found here.")
        return

    text = ""
    for i, btn in enumerate(buttons):
        text += f"{i+1}. {btn['title']}\n{btn['url']}\n\n"

    await message.answer(text.strip())


# ─── /migrate ──────────────────────────────────────────────────────────────
#
# load_data() already migrates the tree in memory on every call. This
# command forces an immediate save, so the live data.json on GitHub gets
# the new button structure right away instead of waiting for the next
# unrelated edit to trigger a save.

@dp.message(Command("migrate"))
async def migrate_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data()
    save_data(data)
    await message.answer("✅ Data migrated to the new button structure and saved.")


# ─── /tree ────────────────────────────────────────────────────────────────────

@dp.message(Command("tree"))
async def tree_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data()

    def build_tree(folders, level=0):
        text = ""
        indent = "  " * level
        for folder in folders:
            text += f"{indent}📁 {folder['name']}\n"
            for btn in folder.get("buttons", []):
                text += f"{indent}  🔗 {btn['title']}\n"
            if folder.get("children"):
                text += build_tree(folder["children"], level + 1)
        return text

    tree_text = build_tree(data["folders"])
    if not tree_text:
        tree_text = "No folders found."

    await message.answer(tree_text)


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
