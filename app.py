from flask import Flask, request, abort

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import *

# ====== python 的函數庫 ==========
import tempfile, os
import datetime
import time
import traceback
# ====== python 的函數庫 ==========

# ====== Selenium + 爬蟲相關套件 ==========
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import csv
import sys
# ====== Selenium + 爬蟲相關套件 ==========

app = Flask(__name__)
static_tmp_path = os.path.join(os.path.dirname(__file__), 'static', 'tmp')

# Channel Access Token
line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
# Channel Secret
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))


# ===================================
# UDN 運動新聞爬蟲設定（用 Selenium）
# ===================================
URL = 'https://udn.com/news/cate/2/7227'  # 運動新聞
BASE_URL = 'https://udn.com'
CLICK_COUNT = 8
WAIT_TIMEOUT = 30
# 請改成你自己的 chromedriver 路徑
CHROMEDRIVER_PATH = r'/Users/weixiang/Downloads/chromedriver'


def count_articles(driver):
    """計算當前 DOM 中符合選擇器的文章數量。"""
    return len(driver.find_elements(By.CSS_SELECTOR, 'div.story-list__text a'))


def scrape_udn_with_selenium(click_times):
    """回傳一個 list，裡面每筆是 {'標題':..., '連結':...}"""
    final_data = []

    # 檢查 ChromeDriver 執行檔是否存在
    if not os.path.exists(CHROMEDRIVER_PATH):
        print("❌ 致命錯誤：指定的 ChromeDriver 執行檔路徑不存在！")
        print(f"請檢查路徑: {CHROMEDRIVER_PATH}")
        return []

    # --- Chrome 選項設定 ---
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/143.0.7499.41 Safari/537.36'
    )

    # 初始化 WebDriver
    try:
        service_obj = Service(CHROMEDRIVER_PATH)
        driver = webdriver.Chrome(service=service_obj, options=chrome_options)
        time.sleep(1)
    except Exception as e:
        print("❌ 錯誤：無法啟動瀏覽器。")
        print("請檢查您的 ChromeDriver 執行權限 (chmod +x)。")
        print(f"原始錯誤訊息: {e}")
        return []

    driver.get(URL)
    print(f"--- 啟動瀏覽器並載入運動新聞頁面 ---")

    # 模擬點擊「更多」按鈕
    for i in range(click_times):
        try:
            print(f"--- 模擬點擊第 {i + 1} 次 More 按鈕 ---")

            initial_article_count = count_articles(driver)
            print(f"  [偵測] 點擊前文章數量: {initial_article_count}")

            more_button = WebDriverWait(driver, WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CLASS_NAME, "btn-more--news"))
            )

            if more_button.is_displayed() and more_button.is_enabled():
                driver.execute_script("arguments[0].scrollIntoView(true);", more_button)
                driver.execute_script("arguments[0].click();", more_button)

                WebDriverWait(driver, WAIT_TIMEOUT).until(
                    lambda d: count_articles(d) > initial_article_count
                )

                final_article_count = count_articles(driver)
                newly_loaded = final_article_count - initial_article_count
                print(f"  [成功] 載入完成，新增 {newly_loaded} 筆資料。總數: {final_article_count}")
            else:
                print("More 按鈕不可用或已全部載入完畢，停止點擊。")
                break

        except Exception as e:
            print(f"點擊第 {i + 1} 次失敗或載入完成: Timeout 或 {e}")
            break

    print("\n--- 開始解析 HTML 內容 ---")

    final_html = driver.page_source
    driver.quit()

    soup = BeautifulSoup(final_html, 'html.parser')
    news_elements = soup.select('div.story-list__text a')

    for element in news_elements:
        title = element.text.strip()
        relative_link = element.get('href')

        if title and relative_link:
            full_link = f"{BASE_URL}{relative_link}" if relative_link.startswith('/') else relative_link

            if 'udn.com' in full_link:
                final_data.append({
                    '標題': title,
                    '連結': full_link,
                })

    return final_data


# ==========================
# Flask / LINE Webhook
# ==========================
# 監聽所有來自 /callback 的 Post Request
@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']
    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    # handle webhook body
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
    """
    收到任何文字訊息 → 回傳一個按鈕模板訊息，
    目前只有「運動新聞」這個選項
    """

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
        # 開始爬取文章
        scraped_data = scrape_udn_with_selenium(click_times=CLICK_COUNT)

        if not scraped_data:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='目前無法取得運動新聞，請稍後再試。')
            )
            return

        # 取最新的前 5 筆
        top5 = scraped_data[:5]

        messages = []
        for i, row in enumerate(top5, start=1):
            text = f"{i}. {row['標題']}\n{row['連結']}"
            messages.append(TextSendMessage(text=text))

        # LINE 一次最多回傳 5 則訊息，剛好
        line_bot_api.reply_message(event.reply_token, messages)
    else:
        # 其他未定義的 Postback
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
    # host 設 0.0.0.0 才能被外部（如 Heroku）訪問
    app.run(host='0.0.0.0', port=port)
