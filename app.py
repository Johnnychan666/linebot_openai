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
import time
import traceback
# ====== python 的函數庫 ==========

# ====== 爬蟲相關套件（改用 requests + BeautifulSoup） ==========
import requests
from bs4 import BeautifulSoup
# ====== 爬蟲相關套件 ==========

app = Flask(__name__)
static_tmp_path = os.path.join(os.path.dirname(__file__), 'static', 'tmp')

# Channel Access Token
line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
# Channel Secret
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# ===================================
# UDN 運動新聞爬蟲設定（不用 Selenium）
# ===================================
URL = 'https://udn.com/news/cate/2/7227'  # 運動新聞
BASE_URL = 'https://udn.com'


def scrape_udn_latest(limit=5):
    """
    直接用 requests 拿分類頁 HTML，
    回傳一個 list，裡面每筆是 {'標題':..., '連結':...}
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

    # 這個選擇器延用你原本 Selenium 用的
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

        if len(data) >= limit:
            break

    print(f"[爬蟲] 共取得 {len(data)} 筆資料（已截到 {limit} 筆）")
    return data


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
        # 開始爬取最新文章（前 5 則）
        news_list = scrape_udn_latest(limit=5)

        if not news_list:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='目前無法取得運動新聞，請稍後再試。')
            )
            return

        messages = []
        for i, row in enumerate(news_list, start=1):
            text = f"{i}. {row['標題']}\n{row['連結']}"
            messages.append(TextSendMessage(text=text))

        line_bot_api.reply_message(event.reply_token, messages)
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
