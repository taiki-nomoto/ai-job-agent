import os
from openai import OpenAI
from playwright.sync_api import sync_playwright
import gspread
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import re
import time
from datetime import datetime

# ==============================
# エージェント設定
# ==============================

# CrowdWorksでAI評価する最大件数
AI_EVALUATION_LIMIT = 20

# Google Sheetsへ保存するランク
SAVE_RANKS = {"S", "A", "B"}

# 通信エラー時の再試行設定
RETRY_COUNT = 3
RETRY_WAIT_SECONDS = 2


# =====================================
# 基本設定
# =====================================

SPREADSHEET_ID = os.environ["AI_JOB_SPREADSHEET_ID"]
SERVICE_ACCOUNT_FILE = os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]

LANCERS_BASE_URL = "https://tech-agent.lancers.jp"

LANCERS_KEYWORDS = [
    "生成AI",
    "ChatGPT",
    "AIライティング",
]

CROWDWORKS_KEYWORDS = [
    "ChatGPT",
    "生成AI",
    "AIライティング",
    "AI画像生成",
    "Canva AI",
    "Claude",
    "Gemini",
]

# 1回の実行でAI評価する件数
LANCERS_LIMIT = 5
CROWDWORKS_LIMIT = 20


# =====================================
# 初期設定
# =====================================

client = OpenAI()

gc = gspread.service_account(
    filename=SERVICE_ACCOUNT_FILE
)

sheet = gc.open_by_key(
    SPREADSHEET_ID
).worksheet("AI案件管理表")


# =====================================
# 共通関数
# =====================================

def normalize_url(url):
    return url.strip().rstrip("/")


def normalize_title(title):
    title = title.lower().strip()

    # 「のお仕事(...)」など末尾のカテゴリ表記を除く
    title = re.sub(
        r"のお仕事\([^)]*\)$",
        "",
        title
    )

    # 空白・記号の違いを吸収
    title = re.sub(
        r"[\s　【】\[\]「」『』!！?？・\-ー_｜|/／]+",
        "",
        title
    )

    return title


def get_existing_urls():
    for attempt in range(5):
        try:
            return sheet.col_values(9)

        except Exception as e:
            print(
                f"Sheets読み込み失敗 "
                f"{attempt + 1}/5：{e}"
            )

            if attempt < 4:
                time.sleep(5)

    return []



def retry_operation(func, max_attempts=3, wait_seconds=2):
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()

        except Exception as e:
            last_error = e

            if attempt < max_attempts:
                print(
                    f"一時エラー。再試行します "
                    f"({attempt}/{max_attempts})：{e}"
                )
                time.sleep(wait_seconds)

    raise last_error



def save_to_sheet(row):
    next_row = len(sheet.get_all_values()) + 1

    retry_operation(
        lambda: sheet.update(
            values=[row],
            range_name=f"A{next_row}:O{next_row}"
        ),
        max_attempts=RETRY_COUNT,
        wait_seconds=RETRY_WAIT_SECONDS
    )

    print(
        "Sheets書き込み成功：",
        next_row,
        "行目"
    )


def extract_ai_field(label, text):
    match = re.search(
        rf"{re.escape(label)}：\s*(.+)",
        text
    )

    if match:
        return match.group(1).strip()

    return ""


def evaluate_with_ai(title, description):
    prompt = f"""
あなたは、生成AIを勉強し始めた初心者が
副業案件に応募できるか判断するアドバイザーです。

以下の案件を評価してください。

【案件名】
{title}

【案件内容】
{description}

次の形式を必ず守ってください。
初心者向け度とおすすめ度は、
星ではなく1〜5の数字だけで回答してください。

初心者向け度：1〜5
おすすめ度：1〜5
必要スキル：短く
勉強すべきこと：短く
メモ：100文字以内
"""

    response = client.responses.create(
        model="gpt-5.6",
        input=prompt
    )

    result = response.output_text

    beginner_match = re.search(
        r"初心者向け度：\s*([1-5])",
        result
    )

    recommend_match = re.search(
        r"おすすめ度：\s*([1-5])",
        result
    )

    beginner = (
        int(beginner_match.group(1))
        if beginner_match else 0
    )

    recommend = (
        int(recommend_match.group(1))
        if recommend_match else 0
    )

    total_score = beginner + recommend

    if total_score >= 9:
        rank = "S"
    elif total_score >= 7:
        rank = "A"
    elif total_score >= 5:
        rank = "B"
    else:
        rank = "C"

    return {
        "beginner": beginner,
        "recommend": recommend,
        "skills": extract_ai_field(
            "必要スキル",
            result
        ),
        "study": extract_ai_field(
            "勉強すべきこと",
            result
        ),
        "memo": extract_ai_field(
            "メモ",
            result
        ),
        "score": total_score,
        "rank": rank,
    }


def stars(number):
    return (
        "★" * number
        + "☆" * (5 - number)
    )



def extract_crowdworks_reward(text):

    m = re.search(
        r"(?:1|１)(?:社|件|本|文字|枚)につき\s*([\d,.]+)\s*円",
        text
    )
    if m:
        return f"1件あたり{m.group(1)}円"

    m = re.search(
        r"初回\s*(?:1|１)?(?:社|件|本|文字|枚)?\s*([\d,]+)\s*円",
        text
    )
    if m:
        return f"初回{m.group(1)}円"

    m = re.search(
        r"(?:1|１)記事\s*[:：]\s*([\d,]+)\s*円\s*[〜～]?",
        text
    )
    if m:
        return f"1記事{m.group(1)}円〜"

    m = re.search(
        r"文字単価\s*固定報酬制\s*([\d.]+)\s*円",
        text,
        re.S
    )
    if m:
        return f"文字単価{m.group(1)}円"

    m = re.search(
        r"[:：]\s*([\d,]+)\s*円\s*[（(](?:税込|税抜)",
        text
    )
    if m:
        return f"{m.group(1)}円"

    m = re.search(
        r"固定報酬制\s*([\d,]+)\s*円",
        text,
        re.S
    )
    if m:
        return f"{m.group(1)}円"

    m = re.search(
        r"契約金額（目安）:\s*([\d,]+)\s*円\s*[〜～]\s*([\d,]+)\s*円",
        text
    )
    if m:
        return f"{m.group(1)}〜{m.group(2)}円"

    m = re.search(
        r"時給\s*([\d,]+)\s*円",
        text
    )
    if m:
        return f"時給{m.group(1)}円"

    m = re.search(
        r"契約金額(?:（見積額）)?は、?\s*[*＊]*([\d,]+)\s*円",
        text
    )
    if m:
        return f"{m.group(1)}円"

    if "ワーカーと相談する" in text:
        return "相談"

    return "要確認"



# =====================================
# ランサーズ テックエージェント
# =====================================

def search_lancers(existing_urls):

    jobs = {}

    for keyword in LANCERS_KEYWORDS:

        search_url = (
            f"{LANCERS_BASE_URL}/project"
            f"?q_text={quote(keyword)}"
        )

        response = retry_operation(
            lambda: requests.get(
                search_url,
                headers={
                    "User-Agent": "Mozilla/5.0"
                },
                timeout=15
            ),
            max_attempts=RETRY_COUNT,
            wait_seconds=RETRY_WAIT_SECONDS
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        for card in soup.select(
            "li.cp-projects-listItem"
        ):
            form = card.find_parent("form")

            if not form:
                continue

            action = form.get("action")

            if not action:
                continue

            detail_url = normalize_url(
                urljoin(
                    LANCERS_BASE_URL,
                    action
                )
            )

            jobs[detail_url] = True

    new_jobs = [
        url
        for url in jobs
        if url not in existing_urls
    ]

    print()
    print("====================================")
    print("ランサーズ")
    print("検索：", len(jobs))
    print(
        "登録済み：",
        len(jobs) - len(new_jobs)
    )
    print("新規：", len(new_jobs))
    print("====================================")

    added = 0
    errors = 0
    s_count = 0

    for url in new_jobs[:LANCERS_LIMIT]:

        try:
            response = retry_operation(
                lambda: requests.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0"
                    },
                    timeout=15
                ),
                max_attempts=RETRY_COUNT,
                wait_seconds=RETRY_WAIT_SECONDS
            )

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            text = soup.get_text(
                "\n",
                strip=True
            )

            title = (
                soup.title.get_text(strip=True)
                .split(" | ")[0]
                if soup.title else ""
            )

            reward_match = re.search(
                r"月額報酬\s*〜?\s*"
                r"([\d,]+)\s*円",
                text
            )

            if not reward_match:
                reward_match = re.search(
                    r"〜\s*([\d,]+)"
                    r"\s*円\s*/\s*月",
                    text
                )

            reward = (
                reward_match.group(1)
                + "円/月"
                if reward_match
                else "要確認"
            )

            ai = evaluate_with_ai(
                title,
                text[:4000]
            )

            if (
                "フルリモート" in text
                or "完全リモート" in text
            ):
                remote = "○"

            elif "リモート" in text:
                remote = "△"

            else:
                remote = "×"

            row = [
                title,
                "ランサーズ テックエージェント",
                text[:500],
                reward,
                ai["skills"],
                stars(ai["beginner"]),
                stars(ai["recommend"]),
                remote,
                url,
                "",
                "未応募",
                ai["study"],
                ai["memo"],
                ai["score"],
                ai["rank"],
            ]

            save_to_sheet(row)

            existing_urls.add(url)
            added += 1

            if ai["rank"] == "S":
                s_count += 1

            print(
                "追加：",
                title,
                "/",
                ai["rank"]
            )

        except Exception as e:
            errors += 1

            print(
                "ランサーズ処理エラー：",
                url
            )
            print(e)

    return {
        "found": len(jobs),
        "new": len(new_jobs),
        "added": added,
        "errors": errors,
        "s": s_count,
    }


def judge_crowdworks_job(title, job_text):
    judge_text = title + "\n" + job_text
    t = judge_text.lower()

    score = 0
    reasons = []

    # 初心者向け加点
    if "未経験歓迎" in t:
        score += 4
        reasons.append("未経験歓迎(+4)")
    elif "未経験ok" in t:
        score += 3
        reasons.append("未経験OK(+3)")
    elif "未経験" in t:
        score += 2
        reasons.append("未経験(+2)")

    if "初心者歓迎" in t:
        score += 4
        reasons.append("初心者歓迎(+4)")
    elif "初心者ok" in t:
        score += 3
        reasons.append("初心者OK(+3)")

    if "経験不問" in t:
        score += 3
        reasons.append("経験不問(+3)")

    if "実務経験不問" in t:
        score += 4
        reasons.append("実務経験不問(+4)")

    bonuses = {
        "マニュアル": 2,
        "テンプレート": 2,
        "プロンプト完備": 2,
        "コピペ": 2,
        "簡単": 1,
        "chatgpt": 1,
        "生成ai": 1,
        "gemini": 1,
        "claude": 1,
    }

    for word, points in bonuses.items():
        if word in t:
            score += points
            reasons.append(f"{word}(+{points})")

    # ハード条件
    hard_expert = False
    portfolio_required = False

    hard_patterns = [
        r"実務経験.{0,20}(?:必須|必要)",
        r"実務経験.{0,20}\d+\s*年以上",
        r"\d+\s*年以上.{0,20}(?:実務経験|経験)",
        r"経験者.{0,10}(?:募集|限定|のみ)",
        r"即戦力.{0,10}(?:募集|求む|必須)",
        r"講師.{0,15}(?:経験必須|経験が必須|経験者募集)",
    ]

    for pattern in hard_patterns:
        if re.search(pattern, judge_text, re.I | re.S):
            hard_expert = True
            reasons.append("経験者向け必須条件")

    if re.search(
        r"ポートフォリオ.{0,10}(?:必須|提出必須)",
        judge_text,
        re.I | re.S
    ):
        portfolio_required = True
        reasons.append("ポートフォリオ必須")

    # 最終判定
    if hard_expert:
        judge = "経験者向け"

    elif portfolio_required:
        judge = "要確認"

    elif score >= 6:
        judge = "初心者向け"

    elif score >= 1:
        judge = "要確認"

    else:
        judge = "経験者向け"

    return score, judge, reasons



def crowdworks_title_score(title):
    t = title.lower()
    score = 0

    if "未経験歓迎" in t:
        score += 5
    elif "未経験ok" in t:
        score += 4
    elif "未経験" in t:
        score += 3

    if "初心者歓迎" in t:
        score += 5
    elif "初心者ok" in t:
        score += 4

    if "経験不問" in t:
        score += 4

    bonuses = {
        "簡単": 2,
        "マニュアル": 2,
        "コピペ": 2,
        "プロンプト完備": 3,
        "データ入力": 2,
        "記事": 2,
        "ライター": 2,
        "画像生成": 2,
        "canva": 2,
        "モニター": 2,
        "アンケート": 2,
        "完全在宅": 2,
        "フルリモート": 2,
    }

    for word, points in bonuses.items():
        if word in t:
            score += points

    penalties = {
        "経験者募集": -5,
        "実務経験": -5,
        "3年以上": -5,
        "エンジニア": -4,
        "コンサル": -4,
        "講師": -5,
        "ディレクター": -3,
        "マネージャー": -3,
    }

    for word, points in penalties.items():
        if word in t:
            score += points

    return score



# =====================================
# CrowdWorks
# =====================================

BEGINNER_WORDS = {
    "未経験歓迎": 3,
    "初心者歓迎": 3,
    "経験不問": 3,
    "未経験": 2,
    "初心者": 2,
    "プロンプト完備": 2,
    "コピペ": 2,
    "簡単": 1,
    "時間自由": 1,
    "マニュアル完備": 2,
}

EXPERT_WORDS = {
    "実務経験": -3,
    "3年以上": -3,
    "講師": -3,
    "即戦力": -3,
    "経験者募集": -2,
    "ポートフォリオ必須": -2,
}


def search_crowdworks(existing_urls):

    jobs = []
    job_titles = {}

    existing_titles = {
        normalize_title(title)
        for title in sheet.col_values(1)
        if title
    }

    session_titles = set()

    ai_words = [
        "chatgpt",
        "生成ai",
        "ai",
        "claude",
        "gemini",
        "canva",
        "画像生成",
        "ライティング",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        for keyword in CROWDWORKS_KEYWORDS:

            search_url = (
                "https://crowdworks.jp/public/jobs/search"
                "?search%5Bkeywords%5D="
                + quote(keyword)
            )

            page.goto(
                search_url,
                wait_until="networkidle"
            )

            links = page.locator(
                'a[href*="/public/jobs/"]'
            )

            for i in range(links.count()):

                link = links.nth(i)

                href = link.get_attribute("href")
                title = link.inner_text().strip()

                if not href or not title:
                    continue

                if href.startswith("/"):
                    href = (
                        "https://crowdworks.jp"
                        + href
                    )

                if not re.fullmatch(
                    r"https://crowdworks\.jp/"
                    r"public/jobs/\d+/?",
                    href
                ):
                    continue

                href = normalize_url(href)

                title_lower = title.lower()

                if not any(
                    word in title_lower
                    for word in ai_words
                ):
                    continue

                if href not in jobs:
                    jobs.append(href)
                    job_titles[href] = title

        browser.close()

    new_jobs = [
        url
        for url in jobs
        if url not in existing_urls
    ]

    # 初心者向けタイトルを優先
    new_jobs.sort(
        key=lambda url: crowdworks_title_score(
            job_titles.get(url, "")
        ),
        reverse=True
    )

    candidates = []

    # 上位案件から順番に確認し、
    # 一次審査通過が20件集まるまで探す
    for url in new_jobs:

        if len(candidates) >= AI_EVALUATION_LIMIT:
            break

        try:
            response = retry_operation(
                lambda: requests.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0"
                    },
                    timeout=15
                ),
                max_attempts=RETRY_COUNT,
                wait_seconds=RETRY_WAIT_SECONDS
            )

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            full_text = soup.get_text(
                "\n",
                strip=True
            )

            title = (
                soup.title.get_text(strip=True)
                .split(" | ")[0]
                if soup.title else ""
            )

            start = full_text.find(
                "仕事の詳細"
            )

            end = full_text.find(
                "クライアント情報"
            )

            if (
                start != -1
                and end != -1
            ):
                job_text = full_text[
                    start:end
                ]
            else:
                job_text = full_text

            judge_text = title + "\n" + job_text

            # タイトルだけでも明確な経験者向け案件は強制除外
            expert_title_words = [
                "経験者向け",
                "経験者募集",
                "経験者限定",
                "実務経験必須",
                "実務経験者",
                "即戦力募集",
                "即戦力求む",
            ]

            if any(
                word in title
                for word in expert_title_words
            ):
                expert_title_skipped += 1

                print(
                    "経験者タイトルで除外：",
                    title
                )
                continue

            python_score, first_judge, reasons = (
                judge_crowdworks_job(
                    title,
                    job_text
                )
            )

            if first_judge == "経験者向け":
                continue

            deadline_match = re.search(
                r"応募期限\s*"
                r"(\d{4}年\d{2}月\d{2}日)",
                full_text
            )

            deadline = (
                deadline_match.group(1)
                if deadline_match
                else ""
            )

            # 応募期限切れはAI評価しない
            if deadline_match:
                deadline_date = datetime.strptime(
                    deadline_match.group(1),
                    "%Y年%m月%d日"
                ).date()

                if deadline_date < datetime.now().date():
                    continue

            if (
                "完全在宅" in judge_text
                or "フルリモート" in judge_text
            ):
                remote = "○"

            elif "在宅" in judge_text:
                remote = "○"

            else:
                remote = "△"

            reward = extract_crowdworks_reward(
                full_text
            )

            candidates.append({
                "title": title,
                "url": url,
                "job_text": job_text,
                "deadline": deadline,
                "remote": remote,
                "reward": reward,
                "python_score": python_score,
            })

        except Exception as e:
            print(
                "CrowdWorks取得エラー：",
                url
            )
            print(e)

    print()
    print("====================================")
    print("CrowdWorks")
    print("検索：", len(jobs))
    print(
        "登録済み：",
        len(jobs) - len(new_jobs)
    )
    print("新規：", len(new_jobs))
    print(
        "一次審査通過：",
        len(candidates)
    )
    print("====================================")

    added = 0
    errors = 0
    s_count = 0
    a_count = 0
    c_skipped = 0
    ai_evaluated = 0
    title_duplicate_count = 0
    expert_title_skipped = 0

    for job in candidates[
        :CROWDWORKS_LIMIT
    ]:

        try:
            ai = evaluate_with_ai(
                job["title"],
                job["job_text"][:4000]
            )

            ai_evaluated += 1

            # Cランクは管理表へ追加しない
            if ai["rank"] not in SAVE_RANKS:
                c_skipped += 1

                print(
                    "見送り：",
                    job["title"],
                    "/",
                    ai["rank"]
                )
                continue

            normalized_title = normalize_title(
                job["title"]
            )

            # URLが違っても同じタイトルなら重複扱い
            if (
                normalized_title in existing_titles
                or normalized_title in session_titles
            ):
                title_duplicate_count += 1

                print(
                    "タイトル重複で除外：",
                    job["title"]
                )
                continue

            row = [
                job["title"],
                "CrowdWorks",
                job["job_text"][:500],
                job["reward"],
                ai["skills"],
                stars(ai["beginner"]),
                stars(ai["recommend"]),
                job["remote"],
                job["url"],
                job["deadline"],
                "未応募",
                ai["study"],
                ai["memo"],
                ai["score"],
                ai["rank"],
            ]

            save_to_sheet(row)

            existing_urls.add(
                job["url"]
            )

            existing_titles.add(
                normalized_title
            )
            session_titles.add(
                normalized_title
            )

            added += 1

            if ai["rank"] == "S":
                s_count += 1

            if ai["rank"] == "A":
                a_count += 1

            print(
                "追加：",
                job["title"],
                "/",
                ai["rank"]
            )

        except Exception as e:
            errors += 1

            print(
                "CrowdWorks処理エラー：",
                job["url"]
            )
            print(e)

    return {
        "found": len(jobs),
        "new": len(new_jobs),
        "added": added,
        "errors": errors,
        "s": s_count,
        "a": a_count,
        "c_skipped": c_skipped,
        "ai_evaluated": ai_evaluated,
        "title_duplicates": title_duplicate_count,
        "expert_title_skipped": expert_title_skipped,
    }


# =====================================
# メイン処理
# =====================================

def main():
    existing_urls = {
        normalize_url(url)
        for url in get_existing_urls()
        if url
    }

    print()
    print("AI案件調査を開始します")
    print(
        "現在の登録URL数：",
        len(existing_urls)
    )

    lancers = search_lancers(
        existing_urls
    )

    crowdworks = search_crowdworks(
        existing_urls
    )

    print()
    print("====================================")
    print("AI案件調査 完了")
    print("====================================")
    print(
        "検索で見つけた件数：",
        lancers["found"]
        + crowdworks["found"]
    )
    print(
        "新規候補：",
        lancers["new"]
        + crowdworks["new"]
    )
    print(
        "追加した件数：",
        lancers["added"]
        + crowdworks["added"]
    )
    print(
        "エラー：",
        lancers["errors"]
        + crowdworks["errors"]
    )
    print(
        "AI評価した件数：",
        crowdworks["ai_evaluated"]
    )
    print(
        "Cランク見送り：",
        crowdworks["c_skipped"]
    )
    print(
        "タイトル重複で除外：",
        crowdworks["title_duplicates"]
    )
    print(
        "経験者タイトルで除外：",
        crowdworks["expert_title_skipped"]
    )
    print(
        "Aランク：",
        crowdworks["a"]
    )
    print(
        "Sランク：",
        lancers["s"]
        + crowdworks["s"]
    )
    print("====================================")


if __name__ == "__main__":
    main()
