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
    Replaces the old `message.text.replace("/cmd ", "", 1)` pattern, which
    silently failed to strip anything if the command was sent with a bot
    mention (e.g. "/addfolder@MyBot Name" in a group chat) — in that case
    `args` would have ended up as the full, unparsed message text.
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

    # Backward compatibility: very old data.json files may not have a
    # top-level "folders" key at all. Without this, every handler below
    # would raise a KeyError on data["folders"].
    data.setdefault("folders", [])
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
    """Make sure a folder node has both 'children' and 'links' keys (backward compatibility)."""
    if "children" not in node:
        node["children"] = []
    if "links" not in node:
        node["links"] = []


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
    node_list.append({"name": new_name, "children": [], "links": []})
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
        "── Links (any level) ──\n"
        "/addlink Folder|Title|URL\n"
        "/addlink Folder|Sub|Title|URL\n"
        "/addlink Folder|Sub1|Sub2|...|Title|URL\n"
        "/deletelink Folder|...|Title\n"
        "/showlinks Folder\n"
        "/showlinks Folder|Sub|...\n\n"
        "── Other ──\n"
        "/tree"
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


# ─── /addlink (any depth) ───────────────────────────────────────────────────
#
#   Formats accepted:
#     /addlink Folder|URL                     (2 parts → title = URL)
#     /addlink Folder|Title|URL               (3 parts)
#     /addlink Folder|Sub|Title|URL           (4 parts)
#     /addlink Folder|Sub1|Sub2|...|Title|URL (N parts, last 2 = title+url)
#
#   All parts except the last two are the path to the target node.
#   Exception: if only 2 parts, the path is parts[0] and URL = parts[1],
#   title defaults to the URL.

@dp.message(Command("addlink"))
async def add_link(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/addlink Folder|URL\n"
            "/addlink Folder|Title|URL\n"
            "/addlink Folder|Sub|Title|URL\n"
            "/addlink Folder|Sub1|Sub2|...|Title|URL"
        )
        return

    if len(parts) == 2:
        # /addlink Folder|URL — add directly to a root folder, title = URL
        node_path = [parts[0]]
        title     = parts[1]
        url       = parts[1]
    else:
        # last part = URL, second-to-last = title, everything before = path
        node_path = parts[:-2]
        title     = parts[-2]
        url       = parts[-1]

    data = load_data()
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    ensure_node_fields(node)
    node["links"].append({"title": title, "url": url})

    save_data(data)
    await message.answer(f"✅ Added link '{title}' to '{' > '.join(node_path)}'")


# ─── /deletelink (any depth) ────────────────────────────────────────────────
#
#   /deletelink Folder|Title
#   /deletelink Folder|Sub|Title
#   /deletelink Folder|Sub1|Sub2|...|Title
#
#   All parts except the last form the path; last part = link title.

@dp.message(Command("deletelink"))
async def delete_link(message: Message):
    if not is_admin(message):
        return

    parts = split_pipe_args(message)
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/deletelink Folder|Title\n"
            "/deletelink Folder|Sub|Title\n"
            "/deletelink Folder|Sub1|Sub2|...|Title"
        )
        return

    node_path = parts[:-1]
    title     = parts[-1]

    data = load_data()
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    links     = node.get("links", [])
    new_links = [l for l in links if l["title"].lower() != title.lower()]

    if len(new_links) == len(links):
        await message.answer(f"❌ Link '{title}' not found")
        return

    node["links"] = new_links
    save_data(data)
    await message.answer(f"🗑 Deleted link '{title}' from '{' > '.join(node_path)}'")


# ─── /showlinks (any depth) ─────────────────────────────────────────────────
#
#   /showlinks Folder
#   /showlinks Folder|Sub
#   /showlinks Folder|Sub1|Sub2|...

@dp.message(Command("showlinks"))
async def show_links(message: Message):
    if not is_admin(message):
        return

    node_path = split_pipe_args(message)
    if not node_path:
        await message.answer(
            "Usage:\n"
            "/showlinks Folder\n"
            "/showlinks Folder|Sub|..."
        )
        return

    data = load_data()
    node = find_node_by_path(data["folders"], node_path)

    if node is None:
        await message.answer(f"❌ Path not found: {' > '.join(node_path)}")
        return

    links = node.get("links", [])
    if not links:
        await message.answer("No links found here.")
        return

    text = ""
    for i, link in enumerate(links):
        text += f"{i+1}. {link['title']}\n{link['url']}\n\n"

    await message.answer(text.strip())


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
            for link in folder.get("links", []):
                text += f"{indent}  🔗 {link['title']}\n"
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
