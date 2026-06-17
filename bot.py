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
    await message.answer(
        "ACHIEVER 8.0 Admin Bot\n\n"
        "/addfolder FolderName\n"
        "/listfolders\n"
        "/deletefolder FolderName"
    )

@dp.message(Command("addfolder"))
async def add_folder(message: Message):
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
@dp.message(Command("deletefolder"))
async def delete_folder(message: Message):
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

    await message.answer(f"❌ Deleted: {folder_name}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
