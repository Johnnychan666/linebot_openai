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
        print(f"[wordcloud] chat_id
