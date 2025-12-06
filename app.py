from flask import Flask, request, abort

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import *

# ====== python 的函數庫 ==========
import os
import traceback
# ====== python 的函數庫 ==========

# ====== 靜態爬蟲相關套件 ==========
import requests
from bs4 import BeautifulSoup
# ====== 靜態爬蟲相關套件 ==========

app = Flask(__name__)
static_tmp_path = os.path.join(os.path.dirname(__file__), 'static', 'tmp')

# Channel Access Token
line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
# Channel Secret
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# ===================================
# UDN 運動新聞靜態爬蟲設定
# ===================================
URL = 'https://udn.com/news/cate/2/7227'  # 運動新聞
BASE_URL = 'https://udn.com'

# 每頁顯示幾則（按一次按鈕 = 一頁）
PAGE_SIZE = 5

# 紀錄每個聊天目前看到第幾頁
# key: chat_id (user_id / group_id / room_id)
# value: page (1 開始)
news_page_state = {}


def scrape_udn_latest():
    """
    靜態爬蟲：抓 UDN 運動新聞列表
    回傳一個 list，每筆是 {'標題': ..., '連結': ...}
    （不在這裡做分頁，一次抓多筆回來，後面再切 1-5、6-10）
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(URL, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print("❌ 取得 UDN 頁面失敗：", e)
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')

    # 延用你原本的選擇器
    news_elements = soup.select('div.story-list__text a')

    data = []
    for element in news_elements:
        title = element.get_text(strip=True)
        href = element.get('href')

        if not title or not href:
            continue

        if href.startswith('/'):
            href = BASE_URL + href

        if 'udn.com' not in href:
            continue

        data.append({
            '標題': title,
            '連結': href,
        })

    print(f"[爬蟲] 共取得 {len(data)} 筆資料")
    return data


def get_chat_id(event):
    """
    取得這個聊天的唯一 ID：
    - 1:1 對話 → user_id
    - 群組 → group_id
    - 多人聊天室 → room_id
    """
    source = event.source
    if isinstance(source, SourceUser):
        return source.user_id
    elif isinstance(source, SourceGroup):
        return source.group_id
    elif isinstance(source, SourceRoom):
        return source.room_id
    else:
        return "unknown"


# ==========================
# Flask / LINE Webhook
# ==========================
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


# ==========================
# 處理文字訊息（先跳出運動選項按鈕）
# ==========================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    buttons_template = TemplateSendMessage(
        alt_text='功能選單',
        template=ButtonsTemplate(
            title='功能選單',
            text='請選擇想使用的服務',
            actions=[
                PostbackAction(
                    label='運動新聞',
                    display_text='我要看運動新聞',
                    data='action=sports_news'
                )
            ]
        )
    )

    line_bot_api.reply_message(event.reply_token, buttons_template)


# ==========================
# 處理 Postback（按下運動新聞按鈕）
# ==========================
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    print(f"[Postback] data = {data}")

    if data == 'action=sports_news':
        chat_id = get_chat_id(event)

        # 目前是第幾頁？（預設第 1 頁）
        current_page = news_page_state.get(chat_id, 1)
        print(f"[sports_news] chat_id={chat_id}, current_page={current_page}")

        # 每次按按鈕都重新爬一次最新列表
        news_list = scrape_udn_latest()

        if not news_list:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='目前無法取得運動新聞，請稍後再試。')
            )
            return

        # 計算這一頁要顯示的範圍（1–5、6–10、11–15...）
        start_idx = (current_page - 1) * PAGE_SIZE
        end_idx = current_page * PAGE_SIZE
        page_items = news_list[start_idx:end_idx]

        if not page_items:
            # 已經沒有更多新聞了，提示一下並把頁數重置回 1
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='已經沒有更多最新新聞了，我幫你從第一頁重新開始喔！')
            )
            news_page_state[chat_id] = 1
            return

        messages = []
        # 顯示實際是第幾則（用全體排序的編號）
        for i, row in enumerate(page_items, start=start_idx + 1):
            text = f"{i}. {row['標題']}\n{row['連結']}"
            messages.append(TextSendMessage(text=text))

        line_bot_api.reply_message(event.reply_token, messages)

        # 下一次按按鈕，就看下一頁
        news_page_state[chat_id] = current_page + 1

    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='這個功能尚未支援唷！')
        )


# ==========================
# 歡迎新成員加入群組
# ==========================
@handler.add(MemberJoinedEvent)
def welcome(event):
    uid = event.joined.members[0].user_id
    gid = event.source.group_id
    profile = line_bot_api.get_group_member_profile(gid, uid)
    name = profile.display_name
    message = TextSendMessage(text=f'{name} 歡迎加入！')
    line_bot_api.reply_message(event.reply_token, message)


# ==========================
# 主程式入口
# ==========================
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
