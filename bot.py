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

ADMIN_ID = 8837854952

def is_admin(message):
    return message.from_user.id == ADMIN_ID

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
    return json.loads(content)

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

# ─── Recursive tree helpers ───────────────────────────────────────────────────

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
    """Make sure a folder node has both 'children' and 'links' keys."""
    if "children" not in node:
        node["children"] = []
    if "links" not in node:
        node["links"] = []

# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "ACHIEVER 8.0 Admin Bot\n\n"
        "── Folders ──\n"
        "/listfolders\n"
        "/addfolder FolderName\n"
        "/deletefolder FolderName\n"
        "/renamefolder OldFolder|NewFolder\n\n"
        "── Subfolders (unlimited depth) ──\n"
        "/addsubfolder Folder|Sub\n"
        "/addsubfolder Folder|Sub1|Sub2|...\n"
        "/deletesubfolder Folder|Sub1|...|TargetSub\n"
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

# ─── /addfolder ───────────────────────────────────────────────────────────────

@dp.message(Command("addfolder"))
async def add_folder(message: Message):
    if not is_admin(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage:\n/addfolder FolderName")
        return

    folder_name = args[1].strip()
    data = load_data()

    data["folders"].append({"name": folder_name, "children": [], "links": []})
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

# ─── /deletefolder ────────────────────────────────────────────────────────────

@dp.message(Command("deletefolder"))
async def delete_folder(message: Message):
    if not is_admin(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage:\n/deletefolder FolderName")
        return

    folder_name = args[1].strip()
    data = load_data()

    before = len(data["folders"])
    data["folders"] = [
        f for f in data["folders"]
        if f["name"].lower() != folder_name.lower()
    ]

    if len(data["folders"]) == before:
        await message.answer("❌ Folder not found")
        return

    save_data(data)
    await message.answer(f"🗑 Deleted: {folder_name}")

# ─── /renamefolder ────────────────────────────────────────────────────────────

@dp.message(Command("renamefolder"))
async def rename_folder(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/renamefolder ", "", 1).strip()
    if "|" not in args:
        await message.answer("Usage:\n/renamefolder OldFolder|NewFolder")
        return

    old_name, new_name = args.split("|", 1)
    data = load_data()

    found = False
    for folder in data["folders"]:
        if folder["name"].lower() == old_name.lower():
            folder["name"] = new_name
            found = True
            break

    if not found:
        await message.answer("❌ Folder not found")
        return

    save_data(data)
    await message.answer(f"✅ Renamed '{old_name}' to '{new_name}'")

# ─── /addsubfolder  (unlimited depth) ────────────────────────────────────────
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

    args = message.text.replace("/addsubfolder ", "", 1).strip()
    parts = [p.strip() for p in args.split("|")]

    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/addsubfolder ParentFolder|SubFolder\n"
            "/addsubfolder Folder|Sub1|Sub2|NewSub"
        )
        return

    parent_path = parts[:-1]   # path to existing parent
    new_name    = parts[-1]    # name of the new child

    data   = load_data()
    parent = find_node_by_path(data["folders"], parent_path)

    if parent is None:
        await message.answer(f"❌ Path not found: {' > '.join(parent_path)}")
        return

    ensure_node_fields(parent)
    parent["children"].append({"name": new_name, "children": [], "links": []})

    save_data(data)
    await message.answer(
        f"✅ Added '{new_name}' inside '{' > '.join(parent_path)}'"
    )

# ─── /deletesubfolder  (unlimited depth) ─────────────────────────────────────
#
#   /deletesubfolder Maths|Algebra
#   /deletesubfolder Maths|Algebra|Quadratic

@dp.message(Command("deletesubfolder"))
async def delete_subfolder(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/deletesubfolder ", "", 1).strip()
    parts = [p.strip() for p in args.split("|")]

    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/deletesubfolder ParentFolder|SubFolder\n"
            "/deletesubfolder Folder|Sub1|Sub2|TargetSub"
        )
        return

    parent_path  = parts[:-1]
    target_name  = parts[-1]
    data         = load_data()
    parent       = find_node_by_path(data["folders"], parent_path)

    if parent is None:
        await message.answer(f"❌ Path not found: {' > '.join(parent_path)}")
        return

    children    = parent.get("children", [])
    new_children = [c for c in children if c["name"].lower() != target_name.lower()]

    if len(new_children) == len(children):
        await message.answer(f"❌ Subfolder '{target_name}' not found")
        return

    parent["children"] = new_children
    save_data(data)
    await message.answer(
        f"🗑 Deleted '{target_name}' from '{' > '.join(parent_path)}'"
    )

# ─── /renamesubfolder  (unlimited depth) ─────────────────────────────────────
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

    args  = message.text.replace("/renamesubfolder ", "", 1).strip()
    parts = [p.strip() for p in args.split("|")]

    if len(parts) < 3:
        await message.answer(
            "Usage:\n"
            "/renamesubfolder ParentFolder|OldSubfolder|NewSubfolder\n"
            "/renamesubfolder Folder|Sub1|...|OldName|NewName"
        )
        return

    parent_path = parts[:-2]
    old_name    = parts[-2]
    new_name    = parts[-1]

    data   = load_data()
    parent = find_node_by_path(data["folders"], parent_path)

    if parent is None:
        await message.answer(f"❌ Path not found: {' > '.join(parent_path)}")
        return

    found = False
    for child in parent.get("children", []):
        if child["name"].lower() == old_name.lower():
            child["name"] = new_name
            found = True
            break

    if not found:
        await message.answer(f"❌ Subfolder '{old_name}' not found")
        return

    save_data(data)
    await message.answer(
        f"✅ Renamed '{old_name}' to '{new_name}' inside '{' > '.join(parent_path)}'"
    )

# ─── /addlink  (any depth) ───────────────────────────────────────────────────
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

    args  = message.text.replace("/addlink ", "", 1).strip()
    parts = [p.strip() for p in args.split("|")]

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
        # /addlink Folder|URL  — add directly to root folder, title = URL
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
    await message.answer(
        f"✅ Added link '{title}' to '{' > '.join(node_path)}'"
    )

# ─── /deletelink  (any depth) ────────────────────────────────────────────────
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

    args  = message.text.replace("/deletelink ", "", 1).strip()
    parts = [p.strip() for p in args.split("|")]

    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "/deletelink Folder|Title\n"
            "/deletelink Folder|Sub|Title\n"
            "/deletelink Folder|Sub1|Sub2|...|Title"
        )
        return

    node_path  = parts[:-1]
    title      = parts[-1]
    data       = load_data()
    node       = find_node_by_path(data["folders"], node_path)

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
    await message.answer(
        f"🗑 Deleted link '{title}' from '{' > '.join(node_path)}'"
    )

# ─── /showlinks  (any depth) ─────────────────────────────────────────────────
#
#   /showlinks Folder
#   /showlinks Folder|Sub
#   /showlinks Folder|Sub1|Sub2|...

@dp.message(Command("showlinks"))
async def show_links(message: Message):
    if not is_admin(message):
        return

    args      = message.text.replace("/showlinks ", "", 1).strip()
    node_path = [p.strip() for p in args.split("|")]
    data      = load_data()
    node      = find_node_by_path(data["folders"], node_path)

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
