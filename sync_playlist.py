import os
import re
import json
import csv
import io
from datetime import datetime, timezone

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SHEET_RANGE = 'Foglio1!A2:G'
HEADERS = ['Track name', 'Artist name', 'Album', 'Playlist name', 'Type', 'ISRC', 'Spotify - id']


def get_playlist_tracks(playlist_id):
    """Legge la pagina embed pubblica della playlist, senza API key né login."""
    url = f'https://open.spotify.com/embed/playlist/{playlist_id}'
    resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    resp.raise_for_status()
    html = resp.text

    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
    if not match:
        raise RuntimeError(
            'Struttura pagina Spotify non riconosciuta: Spotify potrebbe aver cambiato '
            'il formato della pagina embed. Serve aggiornare il parsing.'
        )

    data = json.loads(match.group(1))
    found = []
    _find_tracks(data, found)
    playlist_name = _find_playlist_name(data) or 'Playlist'

    tracks, seen = [], set()
    for t in found:
        uri = t.get('uri', '')
        if not uri.startswith('spotify:track:'):
            continue
        track_id = uri.replace('spotify:track:', '')
        if track_id in seen:
            continue
        seen.add(track_id)
        tracks.append({
            'id': track_id,
            'name': t.get('name', ''),
            'album': (t.get('album') or {}).get('name', ''),
            'artist': ', '.join(a.get('name', '') for a in t.get('artists', [])),
            'isrc': ''  # non disponibile dalla pagina embed pubblica
        })
    return playlist_name, tracks


def _find_tracks(node, found):
    """Cerca ricorsivamente oggetti che sembrano una traccia (name + artists + uri di tipo track)."""
    if isinstance(node, list):
        for item in node:
            _find_tracks(item, found)
    elif isinstance(node, dict):
        if node.get('name') and isinstance(node.get('artists'), list) and str(node.get('uri', '')).startswith('spotify:track:'):
            found.append(node)
        else:
            for value in node.values():
                _find_tracks(value, found)


def _find_playlist_name(node):
    if isinstance(node, dict):
        if node.get('type') == 'playlist' and isinstance(node.get('name'), str):
            return node['name']
        for value in node.values():
            result = _find_playlist_name(value)
            if result:
                return result
    return None


def get_google_services():
    creds_info = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
    sheets = build('sheets', 'v4', credentials=creds)
    drive = build('drive', 'v3', credentials=creds)
    return sheets, drive


def get_existing_ids(sheets, sheet_id):
    resp = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=SHEET_RANGE).execute()
    ids = set()
    for row in resp.get('values', []):
        if len(row) >= 7 and row[6]:
            ids.add(row[6])
    return ids


def rewrite_sheet(sheets, sheet_id, playlist_name, tracks):
    """Ricostruisce il corpo del foglio a specchio della playlist (gestisce aggiunte, rimozioni, riordini)."""
    sheets.spreadsheets().values().clear(spreadsheetId=sheet_id, range=SHEET_RANGE).execute()
    if not tracks:
        return
    rows = [[t['name'], t['artist'], t['album'], playlist_name, 'Playlist', t['isrc'], t['id']] for t in tracks]
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range='Foglio1!A2',
        valueInputOption='RAW',
        body={'values': rows}
    ).execute()


def build_csv(playlist_name, tracks):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADERS)
    for t in tracks:
        writer.writerow([t['name'], t['artist'], t['album'], playlist_name, 'Playlist', t['isrc'], t['id']])
    return buf.getvalue()


def upload_csv_to_drive(drive, folder_id, csv_content):
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'Spotify_Library_{stamp}.csv'
    media = MediaIoBaseUpload(io.BytesIO(csv_content.encode('utf-8')), mimetype='text/csv')
    file = drive.files().create(
        body={'name': filename, 'parents': [folder_id]},
        media_body=media,
        fields='id, webViewLink'
    ).execute()
    drive.permissions().create(
        fileId=file['id'],
        body={'role': 'reader', 'type': 'anyone'}
    ).execute()
    return file['webViewLink']


def update_dub_link(api_key, link_id, new_url):
    resp = requests.patch(
        f'https://api.dub.co/links/{link_id}',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'url': new_url}
    )
    print('Aggiornamento ggl.link:', resp.status_code, resp.text)


def main():
    playlist_id = os.environ['SPOTIFY_PLAYLIST_ID']
    sheet_id = os.environ['GOOGLE_SHEET_ID']
    folder_id = os.environ['DRIVE_FOLDER_ID']
    dub_api_key = os.environ.get('DUB_API_KEY')
    dub_link_id = os.environ.get('DUB_LINK_ID')

    sheets, drive = get_google_services()
    existing_ids = get_existing_ids(sheets, sheet_id)

    playlist_name, tracks = get_playlist_tracks(playlist_id)
    current_ids = {t['id'] for t in tracks}

    if existing_ids == current_ids:
        print('Nessun cambiamento')
        return

    added = current_ids - existing_ids
    removed = existing_ids - current_ids
    print(f'Cambiamenti rilevati: +{len(added)} aggiunti, -{len(removed)} rimossi')

    rewrite_sheet(sheets, sheet_id, playlist_name, tracks)
    csv_content = build_csv(playlist_name, tracks)
    file_url = upload_csv_to_drive(drive, folder_id, csv_content)

    if dub_api_key and dub_link_id:
        update_dub_link(dub_api_key, dub_link_id, file_url)


if __name__ == '__main__':
    main()
