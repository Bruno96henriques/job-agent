import os
import json
import re
import time
import random
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import anthropic
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

HISTORY_FILE = Path("data/history.json")
REPORT_FILE = Path("report/index.html")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

BRUNO_PROFILE = """
Consultor Sénior com 5 anos de experiência em consultoria de gestão, principalmente setor público e institucional.
Experiências principais: gestão de projetos, candidaturas a fundos europeus, reengenharia de processos,
elaboração de propostas comerciais, coordenação de equipas de analistas, documentação técnica.
Procura vagas em: consultoria de estratégia, consultoria de negócio, gestão de projetos, business analyst sénior.
Localização: Lisboa. Não tem interesse em vagas 100% técnicas de IT, engenharia de software ou programação.
"""

KEYWORDS = [
    "consultor estrategia",
    "business consultant",
    "gestor projeto",
    "consultoria gestao lisboa"
]


def load_history():
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": [], "last_run": None}


def save_history(history):
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def make_job_id(title, company):
    return f"{title.lower().strip()}-{company.lower().strip()}"


def scrape_indeed(keyword):
    jobs = []
    url = f"https://pt.indeed.com/jobs?q={requests.utils.quote(keyword)}&l=Lisboa&sort=date"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", attrs={"data-testid": "slider_item"}) or \
                soup.find_all("li", class_=re.compile("css-"))
        for card in cards[:15]:
            title_el = card.find("h2") or card.find("a", class_=re.compile("jobTitle"))
            company_el = card.find("span", {"data-testid": "company-name"}) or \
                         card.find(class_=re.compile("companyName"))
            desc_el = card.find("div", class_=re.compile("job-snippet"))
            link_el = card.find("a", href=re.compile("/rc/|/pagead/"))

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            desc = desc_el.get_text(strip=True) if desc_el else ""
            link = ("https://pt.indeed.com" + link_el["href"]) if link_el else ""

            if title and company and len(title) > 3:
                jobs.append({
                    "title": title, "company": company,
                    "description": desc, "link": link, "source": "Indeed"
                })
    except Exception as e:
        print(f"Indeed error '{keyword}': {e}")
    return jobs


def scrape_linkedin(keyword):
    jobs = []
    url = f"https://www.linkedin.com/jobs/search/?keywords={requests.utils.quote(keyword)}&location=Lisboa&sortBy=DD"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", class_=re.compile("base-card"))
        for card in cards[:15]:
            title_el = card.find("h3", class_=re.compile("base-search-card__title"))
            company_el = card.find("h4", class_=re.compile("base-search-card__subtitle"))
            link_el = card.find("a", href=True)

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            link = link_el["href"] if link_el else ""

            if title and company and len(title) > 3:
                jobs.append({
                    "title": title, "company": company,
                    "description": "", "link": link, "source": "LinkedIn"
                })
    except Exception as e:
        print(f"LinkedIn error '{keyword}': {e}")
    return jobs


def scrape_net_empregos(keyword):
    jobs = []
    url = f"https://www.net-empregos.com/pesquisa-de-empregos.asp?q={requests.utils.quote(keyword)}&zona=11"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", class_=re.compile("job|oferta|emprego"))
        for card in cards[:15]:
            title_el = card.find(["h2", "h3"])
            company_el = card.find(class_=re.compile("empresa|company"))
            link_el = card.find("a", href=True)

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            link = link_el["href"] if link_el else ""
            if link and not link.startswith("http"):
                link = "https://www.net-empregos.com" + link

            if title and company and len(title) > 3:
                jobs.append({
                    "title": title, "company": company,
                    "description": "", "link": link, "source": "Net-Empregos"
                })
    except Exception as e:
        print(f"Net-Empregos error '{keyword}': {e}")
    return jobs


def deduplicate(jobs):
    seen = set()
    unique = []
    for job in jobs:
        job_id = make_job_id(job["title"], job["company"])
        if job_id not in seen:
            seen.add(job_id)
            unique.append(job)
    return unique


def filter_new(jobs, history):
    seen_ids = set(history.get("seen_ids", []))
    new_jobs = []
    for job in jobs:
        job_id = make_job_id(job["title"], job["company"])
        if job_id not in seen_ids:
            new_jobs.append(job)
            seen_ids.add(job_id)
    history["seen_ids"] = list(seen_ids)
    return new_jobs


def analyze_with_claude(jobs):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    analyzed = []
    for job in jobs:
        prompt = f"""Analisa esta vaga para o seguinte candidato:

PERFIL:
{BRUNO_PROFILE}

VAGA:
Título: {job['title']}
Empresa: {job['company']}
Descrição: {job['description'] or 'Não disponível'}

Classifica:
3 = Muito relevante
2 = Relevante
1 = Para considerar
0 = Ignorar (IT técnico, engenharia, não em Lisboa, etc.)

Responde APENAS com JSON válido:
{{"score": <0-3>, "reason": "<uma linha>"}}"""

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()
            result = json.loads(text)
            job["score"] = int(result.get("score", 1))
            job["reason"] = result.get("reason", "")
        except Exception as e:
            print(f"Claude error '{job['title']}': {e}")
            job["score"] = 1
            job["reason"] = ""

        if job["score"] > 0:
            analyzed.append(job)
        time.sleep(0.5)

    return analyzed


def generate_html_report(jobs, date_str):
    very_relevant = [j for j in jobs if j["score"] == 3]
    relevant = [j for j in jobs if j["score"] == 2]
    consider = [j for j in jobs if j["score"] == 1]

    source_colors = {
        "LinkedIn": "#0077b5",
        "Indeed": "#2164f3",
        "Net-Empregos": "#e84118"
    }

    def job_rows(job_list):
        rows = ""
        for j in job_list:
            color = source_colors.get(j["source"], "#666")
            rows += f"""
            <tr>
                <td style="padding:12px;border-bottom:1px solid #eee;">
                    <a href="{j['link']}" target="_blank" style="font-weight:600;color:#111;text-decoration:none;">{j['title']}</a>
                    <br><small style="color:#888;">{j.get('reason','')}</small>
                </td>
                <td style="padding:12px;border-bottom:1px solid #eee;color:#444;">{j['company']}</td>
                <td style="padding:12px;border-bottom:1px solid #eee;">
                    <span style="background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:12px;">{j['source']}</span>
                </td>
                <td style="padding:12px;border-bottom:1px solid #eee;">
                    <a href="{j['link']}" target="_blank"
                       style="background:#111;color:white;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:13px;">Ver vaga</a>
                </td>
            </tr>"""
        return rows

    def section(title, color, job_list):
        if not job_list:
            return ""
        return f"""
        <h2 style="color:{color};margin-top:32px;">{title} ({len(job_list)})</h2>
        <table style="width:100%;border-collapse:collapse;background:white;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1);">
            <thead>
                <tr style="background:#f8f8f8;">
                    <th style="padding:12px;text-align:left;color:#888;font-weight:500;">Vaga</th>
                    <th style="padding:12px;text-align:left;color:#888;font-weight:500;">Empresa</th>
                    <th style="padding:12px;text-align:left;color:#888;font-weight:500;">Site</th>
                    <th style="padding:12px;"></th>
                </tr>
            </thead>
            <tbody>{job_rows(job_list)}</tbody>
        </table>"""

    html = f"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vagas {date_str}</title>
<style>
  body {{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f2f3f5;margin:0;padding:24px;}}
  .wrap {{max-width:1000px;margin:0 auto;}}
</style>
</head>
<body>
<div class="wrap">
  <h1 style="color:#111;margin-bottom:4px;">Relatório de Vagas</h1>
  <p style="color:#888;margin-top:0;">{date_str}</p>
  <div style="background:white;padding:20px;border-radius:8px;margin-bottom:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);display:flex;gap:24px;align-items:center;">
    <span style="font-size:28px;font-weight:700;">{len(jobs)} vagas novas</span>
    <span style="color:#27ae60;font-weight:500;">{len(very_relevant)} muito relevantes</span>
    <span style="color:#e67e22;font-weight:500;">{len(relevant)} relevantes</span>
    <span style="color:#95a5a6;font-weight:500;">{len(consider)} para considerar</span>
  </div>
  {section("Muito Relevantes", "#27ae60", very_relevant)}
  {section("Relevantes", "#e67e22", relevant)}
  {section("Para Considerar", "#95a5a6", consider)}
</div>
</body>
</html>"""

    REPORT_FILE.parent.mkdir(exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    return html


def send_email(jobs, date_str, report_url):
    very_relevant = [j for j in jobs if j["score"] == 3]
    relevant = [j for j in jobs if j["score"] == 2]
    consider = [j for j in jobs if j["score"] == 1]

    def lines(job_list):
        if not job_list:
            return "  Nenhuma hoje\n"
        return "\n".join(f"  - {j['title']} | {j['company']}" for j in job_list) + "\n"

    body = f"""Boa noite Bruno,

Hoje encontrei {len(jobs)} vagas novas.

MUITO RELEVANTES ({len(very_relevant)})
{lines(very_relevant)}
RELEVANTES ({len(relevant)})
{lines(relevant)}
PARA CONSIDERAR ({len(consider)})
{lines(consider)}
Ver relatório completo: {report_url}

Job Agent"""

    message = Mail(
        from_email=os.environ.get("EMAIL_FROM", os.environ["EMAIL_TO"]),
        to_emails=os.environ["EMAIL_TO"],
        subject=f"Vagas - {date_str} ({len(jobs)} novas)",
        plain_text_content=body
    )

    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    response = sg.send(message)
    print(f"Email sent: {response.status_code}")


def main():
    print(f"Job agent starting at {datetime.now().isoformat()}")
    date_str = datetime.now().strftime("%d de %B de %Y")
    history = load_history()

    all_jobs = []
    for keyword in KEYWORDS:
        print(f"Searching: {keyword}")
        all_jobs.extend(scrape_indeed(keyword))
        time.sleep(random.uniform(1, 2))
        all_jobs.extend(scrape_linkedin(keyword))
        time.sleep(random.uniform(1, 2))
        all_jobs.extend(scrape_net_empregos(keyword))
        time.sleep(random.uniform(1, 2))

    print(f"Raw jobs: {len(all_jobs)}")
    all_jobs = deduplicate(all_jobs)
    print(f"After dedup: {len(all_jobs)}")
    new_jobs = filter_new(all_jobs, history)
    print(f"New jobs: {len(new_jobs)}")

    report_url = "https://Bruno96henrques.github.io/job-agent/"

    if new_jobs:
        analyzed = analyze_with_claude(new_jobs)
        print(f"Relevant jobs: {len(analyzed)}")
        generate_html_report(analyzed, date_str)
        send_email(analyzed, date_str, report_url)
    else:
        print("No new jobs today.")
        generate_html_report([], date_str)

    history["last_run"] = datetime.now().isoformat()
    save_history(history)
    print("Done.")


if __name__ == "__main__":
    main()
