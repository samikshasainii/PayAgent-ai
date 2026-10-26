import json
import os
import re
import pytest

COLLECTION_PATH = "docs/postman/PayAgent.postman_collection.json"
ENVIRONMENT_PATH = "docs/postman/PayAgent.local.postman_environment.json"

ALLOWED_PA_KEYS = {
    "pa_demo_agent_key_secret_123",
    "pa_demo_agent_key_secret_456",
}

ALLOWED_EMAIL_DOMAINS = {
    "example.com",
}

def test_postman_files_exist_and_valid_json():
    assert os.path.isfile(COLLECTION_PATH), f"Missing {COLLECTION_PATH}"
    assert os.path.isfile(ENVIRONMENT_PATH), f"Missing {ENVIRONMENT_PATH}"

    with open(COLLECTION_PATH, "r", encoding="utf-8") as f:
        col = json.load(f)
    with open(ENVIRONMENT_PATH, "r", encoding="utf-8") as f:
        env = json.load(f)

    assert "info" in col and "item" in col
    assert "values" in env

def test_postman_collection_folders_order():
    with open(COLLECTION_PATH, "r", encoding="utf-8") as f:
        col = json.load(f)

    folder_names = [item["name"] for item in col["item"]]
    expected_folders = [
        "Setup",
        "Happy path",
        "Over budget",
        "Replay",
        "Injection",
        "HITL approve",
        "Status"
    ]
    assert folder_names == expected_folders, f"Folder structure mismatch: {folder_names} != {expected_folders}"

def test_postman_no_secret_leaks_or_live_credentials():
    with open(COLLECTION_PATH, "r", encoding="utf-8") as f:
        col_text = f.read()
    with open(ENVIRONMENT_PATH, "r", encoding="utf-8") as f:
        env_text = f.read()

    combined_text = col_text + "\n" + env_text

    # 1. Check for unauthorized pa_ keys
    pa_keys = re.findall(r"\bpa_[A-Za-z0-9_-]+\b", combined_text)
    for k in pa_keys:
        assert k in ALLOWED_PA_KEYS, f"Unauthorized agent API key found in Postman files: {k}"

    # 2. Check for database URLs
    assert "postgresql" not in combined_text.lower(), "Found PostgreSQL connection string in Postman files"
    assert "supabase" not in combined_text.lower(), "Found Supabase URL/domain in Postman files"
    assert "sqlite:///" not in combined_text.lower(), "Found hardcoded SQLite database path in Postman files"

    # 3. Check for unauthorized emails
    emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", combined_text)
    for email in emails:
        domain = email.split("@")[1].lower()
        assert domain in ALLOWED_EMAIL_DOMAINS, f"Non-placeholder email found in Postman files: {email}"
        assert not email.lower().startswith("sb-"), f"PayPal sandbox email found in Postman files: {email}"
