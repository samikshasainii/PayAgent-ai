#!/usr/bin/env python3
"""
PayAgent AI — Safe Hosted Database Seeding Script

Loads connection parameters from a gitignored .env.hosted file, verifies Supabase pooler
configuration and production environment posture, and initializes demo data without
shell export patterns or credential leaks.
"""
import os
import sys
import subprocess
from urllib.parse import urlsplit
from dotenv import dotenv_values

def main():
    # 1. Refuses any flags or arguments outright
    if len(sys.argv) > 1:
        print("Error: scripts/seed_hosted.py does not accept command-line flags or arguments.", file=sys.stderr)
        sys.exit(2)

    # 2. Check that .env.hosted is gitignored
    env_file = ".env.hosted"
    git_check = subprocess.run(["git", "check-ignore", "-q", env_file], check=False)
    if git_check.returncode != 0:
        print(f"Error: Refusing to proceed. {env_file} is not gitignored.", file=sys.stderr)
        sys.exit(2)

    if not os.path.isfile(env_file):
        print(f"Error: {env_file} file not found.", file=sys.stderr)
        sys.exit(2)

    # 3. Load .env.hosted into os.environ BEFORE importing anything from src
    hosted_env = dotenv_values(env_file)
    for k, v in hosted_env.items():
        if v is not None:
            os.environ[k] = v

    # 4. Enforce DATABASE_URL starts with 'postgresql+asyncpg://' and contains '.pooler.supabase.com'
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url.startswith("postgresql+asyncpg://") or ".pooler.supabase.com" not in db_url:
        print(
            "Error: DATABASE_URL must start with 'postgresql+asyncpg://' and contain '.pooler.supabase.com'.",
            file=sys.stderr,
        )
        sys.exit(2)

    # 5. Enforce APP_ENV == "production"
    if os.environ.get("APP_ENV") != "production":
        print("Error: APP_ENV must be set to 'production'.", file=sys.stderr)
        sys.exit(2)

    # 6. Print the DB host only (never user, password or full URL)
    parsed = urlsplit(db_url)
    db_host = parsed.hostname or "unknown"
    print(f"Connecting to hosted Supabase database host: {db_host}")

    # 7. Import src and run seed_database(reset=False, keys_output_path=".env.hosted.agents")
    import asyncio
    from src.seed import seed_database

    keys_output_path = ".env.hosted.agents"
    asyncio.run(seed_database(reset=False, keys_output_path=keys_output_path))
    print("Hosted database seeding complete.")

if __name__ == "__main__":
    main()
