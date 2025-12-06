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
# ====== python 的函數庫 ==========

# ====== 靜態爬蟲相關套件 ==========
import requests
from bs4 import BeautifulSoup
# ====== 靜態爬蟲相關套件 ==========

# ====== 文字雲相關套件 ==========
from wordcloud import WordCloud
import jieba
# ====== 文字雲相關套件 ==========

# ====== 畫圖（詞頻柱狀圖）相關套件 ==========
import matplotlib
matplotlib.use('Agg')  # 伺服器無螢幕環境用這個 backend
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from collections import Counter
# ====== 畫圖相關套件 ==========


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

# 紀錄每個聊天、每個類別「已看過的標題」，給文字雲用
# 結構：{ chat_id: { 'all': [...], 'sports': [...], 'global': [...], ... } }
seen_titles_state = {}

# 使用專案根目錄的 msjh.ttc（微軟正黑體）
WORDCLOUD_FONT_PATH = os.path.join(os.path.dirname(__file__), 'msjh.ttc')


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
# 文字雲 + 詞頻柱狀圖 產生
# ===================================
def generate_wordcloud_for_chat(chat_id, category_key=None):
    """
    根據 chat_id 的已看過標題產生：
    - 詞頻 Top N 柱狀圖
    - 文字雲
    - category_key = None → 全部類別合併
    - category_key = 'sports' / 'global' / ... → 指定類別
    回傳 (freq_image_url, wordcloud_image_url)，若無資料則回傳 (None, None)
    """
    chat_seen = seen_titles_state.get(chat_id)
    if not chat_seen:
        print(f"[wordcloud] chat_id={chat_id} 尚未有任何標題")
        return (None, None)

    titles = []

    if category_key:
        # 指定類別
        titles = chat_seen.get(category_key, [])
    else:
        # 有 'all' 就直接用 'all'
        if 'all' in chat_seen:
            titles = chat_seen['all']
        else:
            # 否則把各類別加總
            for key, arr in chat_seen.items():
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
        plt.gca().invert_yaxis()  # 讓最高的在最上面
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
        font_path=WORDCLOUD_FONT_PATH,  # 使用專案內的 msjh.ttc（繁體中文）
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

    base_url = request.url_root.rstrip('/')  # e.g. https://xxx.onrender.com
    wc_image_url = f"{base_url}/static/tmp/{wc_filename}"

    print(f"[wordcloud] chat_id={chat_id}, category={category_key}, image_url={wc_image_url}")
    return (freq_image_url, wc_image_url)


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
    try:
        user_text = event.message.text.strip()
        chat_id = get_chat_id(event)

        # === 文字雲相關指令 ===
        if "文字雲" in user_text:
            category_key = None

            # 判斷是不是要某一類的文字雲
            if "運動" in user_text:
                category_key = 'sports'
            elif "全球" in user_text:
                category_key = 'global'
            elif "股市" in user_text:
                category_key = 'stock'
            elif "社會" in user_text:
                category_key = 'social'
            elif "產經" in user_text or "產業" in user_text:
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
            # 先丟詞頻柱狀圖
            if freq_url:
                messages.append(
                    ImageSendMessage(
                        original_content_url=freq_url,
                        preview_image_url=freq_url
                    )
                )
            # 再丟文字雲
            messages.append(
                ImageSendMessage(
                    original_content_url=image_url,
                    preview_image_url=image_url
                )
            )

            line_bot_api.reply_message(event.reply_token, messages)
            return

        # === 其他文字 → 顯示「一個泡泡 + 5 個 Quick Reply 按鈕」 ===
        msg = TextSendMessage(
            text='請選擇想看的新聞類別',
            quick_reply=QuickReply(items=[
                QuickReplyButton(
                    action=PostbackAction(
                        label='運動新聞',
                        display_text='我要看運動新聞',
                        data='action=news&cat=sports'
                    )
                ),
                QuickReplyButton(
                    action=PostbackAction(
                        label='全球新聞',
                        display_text='我要看全球新聞',
                        data='action=news&cat=global'
                    )
                ),
                QuickReplyButton(
                    action=PostbackAction(
                        label='股市新聞',
                        display_text='我要看股市新聞',
                        data='action=news&cat=stock'
                    )
                ),
                QuickReplyButton(
                    action=PostbackAction(
                        label='社會新聞',
                        display_text='我要看社會新聞',
                        data='action=news&cat=social'
                    )
                ),
                QuickReplyButton(
                    action=PostbackAction(
                        label='產經新聞',
                        display_text='我要看產經新聞',
                        data='action=news&cat=econ'
                    )
                ),
            ])
        )

        line_bot_api.reply_message(event.reply_token, msg)

    except Exception as e:
        print("[handle_text_message] error:", traceback.format_exc())
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='程式發生錯誤，請查看伺服器 LOG。')
        )


# ==========================
# 處理 Postback（按下各類新聞按鈕）
# ==========================
@handler.add(PostbackEvent)
def handle_postback(event):
    try:
        data = event.postback.data
        print(f"[Postback] raw data = {data}")

        params = parse_qs(data)
        action = params.get('action', [''])[0]
        chat_id = get_chat_id(event)

        if action == 'news':
            category_key = params.get('cat', [''])[0]
            if category_key not in CATEGORIES:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text='未知的新聞類別，請重新選擇。')
                )
                return

            cname = CATEGORIES[category_key]['name']

            # 取得目前這個聊天室、這個類別是第幾頁（預設第 1 頁）
            chat_state = news_page_state.get(chat_id, {})
            current_page = chat_state.get(category_key, 1)
            print(f"[news] chat_id={chat_id}, category={category_key}, current_page={current_page}")

            # 每次按按鈕都重新爬一次最新列表
            news_list = scrape_udn_category(category_key)

            if not news_list:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'目前無法取得{cname}新聞，請稍後再試。')
                )
                return

            # 計算這一頁要顯示的範圍（1–5、6–10、11–15...）
            start_idx = (current_page - 1) * PAGE_SIZE
            end_idx = current_page * PAGE_SIZE
            page_items = news_list[start_idx:end_idx]

            if not page_items:
                # 已經沒有更多新聞了，提示一下並把該類別頁數重置回 1，並清空該類別已看標題
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'{cname}新聞已經沒有更多最新內容了，我幫你從第一頁重新開始喔！')
                )
                chat_state[category_key] = 1
                news_page_state[chat_id] = chat_state
                # 清掉這個聊天室目前所有已看標題（讓文字雲重新累積）
                seen_titles_state[chat_id] = {}
                return

            # 把這一頁的標題累積起來，給文字雲用
            chat_seen = seen_titles_state.get(chat_id, {})
            # all 類別（全部新聞）
            all_list = chat_seen.get('all', [])
            # 單一類別
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

            # === 將本頁 5 則新聞組成「一個文字框」 ===
            lines = []
            for i, row in enumerate(page_items, start=start_idx + 1):
                block = f"{cname}新聞 第{i} 則\n{row['標題']}\n{row['連結']}"
                lines.append(block)

            reply_text = "\n\n".join(lines)  # 每則新聞之間空一行

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )

            # 下一次按按鈕，就看下一頁
            chat_state[category_key] = current_page + 1
            news_page_state[chat_id] = chat_state

        else:
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
