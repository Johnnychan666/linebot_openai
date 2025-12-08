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

# 類別關鍵字（讓使用者講話可以比較自然）
CATEGORY_ALIASES = {
    'sports': ['運動', '體育'],
    'global': ['全球', '國際'],
    'stock': ['股市', '股票'],
    'social': ['社會'],
    'econ': ['產經', '財經', '經濟']
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
    "飆升", "上漲", "暢旺", "樂觀", "改善", "突破", "熱烈", "亮眼", "好消息"
]
NEGATIVE_WORDS = [
    "下跌", "重挫", "暴跌", "虧損", "災", "意外", "火警", "颱風", "地震", "暴雨",
    "死亡", "罹難", "警告", "風險", "衰退", "負成長", "爆炸", "暴力", "侵害", "詐騙",
    "憂慮", "利空"
]


# ===================================
# 一些小工具
# ===================================
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


def detect_category_in_text(user_text):
    """
    嘗試從使用者輸入裡找出類別 key
    例如：'我想看運動新聞第三則摘要' → 'sports'
    """
    for key, aliases in CATEGORY_ALIASES.items():
        for kw in aliases:
            if kw in user_text:
                return key
    return None


def chinese_num_to_int(s):
    """
    把「三」/「十」/「十一」這種簡單中文數字轉成 int
    用不到很大的數，所以寫簡單版即可
    """
    mapping = {'零': 0, '一': 1, '二': 2, '兩': 2, '三': 3, '四': 4,
               '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    if s.isdigit():
        return int(s)

    total = 0
    if len(s) == 1:
        return mapping.get(s, None)
    # 只處理 11～19 / 20 / 21… 這類常見寫法
    if s[0] == '十':
        # 十三、十五
        unit = mapping.get(s[1], 0) if len(s) > 1 else 0
        return 10 + unit
    if s[-1] == '十':
        # 三十
        ten = mapping.get(s[0], 0)
        return ten * 10
    if '十' in s:
        idx = s.index('十')
        ten = mapping.get(s[:idx], 0)
        unit = mapping.get(s[idx+1:], 0)
        return ten * 10 + unit
    return None


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
            for key, arr in chat_seen.items():
                titles.extend(arr)

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
# 情緒分析：單一類別
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

    if pos > neg:
        label = "偏正向"
    elif neg > pos:
        label = "偏負向"
    else:
        label = "中立"

    # 雙極化：正向、負向都不少
    if pos >= 3 and neg >= 3:
        label = "雙極化"

    return (total, pos, neg, neu, label)


# ===================================
# 情緒分析：整體比較
# ===================================
def analyze_sentiment_all_categories(chat_id):
    """
    對目前有看過的各類別做情緒分析，回傳一段總結文字。
    """
    lines = ["【五大新聞情緒比較】"]
    has_any = False

    for key in CATEGORIES.keys():
        result = analyze_sentiment_for_chat(chat_id, key)
        if not result:
            continue

        has_any = True
        total, pos, neg, neu, label = result
        cname = CATEGORIES[key]['name']
        line = f"{cname}：🙂 {pos} / ☹️ {neg} / 😐 {neu}（{label}）"
        lines.append(line)

    if not has_any:
        return None

    return "\n".join(lines)


# ===================================
# 單篇新聞摘要
# ===================================
def parse_summary_request(user_text):
    """
    從句子裡抓：
      類別 + 第幾則 + 摘要
    例如：
      我想看運動新聞第三則摘要
      幫我看股市第10則新聞摘要
    回傳 (category_key, index) 或 (None, None)
    """
    if "摘要" not in user_text:
        return (None, None)

    category_key = detect_category_in_text(user_text)
    if not category_key:
        return (None, None)

    # 找「第X則」X 可以是數字或簡單中文數字
    m = re.search(r'第([0-9零一二三四五六七八九十兩]+)則', user_text)
    if not m:
        return (None, None)

    raw_num = m.group(1)
    idx = chinese_num_to_int(raw_num)
    if not idx or idx <= 0:
        return (None, None)

    return (category_key, idx)


def fetch_article_summary(category_key, index):
    """
    重新爬該類別新聞列表，抓第 index 則的連結，再去該頁面抓內文做簡單摘要
    回傳 (ok, message)
    """
    cname = CATEGORIES[category_key]['name']
    news_list = scrape_udn_category(category_key)
    if not news_list:
        return (False, f"目前暫時抓不到「{cname}」新聞，請稍後再試。")

    if index < 1 or index > len(news_list):
        return (False, f"目前「{cname}新聞」只有 {len(news_list)} 則可用，找不到第 {index} 則喔！")

    item = news_list[index - 1]
    url = item['連結']
    title = item['標題']

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
        print(f"❌ 抓內文失敗：{e}")
        return (False, f"這則新聞的內文暫時讀取不到，不好意思 > <\n可以先點連結自己看：\n{url}")

    soup = BeautifulSoup(resp.text, 'html.parser')

    # 盡量找出主要內文區塊
    paragraphs = []
    # 常見幾種寫法，盡量多抓一些
    candidates = [
        'section#story_body_content p',
        'div.article-body p',
        'div#story_body_content p',
        'article p'
    ]
    for sel in candidates:
        nodes = soup.select(sel)
        if nodes:
            for p in nodes:
                text = p.get_text(strip=True)
                if text:
                    paragraphs.append(text)
            break

    if not paragraphs:
        # 保底：全部 <p>
        for p in soup.select('p'):
            tx = p.get_text(strip=True)
            if len(tx) > 10:
                paragraphs.append(tx)

    if not paragraphs:
        return (True, f"【{cname}新聞 第{index}則】\n{title}\n\n（抱歉，內文無法解析，可以直接點原始連結觀看）\n{url}")

    full_text = " ".join(paragraphs)
    summary_len = 160
    summary = full_text[:summary_len] + ("..." if len(full_text) > summary_len else "")

    msg = (
        f"【{cname}新聞 第{index}則摘要】\n"
        f"{title}\n\n"
        f"{summary}\n\n"
        f"原文連結：{url}"
    )
    return (True, msg)


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
        "   同一類別可以往後看 6～10、11～15 ...\n"
        "2️⃣ 根據你看過的新聞標題，做詞頻柱狀圖＋文字雲，幫你做簡單的文字探勘分析。\n"
        "3️⃣ 幫你看看各類新聞大致是偏正向、負向還是中立的情緒。\n\n"
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
        chat_id = get_chat_id(event)

        # --- 感謝類：謝謝、感謝 etc. ---
        if any(kw in user_text for kw in ["謝謝", "感謝", "thank you", "thx"]):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="不客氣～很高興可以幫上忙，隨時都可以再來看新聞或做分析 😄")
            )
            return

        # --- 清空 / 重來：有「重」「清空」之類關鍵字 ---
        reset_keywords = ["重新開始", "重抓", "從頭開始", "清空紀錄", "重算", "重置", "重新抓"]
        if any(kw in user_text for kw in reset_keywords):
            cat_key = detect_category_in_text(user_text)

            # 有提到特定類別 → 重置該類別
            if cat_key:
                cname = CATEGORIES[cat_key]['name']
                chat_state = news_page_state.get(chat_id, {})
                chat_state[cat_key] = 1
                news_page_state[chat_id] = chat_state

                chat_seen = seen_titles_state.get(chat_id, {})
                # 清掉該類別的標題
                if cat_key in chat_seen:
                    del chat_seen[cat_key]
                # 全部統計一起歸零，比較單純
                chat_seen['all'] = []
                seen_titles_state[chat_id] = chat_seen

                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f"好的～已幫你把「{cname}新聞」的閱讀紀錄先歸零，"
                             f"之後這一類會從最新的第 1～5 則重新開始計算。"
                    )
                )
                return
            else:
                # 沒有提到特定類別 → 全部清空
                news_page_state.pop(chat_id, None)
                seen_titles_state.pop(chat_id, None)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="了解，我已經把你目前的新聞閱讀紀錄都整理掉囉～\n"
                             "接下來每一個類別都會從最新的第 1～5 則重新開始 👍"
                    )
                )
                return

        # --- 單篇摘要：類別 + 第X則 + 摘要 ---
        if "摘要" in user_text:
            cat_key, idx = parse_summary_request(user_text)
            if cat_key and idx:
                ok, msg = fetch_article_summary(cat_key, idx)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=msg)
                )
                return
            # 有「摘要」但沒抓到 → 給個提示
            if any(alias in user_text for aliases in CATEGORY_ALIASES.values() for alias in aliases):
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="想看哪一則新聞的摘要呢？\n"
                             "可以像這樣跟我說：\n"
                             "「我想看股市新聞第 3 則摘要」或「幫我看運動新聞第十則摘要」"
                    )
                )
                return

        # --- 文字雲相關指令 ---
        if "文字雲" in user_text:
            category_key = detect_category_in_text(user_text)

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

        # --- 情緒分析（文字訊息版）---
        if "情緒分析" in user_text:
            # 若有提到特定類別 → 單一類
            cat_key = detect_category_in_text(user_text)
            if cat_key:
                cname = CATEGORIES[cat_key]['name']
                result = analyze_sentiment_for_chat(chat_id, cat_key)
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
                    f'➡️ 整體{label}'
                )
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=reply_text)
                )
                return
            else:
                # 沒指定類別 → 做整體比較
                summary = analyze_sentiment_all_categories(chat_id)
                if not summary:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(
                            text='目前還沒有足夠的新聞標題可以做情緒分析，先多看幾則不同類別的新聞吧！'
                        )
                    )
                    return

                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=summary)
                )
                return

        # === 其他文字 → 類別選擇泡泡 ===
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
                # 清掉此聊天室的統計，重新累積
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

        # ===== 情緒分析（按鈕版：單一類別） =====
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
                f'➡️ 整體{label}'
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
