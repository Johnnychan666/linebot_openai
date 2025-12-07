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
import re
# ====== python 的函數庫 ==========

# ====== 靜態爬蟲相關套件 ==========
import requests
from bs4 import BeautifulSoup
# ====== 靜態爬蟲相關套件 ==========

# ====== 文字雲 / 圖表相關套件 ======
from wordcloud import WordCloud
import jieba
import matplotlib
matplotlib.use("Agg")  # 無 GUI 環境用
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
# ====== 文字雲 / 圖表相關套件 ======


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

# 每次按按鈕顯示幾則
PAGE_SIZE = 5

# 紀錄每個聊天、每個類別目前看到第幾頁
# 結構：{ chat_id: { category_key: page_int } }
news_page_state = {}

# 紀錄每個聊天、每個類別「已看過的標題」，給文字雲 / 情緒分析 / 摘要用
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

# ====== 摘要觸發關鍵字（可自由問法） ======
SUMMARY_TRIGGERS = [
    "摘要", "重點", "大綱", "簡述", "簡介", "簡要說明",
    "講什麼", "說什麼", "在講什麼", "在說什麼",
    "大概在講什麼", "大概內容", "內容大意", "summary"
]

# ====== 感謝關鍵字 ======
THANK_TRIGGERS = [
    "謝謝", "感謝", "感恩", "thanks", "thank you", "thx"
]


# ===================================
# 爬蟲：抓指定類別的新聞列表（靜態）
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

    # ====== 準備資料：斷詞 ======
    all_titles = "。".join(titles)
    words = list(jieba.cut(all_titles, cut_all=False))

    # 去掉太短或空白的詞
    clean_words = [w.strip() for w in words if len(w.strip()) >= 2]

    os.makedirs(static_tmp_path, exist_ok=True)

    # ====== 產生詞頻柱狀圖 ======
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

    # ====== 產生文字雲 ======
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


# ===================================
# 摘要相關工具：中文數字 & index 解析
# ===================================
def chinese_numeral_to_int(s: str):
    """
    只處理 1~99 的簡單中文數字：一二三四五六七八九十、兩、零
    例如：'三' -> 3, '十' -> 10, '十三' -> 13, '三十' -> 30, '三十五' -> 35
    """
    digit_map = {
        "零": 0, "〇": 0,
        "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9
    }

    s = s.strip()
    if not s:
        return None

    # 單獨「十」
    if s == "十":
        return 10

    # 有「十」的情況
    if "十" in s:
        parts = s.split("十")
        # '十X' → 10 + X
        if parts[0] == "":
            tens = 1
        else:
            tens = digit_map.get(parts[0], 0)

        ones = 0
        if len(parts) > 1 and parts[1] != "":
            ones = digit_map.get(parts[1], 0)

        val = tens * 10 + ones
        return val if val > 0 else None

    # 沒有「十」，視為個位數
    if len(s) == 1:
        return digit_map.get(s, None)

    return None


def extract_index_from_text(text: str):
    """
    從句子裡抓出「第幾則」：
    - 支援中文數字：第十則、第十三則、第三則…
    - 支援阿拉伯數字：第10則、第3則…
    """
    # 中文數字
    m = re.search(r'第\s*([一二兩三四五六七八九十〇零]+)\s*[則条條篇筆項篇條]?', text)
    if m:
        cn = m.group(1)
        idx = chinese_numeral_to_int(cn)
        if idx is not None:
            return idx

    # 阿拉伯數字「第10則」
    m = re.search(r'第\s*(\d+)\s*[則条條篇筆項篇條]?', text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # 備援：抓句子裡第一個阿拉伯數字
    m = re.search(r'(\d+)', text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    return None


def extract_category_from_text(text: str):
    """
    嘗試從使用者輸入中判斷是哪個新聞類別
    例如：股市新聞 / 股市 / 股票 / 全球 / 國際 / 財經 / 產業...
    """
    # 先看正式名稱
    for key, info in CATEGORIES.items():
        name = info['name']
        if name in text or (name + "新聞") in text:
            return key

    # 再看一些別名
    synonyms = {
        "體育": "sports",
        "國際": "global",
        "國外": "global",
        "股票": "stock",
        "股價": "stock",
        "財經": "econ",
        "經濟": "econ",
        "產業": "econ",
    }
    for kw, ck in synonyms.items():
        if kw in text:
            return ck

    return None


def is_summary_intent(text: str):
    """
    判斷這句話是不是「想要摘要」
    """
    for trig in SUMMARY_TRIGGERS:
        if trig in text:
            return True
    return False


def fetch_article_summary(url: str):
    """
    進入單一新聞頁，抓取內文前幾句當作簡易摘要
    """
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
        print(f"❌ 取得單篇新聞失敗：{e}")
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')

    # 嘗試幾種常見結構
    paragraphs = (
        soup.select('#story_body_content p') or
        soup.select('section.article-content__editor p') or
        soup.select('article p') or
        soup.select('p')
    )

    texts = [p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)]
    if not texts:
        return None

    full = "".join(texts)
    if len(full) > 120:
        return full[:120] + "……"
    return full


def build_news_summary_reply(category_key: str, index: int):
    """
    組出「某類別第 index 則新聞」的摘要回覆文字
    """
    if category_key not in CATEGORIES:
        return "看不懂你說的是哪個新聞類別，可以再跟我說一次嗎？"

    if index is None or index <= 0:
        return "我沒有聽清楚你說第幾則，試試看\n「請告訴我股市新聞第 3 則摘要」這種說法～"

    cname = CATEGORIES[category_key]['name']
    news_list = scrape_udn_category(category_key)
    if not news_list:
        return f"目前暫時抓不到 {cname} 新聞，稍後再試試看唷！"

    if index > len(news_list):
        return f"目前 {cname} 新聞只有 {len(news_list)} 則，我找不到第 {index} 則 QQ"

    item = news_list[index - 1]
    title = item['標題']
    url = item['連結']

    summary = fetch_article_summary(url)
    if not summary:
        return (
            f"【{cname}新聞 第 {index} 則】\n"
            f"{title}\n\n"
            f"抱歉這篇我沒有成功抓到內文，你可以點連結直接看：\n{url}"
        )

    reply_text = (
        f"【{cname}新聞 第 {index} 則摘要】\n"
        f"{title}\n\n"
        f"📝 內容大意：\n{summary}\n\n"
        f"👉 完整內文：\n{url}"
    )
    return reply_text


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
        "1️⃣ 查看【運動、全球、股市、社會、產經】的最新新聞（每次 5 則），\n"
        "   同一類別可以往後看 6～10、11～15 ...。\n"
        "2️⃣ 根據你看過的新聞標題，做詞頻柱狀圖＋文字雲，幫你做簡單的文字探勘分析。\n"
        "3️⃣ 針對某一則新聞，幫你抓出內文摘要、看看情緒是偏正向、負向還是中立。\n\n"
        "之後你只要跟我說「我想看新聞」或任何訊息，我都會請你先選擇新聞類別 😊"
    )
    msg1 = TextSendMessage(text=intro_text)

    msg2 = TextSendMessage(
        text='請先選擇想看的新聞類別：',
        quick_reply=build_category_quick_reply(action_type="news")
    )

    line_bot_api.reply_message(event.reply_token, [msg1, msg2])


# ==========================
# 處理文字訊息
# ==========================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    try:
        user_text = event.message.text.strip()
        lower_text = user_text.lower()
        chat_id = get_chat_id(event)

        # === 使用者說「謝謝 / 感謝」之類 ===
        if any(key in user_text for key in THANK_TRIGGERS):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="不客氣～很高興能幫上忙！歡迎下次再來看新聞或做分析 😊")
            )
            return

        # === 文字雲相關指令 ===
        if "文字雲" in user_text.replace(" ", ""):
            category_key = None

            if "運動" in user_text:
                category_key = 'sports'
            elif "全球" in user_text or "國際" in user_text:
                category_key = 'global'
            elif "股市" in user_text or "股票" in user_text:
                category_key = 'stock'
            elif "社會" in user_text:
                category_key = 'social'
            elif "產經" in user_text or "財經" in user_text or "產業" in user_text:
                category_key = 'econ'

            freq_url, image_url = generate_wordcloud_for_chat(chat_id, category_key)

            if not image_url:
                if category_key:
                    cname = CATEGORIES[category_key]['name']
                    msg = f'目前還沒有任何「{cname}新聞」的標題可以做文字雲，請先多看幾則 {cname} 新聞喔！'
                else:
                    msg = '你目前還沒有看過任何新聞（或尚未累積足夠標題），請先點選各類別新聞按鈕喔！'
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=msg)
                )
                return

            messages = []
            if freq_url:
                messages.append(
                    ImageSendMessage(
                        original_content_url=freq_url,
                        preview_image_url=freq_url
                    )
                )
            messages.append(
                ImageSendMessage(
                    original_content_url=image_url,
                    preview_image_url=image_url
                )
            )

            line_bot_api.reply_message(event.reply_token, messages)
            return

        # === 情緒分析指令（只要句子裡有「情緒」＋「分析 / 看」之類） ===
        if "情緒" in user_text and ("分析" in user_text or "看" in user_text or "判斷" in user_text):
            msg = TextSendMessage(
                text='請問你要做哪一個類別的情緒分析呢？',
                quick_reply=build_category_quick_reply(action_type="sentiment")
            )
            line_bot_api.reply_message(event.reply_token, msg)
            return

        # === 摘要相關 ===
        if is_summary_intent(user_text):
            cat_key = extract_category_from_text(user_text)
            index = extract_index_from_text(user_text)

            if cat_key and index:
                reply_text = build_news_summary_reply(cat_key, index)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=reply_text)
                )
                return
            else:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="想看哪一則新聞的摘要呢？可以這樣問我：\n"
                             "例如：「請告訴我股市新聞第 3 則摘要」"
                    )
                )
                return

        # === 其他文字 → 類別選擇泡泡（看新聞） ===
        msg = TextSendMessage(
            text='請選擇想看的新聞類別：',
            quick_reply=build_category_quick_reply(action_type="news")
        )

        line_bot_api.reply_message(event.reply_token, msg)

    except Exception:
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

            # 累積標題（給文字雲 / 情緒分析 / 摘要）
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

    except Exception:
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
