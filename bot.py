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

def load_data():
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_USERNAME")
    github_repo = os.getenv("GITHUB_REPO")

    url = f"https://api.github.com/repos/{github_user}/{github_repo}/contents/data.json"

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json"
    }

    response = requests.get(url, headers=headers).json()

    content = base64.b64decode(
        response["content"]
    ).decode("utf-8")

    return json.loads(content)

def save_data(data):
    github_token = os.getenv("GITHUB_TOKEN")
    github_user = os.getenv("GITHUB_USERNAME")
    github_repo = os.getenv("GITHUB_REPO")

    url = f"https://api.github.com/repos/{github_user}/{github_repo}/contents/data.json"

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json"
    }

    current = requests.get(url, headers=headers).json()

    sha = current["sha"]

    content = json.dumps(data, indent=2)
    encoded = base64.b64encode(content.encode()).decode()

    payload = {
        "message": "Update data.json from bot",
        "content": encoded,
        "sha": sha
    }

    requests.put(url, headers=headers, json=payload)

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_admin(message):
        return

    await message.answer(
    "ACHIEVER 8.0 Admin Bot\n\n"
    "/listfolders\n"
    "/addfolder FolderName\n"
    "/deletefolder FolderName\n"
    "/renamefolder OldFolder|NewFolder\n"
    "/addsubfolder Parent|Sub\n"
    "/deletesubfolder Parent|Sub\n"
    "/renamesubfolder Parent|OldSub|NewSub\n"
    "/addlink Parent|Sub|Title|URL\n"
    "/deletelink Parent|Sub|Title\n"
    "/showlinks Parent|Sub\n"
    "/tree"
    )

@dp.message(Command("addfolder"))
async def add_folder(message: Message):
    if not is_admin(message):
        return

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer("Usage:\n/addfolder FolderName")
        return

    folder_name = args[1]

    data = load_data()

    data["folders"].append({
        "name": folder_name,
        "children": []
    })

    save_data(data)

    await message.answer(f"✅ Folder added: {folder_name}")

@dp.message(Command("listfolders"))
async def list_folders(message: Message):
    if not is_admin(message):
        return

    data = load_data()

    folders = data["folders"]

    if not folders:
        await message.answer("No folders found.")
        return

    text = "\n".join(
        [f"{i+1}. {f['name']}" for i, f in enumerate(folders)]
    )

    await message.answer(text)

@dp.message(Command("addsubfolder"))
async def add_subfolder(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/addsubfolder ", "", 1)

    if "|" not in args:
        await message.answer(
            "Usage:\n/addsubfolder ParentFolder|SubFolder"
        )
        return

    parent_name, sub_name = args.split("|", 1)

    data = load_data()

    found = False

    for folder in data["folders"]:
        if folder["name"].lower() == parent_name.lower():

            if "children" not in folder:
                folder["children"] = []

            folder["children"].append({
                "name": sub_name,
                "children": []
            })

            found = True
            break

    if not found:
        await message.answer("❌ Parent folder not found")
        return

    save_data(data)

    await message.answer(
        f"✅ Added '{sub_name}' inside '{parent_name}'"
    )

@dp.message(Command("tree"))
async def tree_cmd(message: Message):
    if not is_admin(message):
        return

    data = load_data()

    def build_tree(folders, level=0):
        text = ""

        for folder in folders:
            text += "  " * level + f"📁 {folder['name']}\n"

            if folder.get("children"):
                text += build_tree(folder["children"], level + 1)

        return text

    tree_text = build_tree(data["folders"])

    if not tree_text:
        tree_text = "No folders found."

    await message.answer(tree_text)

@dp.message(Command("deletesubfolder"))
async def delete_subfolder(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/deletesubfolder ", "", 1)

    if "|" not in args:
        await message.answer(
            "Usage:\n/deletesubfolder ParentFolder|SubFolder"
        )
        return

    parent_name, sub_name = args.split("|", 1)

    data = load_data()

    found_parent = False
    found_sub = False

    for folder in data["folders"]:
        if folder["name"].lower() == parent_name.lower():

            found_parent = True

            children = folder.get("children", [])

            new_children = []

            for child in children:
                if child["name"].lower() == sub_name.lower():
                    found_sub = True
                else:
                    new_children.append(child)

            folder["children"] = new_children
            break

    if not found_parent:
        await message.answer("❌ Parent folder not found")
        return

    if not found_sub:
        await message.answer("❌ Subfolder not found")
        return

    save_data(data)

    await message.answer(
        f"🗑 Deleted '{sub_name}' from '{parent_name}'"
    )

@dp.message(Command("deletefolder"))
async def delete_folder(message: Message):
    if not is_admin(message):
        return

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer("Usage:\n/deletefolder FolderName")
        return

    folder_name = args[1]

    data = load_data()

    data["folders"] = [
        f for f in data["folders"]
        if f["name"].lower() != folder_name.lower()
    ]

    save_data(data)

    await message.answer(f"🗑 Deleted: {folder_name}")

@dp.message(Command("renamefolder"))
async def rename_folder(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/renamefolder ", "", 1)

    if "|" not in args:
        await message.answer(
            "Usage:\n/renamefolder OldFolder|NewFolder"
        )
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

@dp.message(Command("renamesubfolder"))
async def rename_subfolder(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/renamesubfolder ", "", 1)

    parts = args.split("|")

    if len(parts) != 3:
        await message.answer(
            "Usage:\n/renamesubfolder ParentFolder|OldSubfolder|NewSubfolder"
        )
        return

    parent_name, old_name, new_name = parts

    data = load_data()

    found_parent = False
    found_sub = False

    for folder in data["folders"]:
        if folder["name"].lower() == parent_name.lower():
            found_parent = True

            for child in folder.get("children", []):
                if child["name"].lower() == old_name.lower():
                    child["name"] = new_name
                    found_sub = True
                    break

            break

    if not found_parent:
        await message.answer("❌ Parent folder not found")
        return

    if not found_sub:
        await message.answer("❌ Subfolder not found")
        return

    save_data(data)

    await message.answer(
        f"✅ Renamed '{old_name}' to '{new_name}' inside '{parent_name}'"
    )

@dp.message(Command("addlink"))
async def add_link(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/addlink ", "", 1)

    parts = args.split("|")

    if len(parts) != 4:
        await message.answer(
            "Usage:\n/addlink ParentFolder|Subfolder|Title|URL"
        )
        return

    parent_name, sub_name, title, url = parts

    data = load_data()

    found_parent = False
    found_sub = False

    for folder in data["folders"]:
        if folder["name"].lower() == parent_name.lower():
            found_parent = True

            for child in folder.get("children", []):
                if child["name"].lower() == sub_name.lower():
                    found_sub = True

                    if "links" not in child:
                        child["links"] = []

                    child["links"].append({
                        "title": title,
                        "url": url
                    })

                    break

            break

    if not found_parent:
        await message.answer("❌ Parent folder not found")
        return

    if not found_sub:
        await message.answer("❌ Subfolder not found")
        return

    save_data(data)

    await message.answer(
        f"✅ Added link '{title}' to '{parent_name} > {sub_name}'"
    )

@dp.message(Command("deletelink"))
async def delete_link(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/deletelink ", "", 1)

    parts = args.split("|")

    if len(parts) != 3:
        await message.answer(
            "Usage:\n/deletelink ParentFolder|Subfolder|Title"
        )
        return

    parent_name, sub_name, title = parts

    data = load_data()

    found_parent = False
    found_sub = False
    found_link = False

    for folder in data["folders"]:
        if folder["name"].lower() == parent_name.lower():
            found_parent = True

            for child in folder.get("children", []):
                if child["name"].lower() == sub_name.lower():
                    found_sub = True

                    links = child.get("links", [])
                    new_links = []

                    for link in links:
                        if link["title"].lower() == title.lower():
                            found_link = True
                        else:
                            new_links.append(link)

                    child["links"] = new_links
                    break

            break

    if not found_parent:
        await message.answer("❌ Parent folder not found")
        return

    if not found_sub:
        await message.answer("❌ Subfolder not found")
        return

    if not found_link:
        await message.answer("❌ Link not found")
        return

    save_data(data)

    await message.answer(
        f"🗑 Deleted link '{title}' from '{parent_name} > {sub_name}'"
    )

@dp.message(Command("showlinks"))
async def show_links(message: Message):
    if not is_admin(message):
        return

    args = message.text.replace("/showlinks ", "", 1)

    if "|" not in args:
        await message.answer(
            "Usage:\n/showlinks ParentFolder|Subfolder"
        )
        return

    parent_name, sub_name = args.split("|", 1)

    data = load_data()

    found_parent = False
    found_sub = False
    links = []

    for folder in data["folders"]:
        if folder["name"].lower() == parent_name.lower():
            found_parent = True

            for child in folder.get("children", []):
                if child["name"].lower() == sub_name.lower():
                    found_sub = True
                    links = child.get("links", [])
                    break

            break

    if not found_parent:
        await message.answer("❌ Parent folder not found")
        return

    if not found_sub:
        await message.answer("❌ Subfolder not found")
        return

    if not links:
        await message.answer("No links found in this subfolder.")
        return

    text = ""

    for i, link in enumerate(links):
        text += f"{i+1}. {link['title']}\n{link['url']}\n\n"

    await message.answer(text.strip())

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
            
