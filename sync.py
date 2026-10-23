"""Copy website leads from the database into the Avacasa_Leads_Master sheet.

Runs every few minutes from GitHub Actions. Two passes, both idempotent:

1. New leads: every `lead_preferences` doc not already on the "Master Leads"
   tab becomes a new row. "Already on the tab" means the same Preference ID,
   the same email, or the same phone (last 10 digits), so leads typed in by
   hand or added by the old Gmail script are never duplicated.
2. Sessions: rows whose email has a buyer session get Session Duration, DNA
   Traits, Top Searched Properties and Total Interactions from the latest
   session, computed the same way the "Buyer Session Profile" email is.

Columns are found by header name, never by position. Only the columns below
are written; everything the ops team fills (status, notes, owner...) is never
touched.

    python sync.py            # write
    python sync.py --dry-run  # print what would change (counts only)
"""
import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials
from pymongo import MongoClient

SHEET_ID = os.environ.get('LEADS_SHEET_ID', '1koTAaxxNFzjtXu-SjsDp3h7gGXdx3YcJ1I18g7u-bQ4')
TAB = os.environ.get('LEADS_SHEET_TAB', 'Master Leads')
IST = timezone(timedelta(hours=5, minutes=30))

# Sheet header (lower-cased) -> key used below.
COLUMNS = {
    'lead id': 'lead_id', 'lead date': 'lead_date', 'name': 'name', 'email': 'email',
    'phone': 'phone', 'preference id': 'pref_id', 'lead source': 'source',
    'lead quality / score': 'score', 'asset preference': 'asset', 'location of interest': 'locations',
    'budget': 'budget', 'budget min': 'budget_min', 'budget max': 'budget_max',
    'requirement / notes': 'notes', 'session duration': 'duration', 'dna traits': 'traits',
    'top searched properties': 'top_props', 'total interactions': 'interactions',
}
SESSION_KEYS = ('duration', 'traits', 'top_props', 'interactions')


def digits(v):
    return re.sub(r'\D', '', str(v or ''))


def phone_key(v):
    """Last 10 digits: '+91 98…' and '98…' are the same number."""
    d = digits(v)
    return d[-10:] if len(d) >= 10 else d


def norm_email(v):
    return str(v or '').strip().lower()


def budget_bounds(ranges):
    """('INR 50L - INR 1 Cr', 'INR 1 Cr - INR 2 Cr') -> ('INR 50L', 'INR 2 Cr').
    'Under INR 50L' has no lower bound."""
    parts = []
    for r in ranges or []:
        r = str(r).strip()
        if r.lower().startswith('under '):
            parts += ['', r[6:].strip()]
        else:
            parts += [p.strip() for p in re.split(r'\s+-\s+', r) if p.strip()]
    if not parts:
        return '', ''
    return parts[0], parts[-1]


def lead_row(doc, lead_id):
    """The sheet values for one lead_preferences doc, keyed like COLUMNS."""
    phone = doc.get('phone') or {}
    number = digits(phone.get('number'))
    created = doc.get('createdAt')
    if isinstance(created, datetime):
        created = (created if created.tzinfo else created.replace(tzinfo=timezone.utc)).astimezone(IST)
    journey = ((doc.get('context') or {}).get('journey') or {})
    captured = ' on '.join(str(p) for p in (journey.get('sourceComponent'), journey.get('sourcePage')) if p)
    score = doc.get('leadScore')
    lo, hi = budget_bounds(doc.get('budgetRange'))
    return {
        'lead_id': lead_id,
        'lead_date': created.strftime('%d-%b-%Y') if isinstance(created, datetime) else '',
        'name': (doc.get('name') or '').strip(),
        'email': (doc.get('email') or '').strip(),
        'phone': f"+{digits(phone.get('countryCode'))}{number}" if number else '',
        'pref_id': str(doc['_id']),
        'source': captured or 'Website',
        'score': f'{score} / 100' if score is not None else '',
        'asset': ', '.join(doc.get('exploringFor') or []),
        'locations': ', '.join(doc.get('preferredLocations') or []),
        'budget': ', '.join(doc.get('budgetRange') or []),
        'budget_min': lo,
        'budget_max': hi,
        'notes': (doc.get('additionalInfo') or '').strip(),
    }


def session_values(db, session):
    """Session columns for one recommendation_session doc, built like the
    Buyer Session Profile email (session_digest_service.compose_digest)."""
    q = {'session_id': session['session_id']} if session.get('session_id') else None
    if q is None:
        return None
    impressions = list(db['property_recommendation'].find(q).sort('created_at', 1).limit(50))
    interactions = db['recommendation_interaction'].count_documents(q)
    start, last = session.get('started_at'), session.get('last_activity_at')
    minutes = max(1, int((last - start).total_seconds() // 60)) if start and last and last > start else 1
    traits = []
    for imp in reversed(impressions):
        if (imp.get('buyer_dna') or {}).get('traits'):
            traits = imp['buyer_dna']['traits']
            break
    titles = []
    for imp in impressions:
        for r in (imp.get('results') or [])[:5]:
            t = r.get('title')
            if t and t not in titles:
                titles.append(t)
    return {
        'duration': f'~{minutes} min',
        'traits': ', '.join(f"{t.get('label')} ({t.get('confidence')})" for t in traits),
        'top_props': ', '.join(titles[:5]),
        'interactions': str(interactions),
    }


def a1(row, col):
    """1-based row/col -> 'AB12'."""
    s = ''
    while col:
        col, r = divmod(col - 1, 26)
        s = chr(65 + r) + s
    return f'{s}{row}'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)

    db = MongoClient(os.environ['MONGO_CONNECTION_STRING'], serverSelectionTimeoutMS=30000)[
        os.environ.get('MONGO_COLLECTION_NAME', 'avacasa')]
    creds = Credentials.from_service_account_info(
        json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON']),
        scopes=['https://www.googleapis.com/auth/spreadsheets'])
    ws = gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(TAB)

    values = ws.get_all_values()
    headers = [h.strip().lower() for h in values[0]]
    col = {key: headers.index(h) for h, key in COLUMNS.items() if h in headers}
    missing = [h for h in COLUMNS if h not in headers]
    if missing:
        raise SystemExit(f'Sheet is missing columns: {missing}')
    rows = values[1:]

    def cell(r, key):
        i = col[key]
        return r[i].strip() if i < len(r) else ''

    pref_ids = {cell(r, 'pref_id') for r in rows} - {''}
    emails = {norm_email(cell(r, 'email')) for r in rows} - {''}
    phones = {phone_key(cell(r, 'phone')) for r in rows} - {''}
    next_id = max([int(m.group(1)) for r in rows if (m := re.match(r'^L(\d+)$', cell(r, 'lead_id'), re.I))] or [0])

    # 1. New leads, oldest first so Lead IDs follow arrival order.
    new_rows = []
    for doc in db['lead_preferences'].find({}).sort('createdAt', 1):
        email, ph = norm_email(doc.get('email')), phone_key((doc.get('phone') or {}).get('number'))
        if str(doc['_id']) in pref_ids or (email and email in emails) or (ph and ph in phones):
            continue
        next_id += 1
        vals = lead_row(doc, f'L{next_id:05d}')
        row = [''] * len(headers)
        for key, v in vals.items():
            row[col[key]] = v
        new_rows.append(row)
        pref_ids.add(vals['pref_id'])
        emails.add(email)
        phones.add(ph)

    # 2. Session columns for rows (existing + new) whose email has a session.
    latest = {}
    for s in db['recommendation_session'].find({'user_email': {'$nin': [None, '']}}).sort('last_activity_at', 1):
        latest[norm_email(s['user_email'])] = s  # later sessions overwrite earlier
    updates = []
    all_rows = rows + new_rows
    for i, r in enumerate(all_rows):
        s = latest.get(norm_email(cell(r, 'email')))
        if not s:
            continue
        vals = session_values(db, s)
        if not vals:
            continue
        for key in SESSION_KEYS:
            if vals[key] and vals[key] != cell(r, key):
                if i < len(rows):
                    updates.append({'range': a1(i + 2, col[key] + 1), 'values': [[vals[key]]]})
                else:
                    r[col[key]] = vals[key]

    print(f'sheet rows: {len(rows)} | new leads to add: {len(new_rows)} | session cells to update: {len(updates)}')
    if args.dry_run:
        print('dry run: nothing written')
        return
    if new_rows:
        # Write right after the last filled row. append_rows() lets Google
        # pick the spot, and it skips past blank-but-formatted rows (it once
        # put leads under 94 empty rows).
        start = len(values) + 1
        end = start + len(new_rows) - 1
        if end > ws.row_count:
            ws.add_rows(end - ws.row_count)
        ws.update(range_name=f'A{start}:{a1(end, len(headers))}', values=new_rows, value_input_option='RAW')
    if updates:
        ws.batch_update(updates, value_input_option='RAW')
    print('written')


if __name__ == '__main__':
    main()
