#!/usr/bin/env python3
"""
포트폴리오 뉴스 모니터링 - GitHub Actions 실행용
환경변수로 모든 설정을 받습니다.
"""
import os, json, base64, smtplib, re, time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from urllib.parse import quote
import requests
import anthropic

# ── 환경변수 ──────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
KAKAO_REST_API_KEY  = os.environ["KAKAO_REST_API_KEY"]
KAKAO_REFRESH_TOKEN = os.environ["KAKAO_REFRESH_TOKEN"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_SENDER        = os.environ["GMAIL_SENDER"]
GMAIL_RECIPIENTS    = os.environ["GMAIL_RECIPIENTS"].split(",")
GH_TOKEN = os.environ["GH_TOKEN"]
GH_USER  = "fundjylee-cmd"
GH_REPO  = "portfolio-reports"
PAGES_URL = f"https://{GH_USER}.github.io/{GH_REPO}/"

KST = timezone(timedelta(hours=9))

# ── 포트폴리오 딜 목록 ──────────────────────────────────────
PORTFOLIO = [
    {"name": "일본 ESS 에쿼티 투자",         "type": "해외에쿼티",
     "keywords": ["ESS 일본", "에너지저장장치 일본", "일본 재생에너지 정책", "Japan ESS battery storage"]},
    {"name": "SK이노베이션 신종자본증권",     "type": "신종자본증권",
     "keywords": ["SK이노베이션 신종자본증권", "SK이노베이션 신용등급", "SK이노베이션 재무", "SK이노베이션 공시"]},
    {"name": "SK LNG발전 자회사 인수금융",    "type": "인수금융",
     "keywords": ["SK LNG발전", "SK E&S LNG", "SK LNG 인수금융", "LNG 발전 정책"]},
    {"name": "대승엔지니어링 (모듈러스쿨)",   "type": "대출",
     "keywords": ["대승엔지니어링", "모듈러 스쿨", "모듈러 건축", "조립식 학교"]},
    {"name": "성수동 오피스빌딩",            "type": "부동산",
     "keywords": ["성수동 오피스", "성수동 빌딩", "성수동 부동산", "서울 오피스 공실"]},
    {"name": "효성화학 대출 (효성 보증)",     "type": "대출",
     "keywords": ["효성화학", "효성 신용등급", "효성화학 재무", "효성화학 공시", "효성화학 실적"]},
    {"name": "롯데지주 신종자본증권",         "type": "신종자본증권",
     "keywords": ["롯데지주 신종자본증권", "롯데 신용등급", "롯데지주 재무", "롯데지주 공시"]},
]

# ── 뉴스 수집 ──────────────────────────────────────────────
def fetch_google_news(keyword: str) -> list:
    url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:5]:
            title   = item.findtext("title", "")
            link    = item.findtext("link", "")
            pubdate = item.findtext("pubDate", "")
            try:
                dt = parsedate_to_datetime(pubdate).replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - dt).days > 7:
                    continue
            except:
                pass
            items.append({"title": title, "link": link, "pubdate": pubdate})
        return items
    except Exception as e:
        print(f"  뉴스 수집 오류 ({keyword}): {e}")
        return []

def collect_news_for_deal(deal: dict) -> list:
    seen, all_news = set(), []
    for kw in deal["keywords"]:
        for item in fetch_google_news(kw):
            if item["title"] not in seen:
                seen.add(item["title"])
                all_news.append(item)
        time.sleep(0.5)
    return all_news

# ── AI 분석 ────────────────────────────────────────────────
def analyze_with_claude(client, deal: dict, news_items: list) -> dict:
    if not news_items:
        return {"relevant_news": [], "summary": "관련 뉴스 없음", "importance": "없음", "comment": ""}

    news_text = "\n".join([
        f"[{i+1}] {n['title']}\n날짜: {n['pubdate']}\n링크: {n['link']}"
        for i, n in enumerate(news_items)
    ])

    prompt = f"""당신은 증권사 대체투자 파트의 포트폴리오 사후관리 전문가입니다.

포트폴리오 딜: {deal['name']} (유형: {deal['type']})

수집된 뉴스:
{news_text}

분석 기준:
1. 관련성: 위 딜과 직접 관련된 뉴스만 선별 (동명이인·무관 기사 제외)
2. 중요도:
   - 고중요도: 신용등급 변동, 부도/기업회생, 대주주 변경, 담보·유동성 위기
   - 중간중요도: 실적 발표, 경영진 변경, 주요 계약/M&A
   - 낮은중요도: 일반 보도, 홍보성 기사
3. 코멘트 작성 규칙 (엄수):
   - 뉴스 원문에 명시된 사실만 기술
   - 계열사/자회사 관계는 원문 명시 시에만 기재, 직접 판단 금지
   - 신용등급·재무수치는 원문 인용만, 추정 금지
   - 불확실한 내용은 "~로 알려짐", "~보도됨" 표현 사용

JSON으로 응답:
{{
  "relevant_news": [관련 뉴스 번호 배열, 예: [1, 3]],
  "importance": "고중요도" | "중간중요도" | "낮은중요도" | "없음",
  "summary": "핵심 내용 1-2문장",
  "comment": "사후관리 관점 코멘트 2-3문장 (원문 근거만)"
}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        content = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            result = json.loads(m.group())
            relevant = [news_items[i-1] for i in result.get("relevant_news", [])
                        if 1 <= i <= len(news_items)]
            result["relevant_news"] = relevant
            return result
    except Exception as e:
        print(f"  AI 분석 오류: {e}")
    return {"relevant_news": news_items[:3], "summary": "분석 오류", "importance": "낮은중요도", "comment": ""}

# ── HTML 생성 ──────────────────────────────────────────────
def generate_html(results: list, today_str: str) -> str:
    imp_color = {"고중요도": "#dc3545", "중간중요도": "#fd7e14", "낮은중요도": "#6c757d", "없음": "#adb5bd"}
    high = sum(1 for r in results if r["analysis"]["importance"] == "고중요도")
    mid  = sum(1 for r in results if r["analysis"]["importance"] == "중간중요도")

    deal_html = ""
    for r in results:
        imp   = r["analysis"]["importance"]
        color = imp_color.get(imp, "#adb5bd")
        news_html = "".join([
            f'<div style="padding:8px 0;border-bottom:1px solid #f0f0f0;">'
            f'<a href="{n["link"]}" target="_blank" style="color:#1a73e8;text-decoration:none;font-size:13px;">{n["title"]}</a>'
            f'<div style="color:#888;font-size:11px;margin-top:2px;">{n.get("pubdate","")}</div></div>'
            for n in r["analysis"]["relevant_news"]
        ]) or '<div style="color:#aaa;font-size:13px;padding:8px 0;">관련 뉴스 없음</div>'

        summary_html = ""
        if r["analysis"]["summary"] and r["analysis"]["summary"] != "관련 뉴스 없음":
            summary_html = f'<div style="padding:10px 16px;background:#f8f9fa;font-size:13px;color:#333;border-bottom:1px solid #e0e0e0;">{r["analysis"]["summary"]}</div>'

        comment_html = ""
        if r["analysis"].get("comment"):
            comment_html = f'<div style="padding:10px 16px;background:#fff8e1;font-size:13px;color:#555;border-top:1px solid #f0f0f0;"><b style="color:#e65100;">사후관리 코멘트</b><br>{r["analysis"]["comment"]}</div>'

        deal_html += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;margin-bottom:16px;overflow:hidden;">
          <div style="padding:14px 16px;border-left:4px solid {color};display:flex;justify-content:space-between;align-items:center;">
            <div>
              <span style="font-weight:600;font-size:15px;color:#1a1a1a;">{r['deal']['name']}</span>
              <span style="margin-left:8px;color:#666;font-size:12px;">{r['deal']['type']}</span>
            </div>
            <span style="background:{color};color:#fff;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:500;">{imp}</span>
          </div>
          {summary_html}
          <div style="padding:8px 16px 4px;">{news_html}</div>
          {comment_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>포트폴리오 모니터링 | {today_str}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',sans-serif;background:#f5f5f5;margin:0;padding:16px;">
  <div style="max-width:720px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#1a237e,#283593);color:#fff;padding:20px;border-radius:8px;margin-bottom:20px;">
      <div style="font-size:12px;opacity:0.8;margin-bottom:4px;">대체투자 파트 | 포트폴리오 사후관리</div>
      <div style="font-size:20px;font-weight:700;">뉴스 모니터링 브리핑</div>
      <div style="font-size:13px;margin-top:8px;opacity:0.9;">{today_str}</div>
      <div style="margin-top:12px;display:flex;gap:12px;flex-wrap:wrap;">
        <span style="background:rgba(220,53,69,0.8);padding:4px 12px;border-radius:20px;font-size:13px;">고중요도 {high}건</span>
        <span style="background:rgba(253,126,20,0.8);padding:4px 12px;border-radius:20px;font-size:13px;">중간중요도 {mid}건</span>
        <span style="background:rgba(255,255,255,0.2);padding:4px 12px;border-radius:20px;font-size:13px;">딜 {len(results)}개</span>
      </div>
    </div>
    {deal_html}
    <div style="text-align:center;color:#aaa;font-size:11px;padding:16px 0;">자동 생성 · 대체투자 파트 포트폴리오 모니터링 시스템</div>
  </div>
</body>
</html>"""

# ── GitHub Pages 업로드 ─────────────────────────────────────
def upload_to_pages(html: str) -> str:
    api = f"https://api.github.com/repos/{GH_USER}/{GH_REPO}/contents/index.html"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    payload = {
        "message": f"리포트 {datetime.now(KST).strftime('%Y-%m-%d')}",
        "content": base64.b64encode(html.encode()).decode()
    }
    r = requests.get(api, headers=headers)
    if r.ok:
        payload["sha"] = r.json()["sha"]
    requests.put(api, headers=headers, json=payload).raise_for_status()
    ts = datetime.now(KST).strftime("%Y%m%d%H%M")
    return f"{PAGES_URL}?v={ts}"

# ── 카카오톡 발송 ───────────────────────────────────────────
def send_kakao(summary: str, url: str):
    r = requests.post("https://kauth.kakao.com/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_API_KEY,
        "refresh_token": KAKAO_REFRESH_TOKEN,
    })
    r.raise_for_status()
    access_token = r.json()["access_token"]
    today = datetime.now(KST).strftime("%Y년 %m월 %d일")
    template = {
        "object_type": "text",
        "text": f"📊 포트폴리오 모니터링 | {today}\n\n{summary}\n\n📋 상세 리포트\n{url}",
        "link": {}
    }
    requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)}
    ).raise_for_status()
    print("카카오톡 발송 완료")

# ── 이메일 발송 ─────────────────────────────────────────────
def send_email(html: str, today_str: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[포트폴리오 모니터링] {today_str} 뉴스 브리핑"
    msg["From"] = GMAIL_SENDER
    msg["To"] = ", ".join(GMAIL_RECIPIENTS)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_SENDER, GMAIL_RECIPIENTS, msg.as_string())
    print(f"이메일 발송 완료: {', '.join(GMAIL_RECIPIENTS)}")

# ── 메인 ────────────────────────────────────────────────────
def main():
    today_str = datetime.now(KST).strftime("%Y년 %m월 %d일")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print(f"=== 포트폴리오 뉴스 모니터링 시작 ({today_str}) ===")

    results = []
    for deal in PORTFOLIO:
        print(f"\n[{deal['name']}] 뉴스 수집 중...")
        news = collect_news_for_deal(deal)
        print(f"  수집 {len(news)}건 → AI 분석 중...")
        analysis = analyze_with_claude(client, deal, news)
        print(f"  결과: {analysis['importance']} / 관련뉴스 {len(analysis['relevant_news'])}건")
        results.append({"deal": deal, "analysis": analysis})

    html = generate_html(results, today_str)
    high = sum(1 for r in results if r["analysis"]["importance"] == "고중요도")
    mid  = sum(1 for r in results if r["analysis"]["importance"] == "중간중요도")
    summary = f"고중요도 {high}건 | 중간중요도 {mid}건 | 딜 {len(results)}개 모니터링"

    print("\nGitHub Pages 업로드 중...")
    url = upload_to_pages(html)
    print(f"업로드 완료: {url}")

    print("카카오톡 발송 중...")
    send_kakao(summary, url)

    print("이메일 발송 중...")
    send_email(html, today_str)

    print("\n=== 모든 발송 완료 ===")

if __name__ == "__main__":
    main()
