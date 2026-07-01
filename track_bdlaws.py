import os
import json
import requests
from bs4 import BeautifulSoup
import datetime
import re
import hashlib
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
URL = "http://bdlaws.minlaw.gov.bd/laws-of-bangladesh-chronological-index.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

# Resolve paths relative to the script's directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "laws_database.json")
README_FILE = os.path.join(BASE_DIR, "README.md")

# Regular expression to match act-[number].html
ACT_REGEX = re.compile(r"act-(\d+)\.html")

# Historically high-profile laws that must be audited on every run
CRITICAL_LAW_IDS = {"11", "17", "18", "19", "48", "75", "96", "97", "305", "367", "682"}

def load_local_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_local_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def fetch_law_list():
    print(f"Fetching chronological index from {URL}...")
    response = requests.get(URL, headers=HEADERS, timeout=30)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch site, status code: {response.status_code}")
    
    soup = BeautifulSoup(response.text, "html.parser")
    laws = {}
    
    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = ACT_REGEX.search(href)
        if match:
            law_id = match.group(1)
            title = a.get_text(strip=True)
            if not title:
                continue
            laws[law_id] = {
                "id": law_id,
                "title": title,
                "url": f"http://bdlaws.minlaw.gov.bd/act-{law_id}.html"
            }
    return laws

def get_law_hash(url):
    """Fetches the law page, extracts clean body text, and returns its MD5 hash with polite retries and backoff."""
    retries = 3
    backoff = 1.0
    for attempt in range(retries):
        try:
            # Polite delay before requesting (strictly sequential to avoid rate limits)
            time.sleep(0.2)
            
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
                
            if response.status_code != 200:
                return None
            
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Reject generic home page / error page titles
            title_text = soup.title.get_text().strip() if soup.title else ""
            if title_text in ["The Laws of Bangladesh", "Laws of Bangladesh"]:
                time.sleep(backoff)
                backoff *= 2
                continue
                
            # Target only the main content layout of a valid law detail page
            main_content = soup.find("div", class_="boxed-layout")
            if not main_content:
                time.sleep(backoff)
                backoff *= 2
                continue
                
            body_text = main_content.get_text()
            cleaned_text = re.sub(r"\s+", " ", body_text).strip()
            return hashlib.md5(cleaned_text.encode("utf-8")).hexdigest()
        except Exception as e:
            time.sleep(backoff)
            backoff *= 2
            
    # Replaced unicode cross emoji with ASCII [ERROR] to prevent Windows console encoding crashes
    print(f"[ERROR] Failed to fetch valid content for {url} after {retries} attempts.")
    return None

def should_check_daily(law_id, title):
    """Returns True if the law is critical or was enacted recently (>= 2010)."""
    if law_id in CRITICAL_LAW_IDS:
        return True
    
    # Extract year from title (e.g. 1961, 2023)
    match = re.search(r"\b(18|19|20)\d{2}\b", title)
    if match:
        year = int(match.group(0))
        if year >= 2010:
            return True
            
    return False

def track_and_update():
    local_db = load_local_db()
    db_changed = False
    
    try:
        current_laws = fetch_law_list()
    except Exception as e:
        print(f"Error fetching law list: {e}")
        return

    new_laws = []
    modified_laws = []
    
    # Identify new laws
    for law_id, law_info in current_laws.items():
        if law_id not in local_db:
            new_laws.append(law_info)
            local_db[law_id] = {
                "id": law_id,
                "title": law_info["title"],
                "url": law_info["url"],
                "hash": "",
                "first_tracked": datetime.datetime.now().strftime("%Y-%m-%d"),
                "last_changed": datetime.datetime.now().strftime("%Y-%m-%d")
            }
            db_changed = True

    # Build the list of laws to check on this run
    daily_check_ids = []
    other_ids = []
    
    for law_id, law in local_db.items():
        if not law.get("hash"):
            daily_check_ids.append(law_id)
        elif should_check_daily(law_id, law["title"]):
            daily_check_ids.append(law_id)
        else:
            other_ids.append(law_id)
            
    # Sample 50 random laws from the older/non-critical pool
    random_audit_size = min(50, len(other_ids))
    random_audit_ids = random.sample(other_ids, random_audit_size) if other_ids else []
    
    laws_to_check = daily_check_ids + random_audit_ids
    print(f"Auditing content updates: checking {len(daily_check_ids)} critical/recent laws + {len(random_audit_ids)} random older laws (Total: {len(laws_to_check)}).")
    
    # We scrape strictly sequentially (max_workers=1) to prevent rate-limit blocks
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {
            executor.submit(get_law_hash, local_db[law_id]["url"]): law_id
            for law_id in laws_to_check
        }
        
        for future in as_completed(futures):
            law_id = futures[future]
            page_hash = future.result()
            
            if page_hash is None:
                continue
                
            old_hash = local_db[law_id].get("hash", "")
            
            if not old_hash:
                # Initialize hash on first run
                local_db[law_id]["hash"] = page_hash
                db_changed = True
            elif old_hash != page_hash:
                # Replaced unicode warning emoji with ASCII [CHANGED] to prevent Windows console encoding crashes
                print(f"[CHANGED] Change detected in law ID {law_id}: {local_db[law_id]['title']}")
                local_db[law_id]["hash"] = page_hash
                local_db[law_id]["last_changed"] = datetime.datetime.now().strftime("%Y-%m-%d")
                modified_laws.append(local_db[law_id])
                db_changed = True

    # Write changes to README if any new or modified laws
    if new_laws or modified_laws:
        update_readme(new_laws, modified_laws)
        
    if db_changed:
        print("Saving database updates...")
        save_local_db(local_db)
    else:
        print("Everything is up to date. No database changes.")

def update_readme(new_laws, modified_laws):
    content = ""
    if os.path.exists(README_FILE):
        with open(README_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "# 🇧🇩 Laws of Bangladesh Tracker\n\nThis folder tracks updates and changes to the laws of Bangladesh.\n\n"

    # Construct status block
    alert_lines = [
        "<!-- LATEST_LAWS_START -->",
        "> [!IMPORTANT]",
        "> **🔔 Real-time Legislative Updates Tracked!**"
    ]
    
    # Add newly added laws
    if new_laws:
        alert_lines.append("> ### 🆕 Newly Enacted Laws:")
        sorted_new = sorted(new_laws, key=lambda x: int(x['id']), reverse=True)[:10]
        for law in sorted_new:
            alert_lines.append(f"> * **[{law['title']}]({law['url']})** (ID: {law['id']})")
        if len(new_laws) > 10:
            alert_lines.append(f"> * ... and **{len(new_laws) - 10}** other new law(s).")
            
    # Add modified laws (word changes / amendments)
    if modified_laws:
        alert_lines.append("> ### ⚠️ Modified / Amended Provisions:")
        sorted_mod = sorted(modified_laws, key=lambda x: int(x['id']), reverse=True)[:10]
        for law in sorted_mod:
            alert_lines.append(f"> * **[{law['title']}]({law['url']})** (ID: {law['id']}) — *Word change detected on: {law['last_changed']}*")
        if len(modified_laws) > 10:
            alert_lines.append(f"> * ... and **{len(modified_laws) - 10}** other law changes.")

    alert_lines.append("<!-- LATEST_LAWS_END -->")
    alert_block = "\n".join(alert_lines) + "\n"

    # Replace existing block or insert at top
    start_tag = "<!-- LATEST_LAWS_START -->"
    end_tag = "<!-- LATEST_LAWS_END -->"
    
    if start_tag in content and end_tag in content:
        parts_before = content.split(start_tag)[0]
        parts_after = content.split(end_tag)[1]
        new_content = parts_before + alert_block + parts_after
    else:
        lines = content.split("\n")
        title_index = -1
        for i, line in enumerate(lines):
            if line.startswith("# "):
                title_index = i
                break
        
        if title_index != -1:
            new_lines = lines[:title_index + 1] + ["", alert_block] + lines[title_index + 1:]
            new_content = "\n".join(new_lines)
        else:
            new_content = alert_block + "\n" + content

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(new_content.strip() + "\n")
    print("README.md successfully updated with updates banner!")

def main():
    track_and_update()

if __name__ == "__main__":
    main()
