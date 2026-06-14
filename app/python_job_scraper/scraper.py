import requests
import os
import json
import logging
import sqlite3
import time
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from linkedin_api import Linkedin
from sentence_transformers import SentenceTransformer

# Force clear environment variables to bypass local proxies (e.g., Colab environments)
os.environ['NO_PROXY'] = '*'
for var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
    os.environ.pop(var, None)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==========================================
# 1. DATA INGESTION MODULE
# ==========================================
class LinkedInJobIngestor:
    def __init__(self, cookies: dict):
        logging.info("Authenticating to LinkedIn (Cookie Injection)...")
        self.api = Linkedin('dummy', 'dummy', authenticate=False)
        cookie_jar = requests.cookies.cookiejar_from_dict(cookies)
        self.api.client.session.cookies.update(cookie_jar)
        jsessionid = cookies.get("JSESSIONID", "").replace('"', '')
        self.api.client.session.headers["csrf-token"] = jsessionid

    @staticmethod
    def clean_html(raw_html: str) -> str:
        if not raw_html: return ""
        return BeautifulSoup(raw_html, "html.parser").get_text(separator="\n ", strip=True)[:3000]

    def fetch_single_job(self, job_urn: str) -> dict:
        job_id = job_urn.split(':')[-1]
        try:
            job_data = self.api.get_job(job_id)
            title = job_data.get('title', 'Unknown Title')
            desc_data = job_data.get('description')
            raw_description = desc_data.get('text', '') if isinstance(desc_data, dict) else ''

            return {
                "job_id": job_id,
                "title": title,
                "description": self.clean_html(raw_description),
                "url": f"https://www.linkedin.com/jobs/view/{job_id}",
                "status": "success"
            }
        except Exception as e:
            logging.error(f"API Error loading {job_id}: {str(e)}")
            return {"job_id": job_id, "status": "error"}

    def get_jobs_multiple_queries(self, queries: list, location: str, limit_per_query: int = 15) -> list:
        final_jobs = []
        seen_urns = set()

        for query in queries:
            print(f"\n🔍 Searching query: '{query}'...")
            try:
                search_results = self.api.search_jobs(
                    keywords=query, location_name=location, listed_at=24*3600, limit=limit_per_query
                )

                if not search_results or not isinstance(search_results, list):
                    continue

                strict_keyword = query.split()[0].lower()

                for job in search_results:
                    urn = job.get('trackingUrn')
                    title = job.get('title', '').lower()

                    if urn in seen_urns: continue
                    if strict_keyword not in title: continue

                    seen_urns.add(urn)
                    res = self.fetch_single_job(urn)
                    if res["status"] == "success":
                        final_jobs.append(res)
                        print(f"📥 Downloaded: {res.get('title')}")
                    time.sleep(1.2)

            except Exception as e:
                logging.error(f"Query failure '{query}': {str(e)}")

        return final_jobs

# ==========================================
# 2. DATABASE & DETERMINISTIC FILTER
# ==========================================
class JobDatabaseManager:
    def __init__(self, db_path: str = "job_cache.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS processed_jobs (job_id TEXT PRIMARY KEY, title TEXT, status TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")

    def is_job_processed(self, job_id: str) -> bool:
        result = self.conn.execute("SELECT status FROM processed_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return result is not None and result[0] not in ("http_error", "parse_error", "api_error")

    def mark_job_processed(self, job_id: str, title: str, status: str):
        self.conn.execute("INSERT OR REPLACE INTO processed_jobs (job_id, title, status) VALUES (?, ?, ?)", (job_id, title, status))
        self.conn.commit()

    def close(self):
        self.conn.close()

class DeterministicFilter:
    def __init__(self, exclusion_markers: set, mandatory_keywords: set):
        self.exclusion_markers = exclusion_markers
        self.mandatory_keywords = mandatory_keywords

    def evaluate(self, title: str, desc: str) -> bool:
        text = (title + " " + desc).lower()
        if any(m in text for m in self.exclusion_markers): return False
        return any(k in text for k in self.mandatory_keywords)

# ==========================================
# 3. SEMANTICS & PURE REST LLM API
# ==========================================
class HybridScoringEngine:
    def __init__(self, resume_text: str, google_api_key: str):
        logging.info("Initializing Hybrid Engine (Pure REST API)...")
        self.encoder = SentenceTransformer('all-MiniLM-L6-v2')

        # Store API key for direct HTTP requests
        self.api_key = google_api_key

        self.resume_vector = self.encoder.encode([resume_text])[0]
        self.resume_norm = max(np.linalg.norm(self.resume_vector), 1e-8)

    def calc_hard_score(self, job_text: str) -> float:
        job_vector = self.encoder.encode([job_text])[0]
        score = np.dot(self.resume_vector, job_vector) / (self.resume_norm * max(np.linalg.norm(job_vector), 1e-8))
        return max(0.0, score) * 100

    def verify_via_llm(self, resume: str, title: str, job_text: str, rules: str) -> dict:
        # DIRECT URL: Stable v1 branch and standard model name
        api_url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent?key={self.api_key}"

        prompt = f"Resume: {resume}\nJob: {title}\nDesc: {job_text}\nRules: {rules}\nReturn JSON: {{\"logical_pass\": bool, \"reason_for_decision\": \"str\"}}"

        # generationConfig block omitted to bypass HTTP 400 in v1 branch
        payload = {
            "contents": [{"parts": [{"text": prompt}]}]
        }

        try:
            response = requests.post(
                api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15.0
            )

            if response.status_code != 200:
                return {"logical_pass": False, "error_type": "api_error", "reason_for_decision": f"HTTP {response.status_code}: {response.text}"}

            response_data = response.json()
            raw_text = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()

            if raw_text.startswith("```"):
                raw_text = raw_text.replace("```json", "").replace("```", "").strip()

            return json.loads(raw_text)

        except json.JSONDecodeError:
            return {"logical_pass": False, "error_type": "json_error", "reason_for_decision": "Failed to parse JSON from model."}
        except Exception as e:
            return {"logical_pass": False, "error_type": "network_error", "reason_for_decision": str(e)}

# ==========================================
# 4. MAIN EXECUTION LOOP
# ==========================================
def main():
    # ---------------------------------------------------------
    # USER CONFIGURATION - MODIFY THESE VARIABLES TO ADAPT TOOL
    # ---------------------------------------------------------
    
    # 1. Credentials
    API_KEY = "AQ.Ab8RN6LfOFVsytuytutt6tu6utu6Wv_Ol7cplyYYsFZGF7w" 
    COOKIES = {
        "li_at": "AQEDAWm8FEEA1ea6AAABnsGjeOQAAAGe5a_8uyguygyugygughvgfcfgcgcgf-3YkCxSXArRDS4kPsgU58Hud0lFMNvITlI8uMZUaLgkY8E1a_Nw5a13sIISGl44Dkb35XmhCrq7nYcKO2", 
        "JSESSIONID": "ajax:8868887989997200"
    }

    # 2. Candidate Baseline (The text used for vector similarity scoring and LLM context)
    RESUME = "Cloud/DevOps: AWS, Terraform, Ansible, Docker, Kubernetes."
    
    # 3. Search Parameters
    QUERIES = ["DevOps", "SRE", "Cloud", "AWS", "Infrastructure"]
    LOCATION = "European Economic Area"
    QUERY_LIMIT = 1000
    
    # 4. Deterministic Filter Sets (Lowercase text only)
    # EXCLUSION_MARKERS: Jobs containing any of these words are dropped immediately before LLM evaluation.
    EXCLUSION_MARKERS = {"senior", "lead", "principal", "head", "manager", "sr.", "director", "expert"}
    # MANDATORY_KEYWORDS: Jobs must contain at least one of these words to proceed.
    MANDATORY_KEYWORDS = {"aws", "terraform", "kubernetes", "docker", "ansible", "cloud", "infrastructure"}
    
    # 5. LLM Evaluation Logic
    # Strict logical constraints sent to the Gemini API for final decision.
    LLM_RULES = "Reject if migration from AWS or >3yr experience required."
    
    # ---------------------------------------------------------

    ingestor = LinkedInJobIngestor(COOKIES)
    db = JobDatabaseManager()
    fast_filter = DeterministicFilter(EXCLUSION_MARKERS, MANDATORY_KEYWORDS)
    engine = HybridScoringEngine(RESUME, API_KEY)

    try:
        approved_jobs = []
        jobs = ingestor.get_jobs_multiple_queries(QUERIES, LOCATION, limit_per_query=QUERY_LIMIT)
        for job in jobs:
            jid, title, desc = job["job_id"], job["title"], job["description"]

            if db.is_job_processed(jid):
                print(f"⏩ Skipped (Already in DB): {title}")
                continue

            if not fast_filter.evaluate(title, desc):
                db.mark_job_processed(jid, title, "rejected_fast")
                print(f"✂️ Killed by fast filter (Missing keywords or contains exclusion markers): {title}")
                continue

            score = engine.calc_hard_score(desc)
            if score < 10.0:
                db.mark_job_processed(jid, title, "rejected_score")
                print(f"📉 Killed by vector scoring (Score {score:.1f} < 10.0): {title}")
                continue

            print("🧠 Transmitting data to Gemini API...")
            res = engine.verify_via_llm(RESUME, title, desc, LLM_RULES)
            time.sleep(4)

            if "error_type" in res:
                print(f"⚠️ LLM Error for {title}: {res.get('reason_for_decision')}")
                db.mark_job_processed(jid, title, res["error_type"])
                continue

            if res.get("logical_pass"):
                print(f"✅ APPROVED: {title} | Reason: {res.get('reason_for_decision')}")
                db.mark_job_processed(jid, title, "approved")
                job["llm_reason"] = res.get("reason_for_decision")
                job["hard_score"] = round(score, 1)
                approved_jobs.append(job)
            else:
                print(f"❌ REJECTED BY LLM: {title} | Reason: {res.get('reason_for_decision')}")
                db.mark_job_processed(jid, title, "rejected_llm")
    finally:
        db.close()
        print("Analysis complete.")

def print_approved_jobs():
    """Reads the SQLite database and prints the approved jobs."""
    conn = sqlite3.connect('job_cache.db')
    query = "SELECT job_id, title, status, timestamp FROM processed_jobs WHERE status = 'approved'"
    df_approved = pd.read_sql_query(query, conn)
    conn.close()

    if df_approved.empty:
        print("No approved jobs found yet.")
    else:
        print(f"Total approved jobs found: {len(df_approved)}")
        print(df_approved.to_string())

if __name__ == "__main__":
    main()
    print("\n--- Generating Report ---")
    print_approved_jobs()