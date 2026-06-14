Pull app.db from all validator machines over SSH/Tailscale, merge into the local orchestrator DB, then flush any pending completions to Google Sheets.

Steps:
1. Run `python3 scripts/merge_remote_dbs.py --dry-run` and show the summary to the user
2. Ask the user to confirm before applying
3. If confirmed, run `python3 scripts/merge_remote_dbs.py --sheet-sync` and report the results
