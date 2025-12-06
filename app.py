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

# ====== 靜態爬蟲相關套件 ==========
import requests
from bs4 import BeautifulSoup
# ====== 靜態爬蟲相關套件 ==========

# ====== 文字雲相關套件 ==========
from wordcloud import WordCloud
import jieba
# ====== 文字雲相關套件 ==========


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

# 每次按按鈕顯示幾則
PAGE_SIZE = 5

# 紀錄每個聊天目前看到第幾頁
news_page_state = {}   # {chat_id: page}

# 紀錄每個聊天「已看過的新聞標題」，給文字雲用
seen_titles_state = {}  # {chat_id: [title1, title2, ...]}

# 使用專案根目錄的 msjh.ttc（微軟正黑體）
WORDCLOUD_FONT_PATH = os.path.join(os.path.dirname(__file__), 'msjh.ttc')


def scrape_udn_latest():
    """
    靜態爬蟲：抓 UDN 運動新聞列表
    回傳 list，每筆是 {'標題': ..., '連結': ...}
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


def generate_wordcloud_for_chat(chat_id):
    """
    根據「這個 chat_id 已看過的新聞標題」產生文字雲，
    把圖片存到 static/tmp/，回傳圖片 URL。
    """
    titles = seen_titles_state.get(chat_id)
    if not titles:
        print(f"[wordcloud] chat_id={chat_id} 尚未有任何標題")
        return None

    if not os.path.exists(WORDCLOUD_FONT_PATH):
        # 如果字型沒被找到，直接印錯並返回 None（避免框框）
        print(f"[wordcloud] 字型檔不存在: {WORDCLOUD_FONT_PATH}")
        return None

    all_titles = "。".join(titles)

    # jieba 斷詞
    words = jieba.cut(all_titles, cut_all=False)
    wc_text = " ".join(words)

    os.makedirs(static_tmp_path, exist_ok=True)

    wc = WordCloud(
        font_path=WORDCLOUD_FONT_PATH,  # 使用專案內的 msjh.ttc（繁體中文）
        width=800,
        height=600,
        background_color="white"
    ).generate(wc_text)

    filename = f'sports_wordcloud_{chat_id}_{int(time.time())}.png'
    filepath = os.path.join(static_tmp_path, filename)
    wc.to_file(filepath)

    # 用 request.url_root 組出完整 URL，例如 https://xxx.onrender.com/static/tmp/xxx.png
    base_url = request.url_root.rstrip('/')  # e.g. https://linebot-openai-test.onrender.com
    image_url = f"{base_url}/static/tmp/{filename}"

    print(f"[wordcloud] chat_id={chat_id}, image_url={image_url}")
    return image_url


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
# 處理文字訊息
# ==========================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_text = event.message.text.strip()
    chat_id = get_chat_id(event)

    # ✅ 觸發文字雲
    if user_text == "幫我生成文字雲":
        image_url = generate_wordcloud_for_chat(chat_id)
        if not image_url:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='目前沒有可用的新聞標題，或文字雲字型檔未正確設定，請先多看幾則運動新聞再試一次喔！')
            )
            return

        image_message = ImageSendMessage(
            original_content_url=image_url,
            preview_image_url=image_url
        )
        line_bot_api.reply_message(event.reply_token, image_message)
        return

    # 其他文字 → 顯示功能選單
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
            # 已經沒有更多新聞了，提示一下並把頁數重置回 1，並清空已看標題
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='已經沒有更多最新新聞了，我幫你從第一頁重新開始喔！')
            )
            news_page_state[chat_id] = 1
            seen_titles_state[chat_id] = []
            return

        # 把這一頁的標題累積起來，給文字雲用
        seen_list = seen_titles_state.get(chat_id, [])
        for row in page_items:
            seen_list.append(row['標題'])
        seen_titles_state[chat_id] = seen_list
        print(f"[sports_news] chat_id={chat_id}, 累積標題數={len(seen_list)}")

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
