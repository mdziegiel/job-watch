import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# The CI target for these tests is the app container with real dependencies.
# Hermes on VM 108 lacks python3-venv/ensurepip, so local pure-logic tests stub
# framework/media modules that are not exercised here. Ugly, but deterministic.
if importlib.util.find_spec("fastapi") is None:
    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def __init__(self, *args, **kwargs): pass
        def add_middleware(self, *args, **kwargs): pass
        def mount(self, *args, **kwargs): pass
        def get(self, *args, **kwargs): return lambda fn: fn
        def post(self, *args, **kwargs): return lambda fn: fn

    class HTTPException(Exception): pass
    class Response:
        def __init__(self, *args, **kwargs): pass

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Response = Response
    sys.modules["fastapi"] = fastapi
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = object
    sys.modules["fastapi.middleware"] = types.ModuleType("fastapi.middleware")
    sys.modules["fastapi.middleware.cors"] = cors
    staticfiles = types.ModuleType("fastapi.staticfiles")
    staticfiles.StaticFiles = lambda *args, **kwargs: object()
    sys.modules["fastapi.staticfiles"] = staticfiles

if importlib.util.find_spec("pydantic") is None:
    pydantic = types.ModuleType("pydantic")
    class BaseModel:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
        def model_dump(self): return dict(self.__dict__)
    pydantic.BaseModel = BaseModel
    sys.modules["pydantic"] = pydantic

if importlib.util.find_spec("docx") is None:
    docx = types.ModuleType("docx")
    docx.Document = lambda *args, **kwargs: object()
    sys.modules["docx"] = docx

if importlib.util.find_spec("reportlab") is None:
    reportlab = types.ModuleType("reportlab")
    sys.modules["reportlab"] = reportlab
    lib = types.ModuleType("reportlab.lib")
    pagesizes = types.ModuleType("reportlab.lib.pagesizes")
    pagesizes.letter = (612, 792)
    styles = types.ModuleType("reportlab.lib.styles")
    styles.getSampleStyleSheet = lambda: {"Title": object(), "BodyText": object()}
    platypus = types.ModuleType("reportlab.platypus")
    platypus.Paragraph = lambda *args, **kwargs: object()
    platypus.Spacer = lambda *args, **kwargs: object()
    class SimpleDocTemplate:
        def __init__(self, *args, **kwargs): pass
        def build(self, *args, **kwargs): pass
    platypus.SimpleDocTemplate = SimpleDocTemplate
    sys.modules["reportlab.lib"] = lib
    sys.modules["reportlab.lib.pagesizes"] = pagesizes
    sys.modules["reportlab.lib.styles"] = styles
    sys.modules["reportlab.platypus"] = platypus

os.environ.setdefault("SCHEDULER_ENABLED", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import main


class JobWatchLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.log_tmp = tempfile.NamedTemporaryFile(delete=False)
        self.log_tmp.close()
        main.DB_PATH = self.tmp.name
        main.JSEARCH_FILTER_LOG_PATH = self.log_tmp.name
        main.JSEARCH_ALLOWED_PUBLISHERS = ["Indeed", "Glassdoor", "ZipRecruiter", "CareerBuilder", "Monster"]
        main.JSEARCH_PER_SCRAPE_LIMIT = 1
        main.JSEARCH_MONTHLY_LIMIT = 200
        main.JSEARCH_ENABLED = True
        main.JSEARCH_RAPIDAPI_KEY = ""
        main.ANTHROPIC_API_KEY = ""
        main.init_db()

    def tearDown(self):
        for path in (self.tmp.name, self.log_tmp.name):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def job(self, **overrides):
        data = {
            "source": "Test",
            "title": "Network Administrator",
            "company": "MRDTech",
            "location": "Lowell, MA",
            "salary": "$120,000",
            "url": "https://example.com/jobs/123?utm_source=garbage",
            "description": "Azure Intune network systems role",
            "remote_type": "hybrid",
        }
        data.update(overrides)
        return data

    def count_jobs(self):
        with main.connect() as c:
            return c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def test_same_canonical_url_is_not_new_on_rescrape(self):
        first, first_is_new = main.upsert_job(self.job())
        second, second_is_new = main.upsert_job(self.job(title="Different scraped title", url="https://example.com/jobs/123?ref=again#section"))

        self.assertTrue(first_is_new)
        self.assertFalse(second_is_new)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.count_jobs(), 1)

    def test_same_title_company_location_is_not_new_even_with_different_url(self):
        first, first_is_new = main.upsert_job(self.job(url="https://jobs.example/a"))
        second, second_is_new = main.upsert_job(self.job(url="https://other.example/b?x=1"))

        self.assertTrue(first_is_new)
        self.assertFalse(second_is_new)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.count_jobs(), 1)

    def test_same_title_company_different_location_is_new_when_url_differs(self):
        _, first_is_new = main.upsert_job(self.job(url="https://jobs.example/a", location="Lowell, MA"))
        _, second_is_new = main.upsert_job(self.job(url="https://jobs.example/b", location="Boston, MA"))

        self.assertTrue(first_is_new)
        self.assertTrue(second_is_new)
        self.assertEqual(self.count_jobs(), 2)

    def test_alerts_only_unalerted_first_insert_rows_and_marks_them(self):
        existing, _ = main.upsert_job(self.job(url="https://jobs.example/existing"))
        new_one, _ = main.upsert_job(self.job(title="Systems Administrator", company="Acme", location="Nashua, NH", url="https://jobs.example/new"))
        with main.connect() as c:
            c.execute("UPDATE jobs SET alerted=1 WHERE id=?", (existing["id"],))

        with patch.object(main, "send_telegram") as telegram:
            main.alert_new_jobs([existing, new_one])

        telegram.assert_called_once()
        self.assertEqual(telegram.call_args.args[0]["id"], new_one["id"])
        with main.connect() as c:
            self.assertEqual(c.execute("SELECT alerted FROM jobs WHERE id=?", (new_one["id"],)).fetchone()[0], 1)

    def test_more_than_limit_sends_one_summary_not_individual_alerts(self):
        jobs = []
        for i in range(main.ALERT_BATCH_LIMIT + 1):
            saved, is_new = main.upsert_job(self.job(title=f"Endpoint Engineer {i}", company=f"Company {i}", location=f"City {i}, MA", url=f"https://jobs.example/{i}"))
            self.assertTrue(is_new)
            jobs.append(saved)

        with patch.object(main, "send_telegram") as telegram, \
             patch.object(main, "send_telegram_summary") as telegram_summary:
            main.alert_new_jobs(jobs)

        telegram.assert_not_called()
        telegram_summary.assert_called_once_with(main.ALERT_BATCH_LIMIT + 1)
        with main.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs WHERE alerted=0").fetchone()[0], 0)

    def test_init_marks_existing_rows_as_alerted(self):
        with main.connect() as c:
            c.execute(
                "INSERT INTO jobs(source,title,company,location,fingerprint,first_seen,last_seen,alerted) VALUES(?,?,?,?,?,?,?,0)",
                ("Seed", "Old Job", "Old Co", "Lowell, MA", "seed-old", main.utcnow(), main.utcnow()),
            )
        main.init_db()
        with main.connect() as c:
            self.assertEqual(c.execute("SELECT alerted FROM jobs WHERE fingerprint='seed-old'").fetchone()[0], 1)

    def test_linkedin_parser_extracts_company_from_public_card(self):
        sample = '''
        <li class="jobs-search-results__list-item">
          <div class="base-card job-search-card">
            <a href="https://www.linkedin.com/jobs/view/123456789?trk=public_jobs_jserp-result_search-card" class="base-card__full-link">
              <span class="sr-only">Network Administrator</span>
            </a>
            <h3 class="base-search-card__title">Network Administrator</h3>
            <h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/acme">Acme Networks</a></h4>
            <span class="job-search-card__location">Lowell, MA</span>
            <time datetime="2026-06-10">2 days ago</time>
          </div>
        </li>
        '''
        jobs = main.parse_linkedin_jobs(sample, "Fallback Title")
        self.assertEqual(jobs[0]["company"], "Acme Networks")
        self.assertEqual(jobs[0]["title"], "Network Administrator")
        self.assertEqual(jobs[0]["location"], "Lowell, MA")
        self.assertTrue(jobs[0]["posted_at"].startswith("2026-06-10"))

    def test_linkedin_parser_falls_back_to_company_slug(self):
        sample = '<div class="base-card job-search-card"><a href="https://www.linkedin.com/jobs/view/endpoint-engineer-at-acme-networks-123456789">Endpoint Engineer</a></div>'
        jobs = main.parse_linkedin_jobs(sample, "Endpoint Engineer")
        self.assertEqual(jobs[0]["company"], "Acme Networks")
        self.assertEqual(main.linkedin_company_from_url("https://www.linkedin.com/jobs/view/it-systems-analyst-at-assetwatch%C2%AE-4407619724"), "Assetwatch")

    def test_startup_backfills_old_linkedin_company_names_from_url(self):
        with main.connect() as c:
            c.execute(
                "INSERT INTO jobs(source,title,company,location,fingerprint,first_seen,last_seen,url) VALUES(?,?,?,?,?,?,?,?)",
                ("LinkedIn", "Endpoint Engineer", "", "Lowell", "li", "2024-01-01", "2024-01-01", "https://www.linkedin.com/jobs/view/endpoint-engineer-at-acme-networks-123456789"),
            )
        self.assertEqual(main.backfill_linkedin_companies(), 1)
        with main.connect() as c:
            self.assertEqual(c.execute("SELECT company FROM jobs WHERE fingerprint='li'").fetchone()[0], "Acme Networks")

    def test_startup_backfills_malformed_linkedin_title_blob(self):
        with main.connect() as c:
            c.execute(
                "INSERT INTO jobs(source,title,company,location,fingerprint,first_seen,last_seen,url) VALUES(?,?,?,?,?,?,?,?)",
                ("LinkedIn", "System Administrator\n\nGreen Plant\nUnited States\n1 day ago", "", "", "blob", "2024-01-01", "2024-01-01", "https://www.linkedin.com/jobs/view/system-administrator-4427025670"),
            )
        self.assertEqual(main.backfill_linkedin_companies(), 1)
        with main.connect() as c:
            row = c.execute("SELECT title, company, location FROM jobs WHERE fingerprint='blob'").fetchone()
        self.assertEqual(row["title"], "System Administrator")
        self.assertEqual(row["company"], "Green Plant")
        self.assertEqual(row["location"], "United States")

    def test_dedupe_jobs_keeps_newest_exact_title_company_location(self):
        with main.connect() as c:
            c.execute(
                "INSERT INTO jobs(source,title,company,location,fingerprint,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                ("Seed", "Same", "Co", "Lowell", "old", "2024-01-01", "2024-01-01"),
            )
            c.execute(
                "INSERT INTO jobs(source,title,company,location,fingerprint,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                ("Seed", "Same", "Co", "Lowell", "new", "2025-01-01", "2025-01-01"),
            )
        self.assertEqual(main.dedupe_jobs(), 1)
        with main.connect() as c:
            self.assertEqual(c.execute("SELECT fingerprint FROM jobs WHERE title='Same'").fetchone()[0], "new")
    def test_scoring_varies_by_title_location_salary_and_skills(self):
        strong = main.score_job(self.job(title="Senior Network Administrator", location="Lowell, MA", salary="$120,000", description="Azure Intune Hyper-V firewall network Active Directory", remote_type="hybrid"))[0]
        weak = main.score_job(self.job(title="Help Desk Technician", location="Worcester, MA", salary="$55,000", description="password resets and tickets", remote_type="onsite", url="https://jobs.example/weak"))[0]
        remote = main.score_job(self.job(title="Endpoint Engineer", location="Remote", salary="$110,000", description="Intune Autopilot Microsoft 365 endpoint security", remote_type="remote", url="https://jobs.example/remote"))[0]
        self.assertNotEqual(strong, weak)
        self.assertNotEqual(remote, weak)
        self.assertGreater(strong, weak)
        self.assertGreater(remote, weak)

    def test_jobs_endpoint_defaults_to_last_7_days_newest_first_and_date_labels(self):
        with main.connect() as c:
            c.execute("INSERT INTO jobs(source,title,company,location,fingerprint,first_seen,last_seen,match_score) VALUES(?,?,?,?,?,?,?,?)", ("Seed", "Old", "Co", "Lowell", "old-filter", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", 10))
        fresh, _ = main.upsert_job(self.job(url="https://jobs.example/fresh"))
        rows = main.jobs()
        self.assertTrue(any(r["id"] == fresh["id"] for r in rows))
        self.assertFalse(any(r.get("fingerprint") == "old-filter" for r in rows))
        self.assertIn("Posted", rows[0]["posted_label"])
        all_rows = main.jobs(date_range="all", sort="oldest")
        self.assertEqual(all_rows[0]["fingerprint"], "old-filter")

    def test_linkedin_relative_date_becomes_posted_label(self):
        sample = """
        <div class="base-card job-search-card">
          <a href="https://www.linkedin.com/jobs/view/systems-administrator-at-acme-123">Systems Administrator</a>
          <time class="job-search-card__listdate">2 days ago</time>
        </div>
        """
        job = main.parse_linkedin_jobs(sample, "Systems Administrator")[0]
        self.assertTrue(job["posted_at"])
        saved, is_new = main.upsert_job(job)
        self.assertTrue(is_new)
        self.assertEqual(saved["posted_label"], "Posted 2 days ago")

    def test_row_without_actual_posted_date_uses_posted_label(self):
        saved, _ = main.upsert_job(self.job(url="https://jobs.example/fallback"))
        self.assertEqual(saved["posted_at"], "")
        self.assertIn("Posted", saved["posted_label"])

    def test_jsearch_normalization_maps_api_payload_to_existing_schema(self):
        raw = {
            "job_title": "Senior Network Administrator",
            "employer_name": "Acme Networks",
            "job_city": "Lowell",
            "job_state": "MA",
            "job_country": "US",
            "job_min_salary": 115000,
            "job_max_salary": 125000,
            "job_salary_currency": "USD",
            "job_salary_period": "year",
            "job_apply_link": "https://jobs.example/acme-netadmin",
            "job_publisher": "Indeed",
            "job_description": "Hybrid Azure Intune network role",
            "job_is_remote": False,
            "job_posted_at_datetime_utc": "2026-06-23T12:34:56Z",
        }
        job = main.normalize_jsearch_job(raw, "Network Administrator")
        self.assertEqual(job["source"], "jsearch")
        self.assertEqual(job["publisher"], "Indeed")
        self.assertEqual(job["title"], "Senior Network Administrator")
        self.assertEqual(job["company"], "Acme Networks")
        self.assertEqual(job["location"], "Lowell, MA, US")
        self.assertEqual(job["salary"], "$115,000 - $125,000 / year")
        self.assertEqual(job["url"], "https://jobs.example/acme-netadmin")
        self.assertEqual(job["remote_type"], "hybrid")
        self.assertTrue(job["posted_at"].startswith("2026-06-23T12:34:56"))

    def test_jsearch_dedupes_against_existing_linkedin_by_title_company_location(self):
        linkedin, first_is_new = main.upsert_job(self.job(source="LinkedIn", title="Endpoint Engineer", company="Acme", location="Lowell, MA", url="https://linkedin.example/job"))
        jsearch, second_is_new = main.upsert_job(self.job(source="jsearch", title="Endpoint Engineer", company="Acme", location="Lowell, MA", url="https://jsearch.example/job"))
        self.assertTrue(first_is_new)
        self.assertFalse(second_is_new)
        self.assertEqual(linkedin["id"], jsearch["id"])
        self.assertEqual(self.count_jobs(), 1)

    def test_existing_row_update_backfills_publisher_without_breaking_dedupe(self):
        first, first_is_new = main.upsert_job(self.job(source="LinkedIn", title="Systems Administrator", company="Acme", location="Lowell, MA", url="https://linkedin.example/acme"))
        second, second_is_new = main.upsert_job(self.job(source="jsearch", publisher="Indeed", title="Systems Administrator", company="Acme", location="Lowell, MA", url="https://indeed.example/acme"))
        self.assertTrue(first_is_new)
        self.assertFalse(second_is_new)
        self.assertEqual(first["id"], second["id"])
        with main.connect() as c:
            row = c.execute("SELECT publisher FROM jobs WHERE id=?", (first["id"],)).fetchone()
        self.assertEqual(row["publisher"], "Indeed")

    def test_jsearch_quota_prevents_api_call_when_exhausted(self):
        main.JSEARCH_RAPIDAPI_KEY = "test-key"
        main.JSEARCH_ENABLED = True
        main.JSEARCH_MONTHLY_LIMIT = 1
        main.setting_put("jsearch_usage_month", main.jsearch_month_key())
        main.setting_put("jsearch_usage_count", "1")
        with patch.object(main.requests, "get") as get:
            self.assertEqual(main.scrape_jsearch("Network Administrator"), [])
        get.assert_not_called()

    def test_jsearch_records_one_request_and_normalizes_response(self):
        main.JSEARCH_RAPIDAPI_KEY = "test-key"
        main.JSEARCH_ENABLED = True
        main.JSEARCH_MONTHLY_LIMIT = 200
        class FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {"data": [{"job_title": "Network Administrator", "employer_name": "Acme", "job_location": "Lowell, MA", "job_apply_link": "https://jobs.example/1", "job_publisher": "Indeed"}]}
        with patch.object(main.requests, "get", return_value=FakeResponse()) as get:
            jobs = main.scrape_jsearch("Network Administrator")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source"], "jsearch")
        self.assertEqual(main.jsearch_usage()["used"], 1)
        get.assert_called_once()

    def test_jsearch_filters_publishers_after_api_call_and_logs_breakdown(self):
        main.JSEARCH_RAPIDAPI_KEY = "test-key"
        main.JSEARCH_ENABLED = True
        main.JSEARCH_MONTHLY_LIMIT = 200
        main.JSEARCH_ALLOWED_PUBLISHERS = ["Indeed", "Glassdoor", "ZipRecruiter", "CareerBuilder", "Monster"]
        rows = [
            {"job_title": "Network Administrator", "employer_name": "Acme", "job_location": "Lowell, MA", "job_apply_link": "https://jobs.example/indeed", "job_publisher": "indeed"},
            {"job_title": "Systems Administrator", "employer_name": "Beta", "job_location": "Lowell, MA", "job_apply_link": "https://jobs.example/linkedin", "job_publisher": "LinkedIn"},
            {"job_title": "Endpoint Engineer", "employer_name": "Gamma", "job_location": "Remote", "job_apply_link": "https://jobs.example/jobilize", "job_publisher": "Jobilize"},
            {"job_title": "Desktop Engineer", "employer_name": "Delta", "job_location": "Boston, MA", "job_apply_link": "https://jobs.example/whatjobs", "job_publisher": "WhatJobs"},
            {"job_title": "M365 Engineer", "employer_name": "Echo", "job_location": "Remote", "job_apply_link": "https://jobs.example/zip", "job_publisher": "ZipRecruiter"},
        ]
        class FakeResponse:
            def raise_for_status(self): pass
            def json(self): return {"data": rows}
        with patch.object(main.requests, "get", return_value=FakeResponse()):
            jobs = main.scrape_jsearch("Network Administrator")
        self.assertEqual([job["publisher"] for job in jobs], ["indeed", "ZipRecruiter"])
        self.assertEqual(main.jsearch_usage()["used"], 1)
        with open(main.JSEARCH_FILTER_LOG_PATH, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("5 found", log)
        self.assertIn("1 indeed kept", log)
        self.assertIn("1 LinkedIn filtered", log)
        self.assertIn("1 Jobilize filtered", log)
        self.assertIn("1 WhatJobs filtered", log)
        self.assertIn("1 ZipRecruiter kept", log)

    def test_scrape_all_only_inserts_allowlisted_jsearch_publishers(self):
        main.JSEARCH_RAPIDAPI_KEY = "test-key"
        main.JSEARCH_ENABLED = True
        main.JSEARCH_MONTHLY_LIMIT = 200
        main.JSEARCH_PER_SCRAPE_LIMIT = 1
        main.JSEARCH_ALLOWED_PUBLISHERS = ["Indeed", "Glassdoor", "ZipRecruiter", "CareerBuilder", "Monster"]
        rows = [
            {"job_title": "Network Administrator", "employer_name": "Acme", "job_location": "Lowell, MA", "job_apply_link": "https://jobs.example/indeed", "job_publisher": "Indeed"},
            {"job_title": "Systems Administrator", "employer_name": "Beta", "job_location": "Lowell, MA", "job_apply_link": "https://jobs.example/linkedin", "job_publisher": "LinkedIn"},
            {"job_title": "Endpoint Engineer", "employer_name": "Gamma", "job_location": "Remote", "job_apply_link": "https://jobs.example/talent", "job_publisher": "Talent.com"},
        ]
        class FakeResponse:
            def raise_for_status(self): pass
            def json(self): return {"data": rows}
        with patch.object(main, "scrape_indeed_mcp", return_value=[]), \
             patch.object(main, "scrape_linkedin", return_value=[]), \
             patch.object(main, "scrape_dice", return_value=[]), \
             patch.object(main, "scrape_ziprecruiter", return_value=[]), \
             patch.object(main.requests, "get", return_value=FakeResponse()):
            result = main.scrape_all()
        self.assertEqual(result["found"], 1)
        with main.connect() as c:
            rows = c.execute("SELECT source, publisher FROM jobs ORDER BY id").fetchall()
        self.assertEqual([(row["source"], row["publisher"]) for row in rows], [("jsearch", "Indeed")])
        with open(main.JSEARCH_FILTER_LOG_PATH, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("3 found", log)
        self.assertIn("1 Indeed kept", log)
        self.assertIn("1 LinkedIn filtered", log)
        self.assertIn("1 Talent.com filtered", log)


if __name__ == "__main__":
    unittest.main()
