import os
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

message = """
✅ QuickJobsBot працює

Категорії:

🛒 Shops
👔 Premium Retail
☕ Cafe & Restaurants
📦 Warehouse
🎯 Easy Jobs
"""

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

requests.post(
    url,
    json={
        "chat_id": CHAT_ID,
        "text": message
    }
)

print("Message sent")
