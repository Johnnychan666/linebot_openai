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
from urllib.parse import parse_qs
from collections import Counter
# ====== python 的函數庫 ==========

# ====== 靜態爬蟲相關套件 ==========
import requests
from bs4 import BeautifulSoup
# ====== 靜態爬蟲相關套件 ==========

# ====== 文字雲 / 圖表相關套件 ==========
from wordcloud import WordCloud
import jieba
import matplotlib
matplotlib.use("Agg")  # 無 GUI 環境用
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
# ====== 文字雲 / 圖表相關套件 ==========


app = Flask(__name__)
static_tmp_path = os.path.join(os.path.dirname(__file__), 'static', 'tmp')

# Channel Access Token / Secret
line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# ===================================
# UDN 各類新聞靜態爬蟲設定
# ===================================
BASE_URL = 'https://udn.com'

# 五個類別設定
CATEGORIES = {
    'sports': {
        'name': '運動',
        'url': 'https://udn.com/news/cate/2/7227'
    },
    'global': {
        'name': '全球',
        'url': 'https://udn.com/news/cate/2/7225'
    },
    'stock': {
        'name': '股市',
        'url': 'https://udn.com/news/cate/2/6645'
    },
    'social': {
        'name': '社會',
        'url': 'https://udn.com/news/cate/2/6639'
    },
    'econ': {
        'name': '產經',
        'url': 'https://udn.com/news/cate/2/6644'
    },
}

# 各類別在使用者句子裡可能出現的關鍵字（可自己再加）
CATEGORY_KEYWORDS = {
    'sports': ['運動', '體育', '球賽', '比賽', '棒球', '籃球', '足球'],
    'global': ['全球', '國際', '世界', '海外'],
    'stock':  ['股市', '股票', '股價', '台股', '股災', '股匯', '大盤'],
    'social': ['社會', '社會新聞', '治安', '命案', '車禍'],
    'econ':   ['產經', '產業', '經濟', '財經', '景氣']
}

# 每次按按鈕顯示幾則
PAGE_SIZE = 5

# 紀錄每個聊天、每個類別目前看到第幾頁
# 結構：{ chat_id: { category_key: page_int } }
news_page_state = {}

# 紀錄每個聊天、每個類別「已看過的標題」，給文字雲 / 情緒分析用
# 結構：{ chat_id: { 'all': [...], 'sports': [...], 'global': [...], ... } }
seen_titles_state = {}

# 使用專案根目錄的 msjh.ttc（微軟正黑體）
WORDCLOUD_FONT_PATH = os.path.join(os.path.dirname(__file__), 'msjh.ttc')

# ====== 簡單情緒字典（可以之後自己再擴充） ======
POSITIVE_WORDS = [
    "成長", "獲利", "創高", "創新高", "利多", "看好", "獎", "奪冠", "勝", "大勝",
    "飆升", "上漲", "暢旺", "樂觀", "改善", "突破", "熱烈", "亮眼"
]
NEGATIVE_WORDS = [
    "下跌", "重挫", "暴跌", "虧損", "災", "意外", "火警", "颱風", "地震", "暴雨",
    "死亡", "罹難", "警告", "風險", "衰退", "負成長", "爆炸", "暴力", "侵害", "詐騙"
]


# ===================================
# 共用小工具
# ===================================
def scrape_udn_category(category_key):
    """
    靜態爬蟲：抓 UDN 指定類別新聞列表
    回傳 list，每筆是 {'標題': ..., '連結': ...}
    """
    if category_key not in CATEGORIES:
        return []

    url = CATEGORIES[category_key]['url']

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ 取得 UDN {category_key} 頁面失敗：", e)
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

    print(f"[爬蟲] {category_key} 共取得 {len(data)} 筆資料")
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


def detect_category_from_text(text: str):
    """
    嘗試從使用者輸入的句子裡抓出類別 key
    找到第一個符合就回傳，例如 'sports' / 'global' / ...
    找不到就回傳 None
    """
    for key, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return key
    return None


# ===================================
# Quick Reply 建立函式
# ===================================
def build_category_quick_reply(action_type="news"):
    """
    建立五個類別的 QuickReply：
    action_type:
      - "news"      → 看新聞
      - "sentiment" → 情緒分析
    """
    return QuickReply(items=[
        QuickReplyButton(
            action=PostbackAction(
                label='運動新聞',
                display_text='我要看運動新聞' if action_type == "news" else '分析運動新聞情緒',
                data=f'action={action_type}&cat=sports'
            )
        ),
        QuickReplyButton(
            action=PostbackAction(
                label='全球新聞',
                display_text='我要看全球新聞' if action_type == "news" else '分析全球新聞情緒',
                data=f'action={action_type}&cat=global'
            )
        ),
        QuickReplyButton(
            action=PostbackAction(
                label='股市新聞',
                display_text='我要看股市新聞' if action_type == "news" else '分析股市新聞情緒',
                data=f'action={action_type}&cat=stock'
            )
        ),
        QuickReplyButton(
            action=PostbackAction(
                label='社會新聞',
                display_text='我要看社會新聞' if action_type == "news" else '分析社會新聞情緒',
                data=f'action={action_type}&cat=social'
            )
        ),
        QuickReplyButton(
            action=PostbackAction(
                label='產經新聞',
                display_text='我要看產經新聞' if action_type == "news" else '分析產經新聞情緒',
                data=f'action={action_type}&cat=econ'
            )
        ),
    ])


# ===================================
# 文字雲 + 詞頻柱狀圖
# ===================================
def generate_wordcloud_for_chat(chat_id, category_key=None):
    """
    根據 chat_id 的已看過標題產生：
    - 詞頻柱狀圖
    - 文字雲
    回傳 (freq_image_url, wordcloud_image_url)
    若沒有資料則回傳 (None, None)
    """
    chat_seen = seen_titles_state.get(chat_id)
    if not chat_seen:
        print(f"[wordcloud] chat_id={chat_id} 尚未有任何標題")
        return (None, None)

    titles = []

    if category_key:
        titles = chat_seen.get(category_key, [])
    else:
        if 'all' in chat_seen:
            titles = chat_seen['all']
        else:
            for _, arr in chat_seen.items():
                titles.extend(arr)

    if not titles:
        print(f"[wordcloud] chat_id={chat_id}, category={category_key} 沒有標題可用")
        return (None, None)

    if not os.path.exists(WORDCLOUD_FONT_PATH):
        print(f"[wordcloud] 字型檔不存在: {WORDCLOUD_FONT_PATH}")
        return (None, None)

    # 斷詞
    all_titles = "。".join(titles)
    words = list(jieba.cut(all_titles, cut_all=False))
    clean_words = [w.strip() for w in words if len(w.strip()) >= 2]

    os.makedirs(static_tmp_path, exist_ok=True)

    # ===== 詞頻柱狀圖 =====
    freq_image_url = None
    if clean_words:
        counter = Counter(clean_words)
        top_n = 15
        most_common = counter.most_common(top_n)

        labels, counts = zip(*most_common)

        font_prop = FontProperties(fname=WORDCLOUD_FONT_PATH)

        plt.figure(figsize=(8, 6))
        y_pos = range(len(labels))
        plt.barh(y_pos, counts)
        plt.yticks(y_pos, labels, fontproperties=font_prop)
        plt.xlabel('詞頻', fontproperties=font_prop)
        plt.title('熱門關鍵詞', fontproperties=font_prop)
        plt.gca().invert_yaxis()
        plt.tight_layout()

        freq_filename = f'freq_{chat_id}'
        if category_key:
            freq_filename += f'_{category_key}'
        freq_filename += f'_{int(time.time())}.png'

        freq_filepath = os.path.join(static_tmp_path, freq_filename)
        plt.savefig(freq_filepath)
        plt.close()

        base_url = request.url_root.rstrip('/')
        freq_image_url = f"{base_url}/static/tmp/{freq_filename}"

        print(f"[freq] chat_id={chat_id}, category={category_key}, image_url={freq_image_url}")
    else:
        print(f"[freq] chat_id={chat_id}, category={category_key} 無足夠詞彙產生柱狀圖")

    # ===== 文字雲 =====
    wc_text = " ".join(clean_words) if clean_words else " ".join(words)

    wc = WordCloud(
        font_path=WORDCLOUD_FONT_PATH,
        width=800,
        height=600,
        background_color="white"
    ).generate(wc_text)

    wc_filename = f'wordcloud_{chat_id}'
    if category_key:
        wc_filename += f'_{category_key}'
    wc_filename += f'_{int(time.time())}.png'

    wc_filepath = os.path.join(static_tmp_path, wc_filename)
    wc.to_file(wc_filepath)

    base_url = request.url_root.rstrip('/')
    wc_image_url = f"{base_url}/static/tmp/{wc_filename}"

    print(f"[wordcloud] chat_id={chat_id}, category={category_key}, image_url={wc_image_url}")
    return (freq_image_url, wc_image_url)


# ===================================
# 情緒分析
# ===================================
def analyze_sentiment_for_chat(chat_id, category_key):
    """
    對某個聊天室 + 類別已累積的標題做簡單情緒分析
    回傳 (total, pos, neg, neu, overall_label) or None
    """
    chat_seen = seen_titles_state.get(chat_id)
    if not chat_seen:
        return None

    titles = chat_seen.get(category_key, [])
    if not titles:
        return None

    pos = neg = neu = 0

    for title in titles:
        score = 0
        for w in POSITIVE_WORDS:
            if w in title:
                score += 1
        for w in NEGATIVE_WORDS:
            if w in title:
                score -= 1

        if score > 0:
            pos += 1
        elif score < 0:
            neg += 1
        else:
            neu += 1

    total = pos + neg + neu
    if total == 0:
        return None

    if pos > neg:
        label = "整體偏「正向」🙂"
    elif neg > pos:
        label = "整體偏「負向」☹️"
    else:
        label = "整體「中立」😐"

    return (total, pos, neg, neu, label)


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
# 新好友加入（FollowEvent）→ 介紹 + 類別選單
# ==========================
@handler.add(FollowEvent)
def handle_follow(event):
    intro_text = (
        "嗨，我是你的「新聞內容助理」📊📈\n\n"
        "我可以幫你：\n"
        "1️⃣ 查看【運動、全球、股市、社會、產經】的最新新聞（每次 5 則），"
        "同一類別可以往後看 6～10、11～15 ...。\n"
        "2️⃣ 根據你看過的新聞標題，做詞頻柱狀圖＋文字雲，幫你做簡單的文字探勘分析。\n\n"
        "之後你只要跟我說「我想看新聞」或任何訊息，我都會請你先選擇新聞類別 😊"
    )
    msg1 = TextSendMessage(text=intro_text)

    msg2 = TextSendMessage(
        text='請先選擇想看的新聞類別：',
        quick_reply=build_category_quick_reply(action_type="news")
    )

    line_bot_api.reply_message(event.reply_token, [msg1, msg2])


# ==========================
# 處理文字訊息（關鍵字變得比較彈性）
# ==========================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    try:
        user_text = event.message.text.strip()
        normalized = user_text.replace(" ", "")
        chat_id = get_chat_id(event)

        # ===== 文字雲相關指令 =====
        # 只要提到「文字雲 / 字雲 / 雲圖 / 關鍵字圖 / 關鍵字」其中一個就觸發
        if any(kw in normalized for kw in ["文字雲", "字雲", "雲圖", "關鍵字圖", "關鍵字"]):
            category_key = detect_category_from_text(normalized)

            freq_url, image_url = generate_wordcloud_for_chat(chat_id, category_key)

            if not image_url:
                if category_key:
                    cname = CATEGORIES[category_key]['name']
                    msg = f'目前還沒有任何「{cname}新聞」的標題可以做文字雲，請先多看幾則 {cname} 新聞喔！'
                else:
                    msg = '你目前還沒有看過任何新聞（或尚未累積足夠標題），請先點選各類別新聞按鈕喔！'
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
                return

            messages = []
            if freq_url:
                messages.append(ImageSendMessage(
                    original_content_url=freq_url,
                    preview_image_url=freq_url
                ))
            messages.append(ImageSendMessage(
                original_content_url=image_url,
                preview_image_url=image_url
            ))

            line_bot_api.reply_message(event.reply_token, messages)
            return

        # ===== 情緒分析相關指令 =====
        if ("情緒分析" in normalized) or ("情緒" in normalized and "分析" in normalized) \
           or ("心情" in normalized and "分析" in normalized):

            # 先看看句子裡有沒有提到類別
            category_key = detect_category_from_text(normalized)

            if category_key:
                cname = CATEGORIES[category_key]['name']
                result = analyze_sentiment_for_chat(chat_id, category_key)

                if not result:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(
                            text=f'目前還沒有足夠的「{cname}新聞」標題可以做情緒分析，請先多看幾則 {cname} 新聞喔！'
                        )
                    )
                    return

                total, pos, neg, neu, label = result
                reply_text = (
                    f'【{cname}新聞 情緒分析】\n'
                    f'目前已累積標題數：{total} 則\n\n'
                    f'🙂 正向：{pos} 則\n'
                    f'☹️ 負向：{neg} 則\n'
                    f'😐 中立：{neu} 則\n\n'
                    f'➡️ {label}'
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                return
            else:
                # 沒說哪一類 → 先請他選類別
                msg = TextSendMessage(
                    text='請問你要做哪一個類別的情緒分析呢？',
                    quick_reply=build_category_quick_reply(action_type="sentiment")
                )
                line_bot_api.reply_message(event.reply_token, msg)
                return

        # ===== 其他文字 → 類別選擇泡泡（看新聞） =====
        msg = TextSendMessage(
            text='請選擇想看的新聞類別：',
            quick_reply=build_category_quick_reply(action_type="news")
        )
        line_bot_api.reply_message(event.reply_token, msg)

    except Exception as e:
        print("[handle_text_message] error:", traceback.format_exc())
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='程式發生錯誤，請查看伺服器 LOG。')
        )


# ==========================
# 處理 Postback（按下各類新聞 / 情緒分析按鈕）
# ==========================
@handler.add(PostbackEvent)
def handle_postback(event):
    try:
        data = event.postback.data
        print(f"[Postback] raw data = {data}")

        params = parse_qs(data)
        action = params.get('action', [''])[0]
        chat_id = get_chat_id(event)

        # ===== 看新聞 =====
        if action == 'news':
            category_key = params.get('cat', [''])[0]
            if category_key not in CATEGORIES:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text='未知的新聞類別，請重新選擇。')
                )
                return

            cname = CATEGORIES[category_key]['name']

            chat_state = news_page_state.get(chat_id, {})
            current_page = chat_state.get(category_key, 1)
            print(f"[news] chat_id={chat_id}, category={category_key}, current_page={current_page}")

            news_list = scrape_udn_category(category_key)

            if not news_list:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'目前無法取得{cname}新聞，請稍後再試。')
                )
                return

            start_idx = (current_page - 1) * PAGE_SIZE
            end_idx = current_page * PAGE_SIZE
            page_items = news_list[start_idx:end_idx]

            if not page_items:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'{cname}新聞已經沒有更多最新內容了，我幫你從第一頁重新開始喔！')
                )
                chat_state[category_key] = 1
                news_page_state[chat_id] = chat_state
                seen_titles_state[chat_id] = {}
                return

            # 累積標題（給文字雲 / 情緒分析）
            chat_seen = seen_titles_state.get(chat_id, {})
            all_list = chat_seen.get('all', [])
            cat_list = chat_seen.get(category_key, [])

            for row in page_items:
                all_list.append(row['標題'])
                cat_list.append(row['標題'])

            chat_seen['all'] = all_list
            chat_seen[category_key] = cat_list
            seen_titles_state[chat_id] = chat_seen

            print(
                f"[news] chat_id={chat_id}, category={category_key}, "
                f"累積全部標題數={len(all_list)}, 該類別標題數={len(cat_list)}"
            )

            # 將本頁 5 則新聞組成一個文字框
            lines = []
            for i, row in enumerate(page_items, start=start_idx + 1):
                block = f"{cname}新聞 第{i} 則\n{row['標題']}\n{row['連結']}"
                lines.append(block)

            reply_text = "\n\n".join(lines)

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )

            chat_state[category_key] = current_page + 1
            news_page_state[chat_id] = chat_state
            return

        # ===== 情緒分析 =====
        if action == 'sentiment':
            category_key = params.get('cat', [''])[0]
            if category_key not in CATEGORIES:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text='未知的新聞類別，請重新選擇。')
                )
                return

            cname = CATEGORIES[category_key]['name']
            result = analyze_sentiment_for_chat(chat_id, category_key)

            if not result:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f'目前還沒有足夠的「{cname}新聞」標題可以做情緒分析，請先多看幾則 {cname} 新聞喔！'
                    )
                )
                return

            total, pos, neg, neu, label = result
            reply_text = (
                f'【{cname}新聞 情緒分析】\n'
                f'目前已累積標題數：{total} 則\n\n'
                f'🙂 正向：{pos} 則\n'
                f'☹️ 負向：{neg} 則\n'
                f'😐 中立：{neu} 則\n\n'
                f'➡️ {label}'
            )

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
            return

        # 其他未定義 action
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='這個功能尚未支援唷！')
        )

    except Exception as e:
        print("[handle_postback] error:", traceback.format_exc())
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='處理 Postback 時發生錯誤，請查看伺服器 LOG。')
        )


# ==========================
# 歡迎新成員加入群組
# ==========================
@handler.add(MemberJoinedEvent)
def welcome_group_member(event):
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
