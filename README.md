# avacasa-leads-sync

Twice a day (09:00 and 21:00 IST), copies new website leads from the database (`lead_preferences`)
into the **Master Leads** tab of `Avacasa_Leads_Master`, and fills each lead's
session columns from their latest buyer session. Replaces the Gmail Apps Script.

- A lead already on the tab (same Preference ID, email, or phone) is never added again.
- Only these columns are written: Lead ID, Lead Date, Name, Email, Phone, Preference ID,
  Lead Source, Lead Quality / Score, Asset Preference, Location of Interest, Budget,
  Budget Min, Budget Max, Requirement / Notes, Session Duration, DNA Traits,
  Top Searched Properties, Total Interactions. Ops columns are never touched.
- Logs show counts only, never names, emails or phones.

## Setup (once)

1. Google Cloud: create a service account, create a JSON key for it, and enable the
   Google Sheets API on that project.
2. Share the sheet with the service account's email as **Editor**.
3. Repo secrets: `MONGO_CONNECTION_STRING` and `GOOGLE_SERVICE_ACCOUNT_JSON`
   (paste the whole key file).
4. Actions → Leads to sheet → Run workflow with "dry run" ticked; check the counts.
5. Turn off the old Apps Script trigger, or it will keep adding rows from Gmail.

## Run locally

    pip install -r requirements.txt
    python sync.py --dry-run
