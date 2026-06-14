Run the remote DB merge: pull app.db from all validator machines over SSH/Tailscale and merge into the local orchestrator DB.

Steps:
1. Run `python3 scripts/merge_remote_dbs.py --dry-run` and show the summary to the user
2. Ask the user to confirm before applying
3. If confirmed, run `python3 scripts/merge_remote_dbs.py` and report the results
