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

# 類別關鍵字（給文字判斷用）
CATEGORY_ALIASES = {
    'sports': ['運動'],
    'global': ['全球', '國際'],
    'stock': ['股市', '股票', '股價'],
    'social': ['社會'],
    'econ': ['產經', '財經', '經濟'],
}

# 每次按按鈕顯示幾則
PAGE_SIZE = 5

# 紀錄每個聊天、每個類別目前看到第幾頁
# 結構：{ chat_id: { category_key: page_int } }
news_page_state = {}

# 紀錄每個聊天、每個類別「已看過的標題」，給文字雲 / 情緒分析用
# 結構：{ chat_id: { 'sports': [...], 'global': [...], ... } }
seen_titles_state = {}

# 紀錄每個聊天最近一次抓到的完整新聞列表（給摘要用）
# 結構：{ chat_id: { category_key: [ {標題, 連結}, ... ] } }
last_news_cache = {}

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
# 類別 / 重置關鍵字解析
# ===================================
def detect_categories_from_text(norm_text):
    """從字串裡找出提到哪些新聞類別，回傳 list of category_key"""
    matched = []
    for key, aliases in CATEGORY_ALIASES.items():
        for a in aliases:
            if a in norm_text:
                matched.append(key)
                break
    return matched


def detect_reset_target(norm_text):
    """
    判斷是否是「重置指令」
    回傳：
      - 'all'      → 清空全部
      - category_key → 清空單一類別
      - None       → 不是重置指令
    """
    has_reset_word = any(w in norm_text for w in ["重新", "重抓", "重來", "重跑", "從頭", "清空"])
    if not has_reset_word:
        return None

    cats = detect_categories_from_text(norm_text)
    if cats:
        # 有重置字 + 類別字 → 重置該類
        return cats[0]

    # 沒有類別字，看看是不是「全部」相關
    if "全部" in norm_text or "全都" in norm_text or "清空紀錄" in norm_text or "清空記錄" in norm_text:
        return "all"

    # 只說「清空紀錄」也當作全部
    if "清空紀錄" in norm_text or "清空記錄" in norm_text:
        return "all"

    return None


def reset_category_for_chat(chat_id, category_key):
    """只重置某一個類別的頁數 & 標題 & 快取"""
    # page
    chat_pages = news_page_state.get(chat_id, {})
    if category_key in chat_pages:
        del chat_pages[category_key]
    news_page_state[chat_id] = chat_pages

    # seen titles
    chat_seen = seen_titles_state.get(chat_id, {})
    if category_key in chat_seen:
        del chat_seen[category_key]
    seen_titles_state[chat_id] = chat_seen

    # cache for summary
    chat_cache = last_news_cache.get(chat_id, {})
    if category_key in chat_cache:
        del chat_cache[category_key]
    last_news_cache[chat_id] = chat_cache

    print(f"[reset] chat_id={chat_id}, category={category_key} 已重置")


def reset_all_for_chat(chat_id):
    """把這個聊天室的所有類別狀態都清空"""
    news_page_state.pop(chat_id, None)
    seen_titles_state.pop(chat_id, None)
    last_news_cache.pop(chat_id, None)
    print(f"[reset] chat_id={chat_id} 全部類別已清空")


# ===================================
# Quick Reply 建立函式
# ===================================
def build_category_quick_reply(action_type="news"):
    """
    建立五個類別的 QuickReply：
    action_type:
      - "news"      → 看新聞
      - "sentiment" → 情緒分析（目前保留，文字版也可以叫）
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
        # 全部類別合併
        for key in CATEGORIES.keys():
            titles.extend(chat_seen.get(key, []))

    if not titles:
        print(f"[wordcloud] chat_id={chat_id}, category={category_key} 沒有標題可用")
        return (None, None)

    if not os.path.exists(WORDCLOUD_FONT_PATH):
        print(f"[wordcloud] 字型檔不存在: {WORDCLOUD_FONT_PATH}")
        return (None, None)

    all_titles = "。".join(titles)
    words = list(jieba.cut(all_titles, cut_all=False))
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
    回傳 (total, pos, neg, neu, label) or None
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

    # 粗略標籤
    if pos > 0 and neg > 0 and abs(pos - neg) <= 1:
        label = "整體「雙極化」😵"
    elif pos > neg:
        label = "整體偏「正向」🙂"
    elif neg > pos:
        label = "整體偏「負向」☹️"
    else:
        label = "整體「中立」😐"

    return (total, pos, neg, neu, label)


def analyze_all_sentiments_for_chat(chat_id):
    """
    對目前有資料的各類別做情緒分析，回傳 dict:
      { category_key: (total, pos, neg, neu, label), ... }
    """
    results = {}
    for key in CATEGORIES.keys():
        r = analyze_sentiment_for_chat(chat_id, key)
        if r:
            results[key] = r
    if not results:
        return None
    return results


# ===================================
# 摘要功能
# ===================================
def summarize_article(url):
    """簡單抓內文前幾句當摘要"""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[summary] 取得文章失敗: {e}")
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')

    # 嘗試幾種常見的內容容器
    paragraphs = soup.select('section#story_body_content p')
    if not paragraphs:
        paragraphs = soup.select('div.article-content__paragraph p')
    if not paragraphs:
        paragraphs = soup.select('p')

    texts = [p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)]
    if not texts:
        return None

    full = " ".join(texts)
    sentences = re.split(r'[。！？!?]', full)

    summary = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(summary) + len(s) > 120 and summary:
            break
        summary += s + "。"
        if len(summary) >= 150:
            break

    return summary or texts[0]


def try_handle_summary_request(user_text, norm_text, chat_id, event):
    """
    嘗試處理「第 N 則摘要」的需求。
    回傳 True 表示已處理並回覆。
    """
    if ("摘要" not in norm_text) and ("大意" not in norm_text) and ("summary" not in norm_text):
        return False

    m = re.search(r"第(\d+)則", norm_text)
    if not m:
        return False

    index = int(m.group(1))
    cat_keys = detect_categories_from_text(norm_text)
    if not cat_keys:
        return False

    category_key = cat_keys[0]
    cname = CATEGORIES[category_key]['name']

    chat_cache = last_news_cache.get(chat_id, {})
    news_list = chat_cache.get(category_key)

    if not news_list or index < 1 or index > len(news_list):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f'目前找不到「{cname}新聞 第{index}則」，可能你還沒看過那一頁的新聞，請先用按鈕把那一則新聞叫出來喔！'
            )
        )
        return True

    item = news_list[index - 1]
    summary = summarize_article(item['連結'])

    if not summary:
        text = (
            f"{cname}新聞 第{index}則：\n"
            f"{item['標題']}\n{item['連結']}\n\n"
            "目前暫時無法抓到摘要，請直接點連結看全文 🙏"
        )
    else:
        text = (
            f"{cname}新聞 第{index}則 摘要：\n"
            f"{item['標題']}\n\n"
            f"{summary}\n\n"
            f"原文連結：{item['連結']}"
        )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
    return True


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
        "3️⃣ 幫你做各類新聞的情緒分析，還可以看五大類情緒比較。\n"
        "4️⃣ 想看某一則新聞的摘要，也可以跟我說「股市新聞第3則摘要」。\n\n"
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
        norm = user_text.replace(" ", "").lower()
        chat_id = get_chat_id(event)

        # === 1. 重置指令判斷 ===
        reset_target = detect_reset_target(norm)
        if reset_target:
            if reset_target == "all":
                reset_all_for_chat(chat_id)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text='已經幫你把所有新聞類別的紀錄清空，下次會從最新第 1～5 則重新開始喔！')
                )
                return
            else:
                reset_category_for_chat(chat_id, reset_target)
                cname = CATEGORIES[reset_target]['name']
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'已幫你把「{cname}新聞」的紀錄清空，下次會從最新第 1～5 則開始。')
                )
                return

        # === 2. 感謝類關鍵字 ===
        if any(k in norm for k in ["謝謝", "感謝", "thx", "thanks", "thankyou", "感恩"]):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='不客氣～很高興能幫上忙！如果還想看其他新聞或做分析，隨時再叫我 😄')
            )
            return

        # === 3. 文字雲相關指令 ===
        if "文字雲" in norm:
            # 看有沒有指定類別
            cats = detect_categories_from_text(norm)
            category_key = cats[0] if cats else None

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

        # === 4. 情緒分析指令 ===
        if ("情緒分析" in norm) or ("情緒" in norm and "分析" in norm):
            cats = detect_categories_from_text(norm)

            # 若有指定類別 → 單一類別情緒分析
            if cats:
                category_key = cats[0]
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

            # 沒有指定類別 → 做「五大類情緒比較」（只列出有資料的類別）
            all_result = analyze_all_sentiments_for_chat(chat_id)
            if not all_result:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text='目前還沒有足夠的新聞標題可以做情緒分析，請先多看幾則各類新聞喔！')
                )
                return

            lines = ["【五大新聞情緒比較】"]
            for key in CATEGORIES.keys():
                if key not in all_result:
                    continue
                cname = CATEGORIES[key]['name']
                total, pos, neg, neu, label = all_result[key]

                if "雙極化" in label:
                    short = "雙極化"
                elif "正向" in label and "負向" not in label:
                    short = "偏正向"
                elif "負向" in label and "正向" not in label:
                    short = "偏負向"
                else:
                    short = "中立"

                line_txt = f"{cname}：🙂 {pos} / ☹️ {neg} / 😐 {neu}（{short}）"
                lines.append(line_txt)

            reply_text = "\n".join(lines)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
            return

        # === 5. 摘要請求 ===
        if try_handle_summary_request(user_text, norm, chat_id, event):
            return

        # === 6. 其他文字 → 類別選擇泡泡 ===
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

            # 把這次抓到的完整列表先存起來，給摘要用
            chat_cache = last_news_cache.get(chat_id, {})
            chat_cache[category_key] = news_list
            last_news_cache[chat_id] = chat_cache

            start_idx = (current_page - 1) * PAGE_SIZE
            end_idx = current_page * PAGE_SIZE
            page_items = news_list[start_idx:end_idx]

            if not page_items:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'{cname}新聞已經沒有更多最新內容了，我幫你從第一頁重新開始喔！')
                )
                # 重置該類頁數 & 相關紀錄
                reset_category_for_chat(chat_id, category_key)
                return

            # 累積標題（給文字雲 / 情緒分析）
            chat_seen = seen_titles_state.get(chat_id, {})
            cat_list = chat_seen.get(category_key, [])

            for row in page_items:
                cat_list.append(row['標題'])

            chat_seen[category_key] = cat_list
            seen_titles_state[chat_id] = chat_seen

            print(
                f"[news] chat_id={chat_id}, category={category_key}, "
                f"該類別累積標題數={len(cat_list)}"
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

        # ===== 情緒分析（從 quick reply 選特定類別） =====
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
