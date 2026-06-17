import json
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import asyncio
import os

print("TOKEN EXISTS:", bool(os.getenv("BOT_TOKEN")))

TOKEN = os.getenv("BOT_TOKEN")
print("TOKEN:", TOKEN)
bot = Bot(token=TOKEN)
dp = Dispatcher()

DATA_FILE = "data.json"

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"name": "ACHIEVER 8.0", "folders": []}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

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
