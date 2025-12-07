from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *

import os
import time
import traceback
from urllib.parse import parse_qs
from collections import Counter

# 爬蟲
import requests
from bs4 import BeautifulSoup

# 文字雲 / 圖表
from wordcloud import WordCloud
import jieba
import matplotlib

matplotlib.use("Agg")  # Render 上沒有 GUI
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

app = Flask(__name__)
static_tmp_path = os.path.join(os.path.dirname(__file__), "static", "tmp")

# LINE Channel 設定
line_bot_api = LineBotApi(os.getenv("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("CHANNEL_SECRET"))

# ===================================
# UDN 各類新聞設定
# ===================================
BASE_URL = "https://udn.com"

CATEGORIES = {
    "sports": {"name": "運動", "url": "https://udn.com/news/cate/2/7227"},
    "global": {"name": "全球", "url": "https://udn.com/news/cate/2/7225"},
    "stock": {"name": "股市", "url": "https://udn.com/news/cate/2/6645"},
    "social": {"name": "社會", "url": "https://udn.com/news/cate/2/6639"},
    "econ": {"name": "產經", "url": "https://udn.com/news/cate/2/6644"},
}

# 類別對應關鍵字（讓問法比較彈性）
CATEGORY_KEYWORDS = {
    "sports": ["運動", "體育", "sports"],
    "global": ["全球", "國際", "國外", "world"],
    "stock": ["股市", "股票", "股價", "股", "stock"],
    "social": ["社會", "社會新聞"],
    "econ": ["產經", "財經", "經濟", "產業"],
}

PAGE_SIZE = 5

# { chat_id: { category: page_int } }
news_page_state = {}

# { chat_id: { 'all': [...], 'sports': [...], ... } }
seen_titles_state = {}

# 最近一批爬到的新聞（給摘要用）
# { chat_id: { category: [ {標題, 連結}, ... ] } }
last_news_cache = {}

# 字型
WORDCLOUD_FONT_PATH = os.path.join(os.path.dirname(__file__), "msjh.ttc")

# 簡單情緒字典
POSITIVE_WORDS = [
    "成長",
    "獲利",
    "創高",
    "創新高",
    "利多",
    "看好",
    "獎",
    "奪冠",
    "勝",
    "大勝",
    "飆升",
    "上漲",
    "暢旺",
    "樂觀",
    "改善",
    "突破",
    "熱烈",
    "亮眼",
]
NEGATIVE_WORDS = [
    "下跌",
    "重挫",
    "暴跌",
    "虧損",
    "災",
    "意外",
    "火警",
    "颱風",
    "地震",
    "暴雨",
    "死亡",
    "罹難",
    "警告",
    "風險",
    "衰退",
    "負成長",
    "爆炸",
    "暴力",
    "侵害",
    "詐騙",
]

# ===================================
# 工具函式
# ===================================


def get_chat_id(event):
    source = event.source
    if isinstance(source, SourceUser):
        return source.user_id
    elif isinstance(source, SourceGroup):
        return source.group_id
    elif isinstance(source, SourceRoom):
        return source.room_id
    return "unknown"


def detect_category_from_text(text):
    """從使用者輸入中猜測新聞類別，回傳 category_key 或 None"""
    for key, kw_list in CATEGORY_KEYWORDS.items():
        for kw in kw_list:
            if kw in text:
                return key
    return None


def cn_num_to_int(s):
    """簡單把一到二十的中文數字轉成整數，其他回傳 None"""
    mapping = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if s.isdigit():
        return int(s)

    if s == "十":
        return 10
    if len(s) == 2 and s[0] == "十" and s[1] in mapping:
        return 10 + mapping[s[1]]
    if len(s) == 2 and s[1] == "十" and s[0] in mapping:
        return mapping[s[0]] * 10
    if len(s) == 3 and s[1] == "十" and s[0] in mapping and s[2] in mapping:
        return mapping[s[0]] * 10 + mapping[s[2]]
    if s in mapping:
        return mapping[s]
    return None


def parse_summary_request(text):
    """
    解析「第幾則摘要」的需求。
    支援：
      - 股市新聞第3則摘要
      - 我想看全球第10則的重點
      - 告訴我社會第十五則新聞摘要
    回傳 (category_key, index) 或 (None, None)
    """
    import re

    cat_key = detect_category_from_text(text)
    if not cat_key:
        return None, None

    # 找「第X則」或「第X條」
    m = re.search(r"第([一二三四五六七八九十0-9]+)[則条條篇筆]", text)
    if not m:
        return None, None

    idx_str = m.group(1)
    idx = cn_num_to_int(idx_str)
    if not idx or idx <= 0:
        return None, None

    return cat_key, idx


# ===================================
# 爬 UDN 單一類別
# ===================================


def scrape_udn_category(category_key):
    if category_key not in CATEGORIES:
        return []

    url = CATEGORIES[category_key]["url"]
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

    soup = BeautifulSoup(resp.text, "html.parser")
    news_elements = soup.select("div.story-list__text a")

    data = []
    for element in news_elements:
        title = element.get_text(strip=True)
        href = element.get("href")

        if not title or not href:
            continue

        if href.startswith("/"):
            href = BASE_URL + href

        if "udn.com" not in href:
            continue

        data.append({"標題": title, "連結": href})

    print(f"[爬蟲] {category_key} 共取得 {len(data)} 筆資料")
    return data


# ===================================
# 摘要相關
# ===================================


def summarize_article(url, max_sentences=3):
    """超簡單版摘要：抓文章內文前幾句"""
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
        print("❌ 取得文章內容失敗：", e)
        return "這則新聞的內文暫時讀取失敗，請稍後再試。"

    soup = BeautifulSoup(resp.text, "html.parser")
    # 這裡用比較寬鬆的選擇器
    article = soup.select_one("section.article-content, div#story_body_content, div.story_bady")

    if not article:
        text = soup.get_text(separator="。", strip=True)
    else:
        text = article.get_text(separator="。", strip=True)

    # 用「。」當句號
    sentences = [s for s in text.split("。") if s.strip()]
    if not sentences:
        return "這則新聞內文字數過少，暫時無法產生摘要。"

    summary = "。".join(sentences[:max_sentences])
    if not summary.endswith("。"):
        summary += "。"
    return summary


def get_nth_news(chat_id, category_key, index):
    """
    從 cache / 重新爬，取得第 index 則新聞(dict)；找不到回傳 None
    index：1-based
    """
    news_by_chat = last_news_cache.get(chat_id, {})
    news_list = news_by_chat.get(category_key)

    if not news_list:
        # 沒有 cache 就重新爬一次
        news_list = scrape_udn_category(category_key)
        if not news_list:
            return None
        if chat_id not in last_news_cache:
            last_news_cache[chat_id] = {}
        last_news_cache[chat_id][category_key] = news_list

    if index <= 0 or index > len(news_list):
        return None
    return news_list[index - 1]


# ===================================
# QuickReply
# ===================================


def build_category_quick_reply():
    return QuickReply(
        items=[
            QuickReplyButton(
                action=PostbackAction(
                    label="運動新聞",
                    display_text="我要看運動新聞",
                    data="action=news&cat=sports",
                )
            ),
            QuickReplyButton(
                action=PostbackAction(
                    label="全球新聞",
                    display_text="我要看全球新聞",
                    data="action=news&cat=global",
                )
            ),
            QuickReplyButton(
                action=PostbackAction(
                    label="股市新聞",
                    display_text="我要看股市新聞",
                    data="action=news&cat=stock",
                )
            ),
            QuickReplyButton(
                action=PostbackAction(
                    label="社會新聞",
                    display_text="我要看社會新聞",
                    data="action=news&cat=social",
                )
            ),
            QuickReplyButton(
                action=PostbackAction(
                    label="產經新聞",
                    display_text="我要看產經新聞",
                    data="action=news&cat=econ",
                )
            ),
        ]
    )


# ===================================
# 文字雲 + 詞頻柱狀圖
# ===================================


def generate_wordcloud_for_chat(chat_id, category_key=None):
    """
    回傳 (freq_image_url, wordcloud_image_url)
    沒資料 → (None, None)
    """
    chat_seen = seen_titles_state.get(chat_id)
    if not chat_seen:
        print(f"[wordcloud] chat_id={chat_id} 尚未有任何標題")
        return (None, None)

    if category_key:
        titles = chat_seen.get(category_key, [])
    else:
        titles = chat_seen.get("all", [])
        if not titles:
            titles = []
            for k, arr in chat_seen.items():
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

    # 詞頻柱狀圖
    freq_image_url = None
    if clean_words:
        counter = Counter(clean_words)
        most_common = counter.most_common(15)
        labels, counts = zip(*most_common)
        font_prop = FontProperties(fname=WORDCLOUD_FONT_PATH)

        plt.figure(figsize=(8, 6))
        y_pos = range(len(labels))
        plt.barh(y_pos, counts)
        plt.yticks(y_pos, labels, fontproperties=font_prop)
        plt.xlabel("詞頻", fontproperties=font_prop)
        plt.title("熱門關鍵詞", fontproperties=font_prop)
        plt.gca().invert_yaxis()
        plt.tight_layout()

        freq_filename = f"freq_{chat_id}"
        if category_key:
            freq_filename += f"_{category_key}"
        freq_filename += f"_{int(time.time())}.png"

        freq_path = os.path.join(static_tmp_path, freq_filename)
        plt.savefig(freq_path)
        plt.close()

        base = request.url_root.rstrip("/")
        freq_image_url = f"{base}/static/tmp/{freq_filename}"

    # 文字雲
    wc_text = " ".join(clean_words) if clean_words else " ".join(words)

    wc = WordCloud(
        font_path=WORDCLOUD_FONT_PATH, width=800, height=600, background_color="white"
    ).generate(wc_text)

    wc_filename = f"wordcloud_{chat_id}"
    if category_key:
        wc_filename += f"_{category_key}"
    wc_filename += f"_{int(time.time())}.png"
    wc_path = os.path.join(static_tmp_path, wc_filename)
    wc.to_file(wc_path)

    base = request.url_root.rstrip("/")
    wc_image_url = f"{base}/static/tmp/{wc_filename}"

    return (freq_image_url, wc_image_url)


# ===================================
# 情緒分析
# ===================================


def analyze_sentiment_for_chat(chat_id, category_key):
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

    # 用於顯示小註解
    if pos > neg and pos >= neu:
        trend = "偏正向"
    elif neg > pos and neg >= neu:
        trend = "偏負向"
    elif pos == neg and pos > neu:
        trend = "雙極化"
    else:
        trend = "中立"

    return {"total": total, "pos": pos, "neg": neg, "neu": neu, "trend": trend}


def analyze_overall_sentiment_for_chat(chat_id):
    """
    對目前有資料的所有類別做情緒分析，比較用
    回傳文字，沒有資料回傳 None
    """
    lines = ["【五大新聞情緒比較】"]

    chat_seen = seen_titles_state.get(chat_id)
    if not chat_seen:
        return None

    has_any = False
    for key, info in CATEGORIES.items():
        result = analyze_sentiment_for_chat(chat_id, key)
        if not result:
            continue
        has_any = True
        name = info["name"]
        pos = result["pos"]
        neg = result["neg"]
        neu = result["neu"]
        trend = result["trend"]
        line = f"{name}：🙂 {pos} / ☹️ {neg} / 😐 {neu}（{trend}）"
        lines.append(line)

    if not has_any:
        return None

    return "\n".join(lines)


# ===================================
# Flask / LINE Webhook
# ===================================


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


# 新好友加入
@handler.add(FollowEvent)
def handle_follow(event):
    intro_text = (
        "嗨，我是你的「新聞內容助理」📊📈\n\n"
        "我可以幫你：\n"
        "1️⃣ 查看【運動、全球、股市、社會、產經】最新新聞（每次 5 則），同一類別可以往後看 6～10、11～15...\n"
        "2️⃣ 依照你看過的新聞標題，做詞頻柱狀圖＋文字雲，幫你做簡單文字探勘分析。\n"
        "3️⃣ 幫你統計各類新聞的情緒（正向 / 負向 / 中立）。\n\n"
        "之後你只要說「我想看新聞」「幫我做情緒分析」「幫我生成文字雲」之類的，我都會陪你一起玩資料 😄"
    )
    msg1 = TextSendMessage(text=intro_text)
    msg2 = TextSendMessage(
        text="請先選擇想看的新聞類別：", quick_reply=build_category_quick_reply()
    )
    line_bot_api.reply_message(event.reply_token, [msg1, msg2])


# 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    try:
        user_text = event.message.text.strip()
        norm = user_text.replace(" ", "").lower()
        chat_id = get_chat_id(event)

        # ---- 感謝訊息回覆 ----
        thank_keywords = ["謝謝", "感謝", "thankyou", "thanks", "thx"]
        if any(k in norm for k in thank_keywords):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="不客氣～😊 歡迎下次再來跟我一起看新聞、做分析！"),
            )
            return

        # ---- 摘要需求 ----
        if "摘要" in user_text or "重點" in user_text:
            cat_key, idx = parse_summary_request(user_text)
            if not cat_key or not idx:
                example = "例如：「請幫我看股市新聞第3則摘要」「告訴我全球新聞第10則重點」"
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"想看哪一則新聞的摘要呢？\n{example}"),
                )
                return

            news = get_nth_news(chat_id, cat_key, idx)
            if not news:
                cname = CATEGORIES[cat_key]["name"]
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f"找不到「{cname}新聞」第 {idx} 則，可能目前資料還不夠或編號超出範圍。"
                    ),
                )
                return

            summary = summarize_article(news["連結"])
            cname = CATEGORIES[cat_key]["name"]
            reply = (
                f"【{cname}新聞 第{idx} 則摘要】\n"
                f"{news['標題']}\n{news['連結']}\n\n"
                f"🔎 摘要：\n{summary}"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        # ---- 文字雲 ----
        if ("文字雲" in user_text) or ("wordcloud" in norm):
            cat_key = detect_category_from_text(user_text)
            freq_url, wc_url = generate_wordcloud_for_chat(chat_id, cat_key)

            if not wc_url:
                if cat_key:
                    cname = CATEGORIES[cat_key]["name"]
                    msg = f"目前還沒有任何「{cname}新聞」標題可以做文字雲，請先多看幾則 {cname} 新聞喔！"
                else:
                    msg = "你目前還沒有看過任何新聞（或尚未累積足夠標題），請先點選各類別新聞按鈕喔！"
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text=msg)
                )
                return

            msgs = []
            if freq_url:
                msgs.append(
                    ImageSendMessage(
                        original_content_url=freq_url, preview_image_url=freq_url
                    )
                )
            msgs.append(
                ImageSendMessage(
                    original_content_url=wc_url, preview_image_url=wc_url
                )
            )
            line_bot_api.reply_message(event.reply_token, msgs)
            return

        # ---- 情緒分析 ----
        if ("情緒" in user_text) and ("析" in user_text or "分析" in user_text):
            cat_key = detect_category_from_text(user_text)

            # 有指定類別 → 單一類
            if cat_key:
                result = analyze_sentiment_for_chat(chat_id, cat_key)
                cname = CATEGORIES[cat_key]["name"]
                if not result:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(
                            text=f"目前還沒有足夠的「{cname}新聞」標題可以做情緒分析，請先多看幾則 {cname} 新聞喔！"
                        ),
                    )
                    return
                r = result
                reply = (
                    f"【{cname}新聞 情緒分析】\n"
                    f"目前已累積標題數：{r['total']} 則\n\n"
                    f"🙂 正向：{r['pos']} 則\n"
                    f"☹️ 負向：{r['neg']} 則\n"
                    f"😐 中立：{r['neu']} 則\n\n"
                    f"➡️ 整體{r['trend']}"
                )
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text=reply)
                )
                return

            # 沒指定類別 → 整體比較
            overall = analyze_overall_sentiment_for_chat(chat_id)
            if not overall:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="你目前看過的各類新聞標題還不夠多，暫時無法做情緒比較，先多看幾則新聞再來吧！"
                    ),
                )
                return

            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=overall)
            )
            return

        # ---- 其他文字 → 類別選單 ----
        msg = TextSendMessage(
            text="請選擇想看的新聞類別：", quick_reply=build_category_quick_reply()
        )
        line_bot_api.reply_message(event.reply_token, msg)

    except Exception:
        print("[handle_text_message] error:", traceback.format_exc())
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text="程式發生錯誤，請查看伺服器 LOG。")
        )


# Postback：看新聞
@handler.add(PostbackEvent)
def handle_postback(event):
    try:
        data = event.postback.data
        print(f"[Postback] raw data = {data}")
        params = parse_qs(data)
        action = params.get("action", [""])[0]
        chat_id = get_chat_id(event)

        if action == "news":
            category_key = params.get("cat", [""])[0]
            if category_key not in CATEGORIES:
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text="未知的新聞類別，請重新選擇。")
                )
                return

            cname = CATEGORIES[category_key]["name"]
            chat_state = news_page_state.get(chat_id, {})
            current_page = chat_state.get(category_key, 1)
            print(
                f"[news] chat_id={chat_id}, category={category_key}, current_page={current_page}"
            )

            news_list = scrape_udn_category(category_key)
            if not news_list:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"目前無法取得{cname}新聞，請稍後再試。"),
                )
                return

            # 更新 cache（給摘要用）
            if chat_id not in last_news_cache:
                last_news_cache[chat_id] = {}
            last_news_cache[chat_id][category_key] = news_list

            start_idx = (current_page - 1) * PAGE_SIZE
            end_idx = current_page * PAGE_SIZE
            page_items = news_list[start_idx:end_idx]

            if not page_items:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f"{cname}新聞已經沒有更多最新內容了，我幫你從第一頁重新開始喔！"
                    ),
                )
                chat_state[category_key] = 1
                news_page_state[chat_id] = chat_state
                seen_titles_state[chat_id] = {}
                return

            # 累積標題（給文字雲 / 情緒分析）
            chat_seen = seen_titles_state.get(chat_id, {})
            all_list = chat_seen.get("all", [])
            cat_list = chat_seen.get(category_key, [])

            for row in page_items:
                all_list.append(row["標題"])
                cat_list.append(row["標題"])

            chat_seen["all"] = all_list
            chat_seen[category_key] = cat_list
            seen_titles_state[chat_id] = chat_seen

            lines = []
            for i, row in enumerate(page_items, start=start_idx + 1):
                block = f"{cname}新聞 第{i} 則\n{row['標題']}\n{row['連結']}"
                lines.append(block)
            reply_text = "\n\n".join(lines)

            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text=reply_text)
            )

            chat_state[category_key] = current_page + 1
            news_page_state[chat_id] = chat_state
            return

        # 其他 action
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text="這個功能尚未支援唷！")
        )

    except Exception:
        print("[handle_postback] error:", traceback.format_exc())
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="處理 Postback 時發生錯誤，請查看伺服器 LOG。"),
        )


# 群組有新成員
@handler.add(MemberJoinedEvent)
def welcome_group_member(event):
    uid = event.joined.members[0].user_id
    gid = event.source.group_id
    profile = line_bot_api.get_group_member_profile(gid, uid)
    name = profile.display_name
    message = TextSendMessage(text=f"{name} 歡迎加入！")
    line_bot_api.reply_message(event.reply_token, message)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
