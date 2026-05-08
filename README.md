# CMS – 5KP Website

Streamlit-basiertes Content-Management-System für die 5KP-Website.  
Die App nutzt AWS Bedrock (Claude) um HTML-Änderungen per KI zu generieren und deployt sie über die GitHub API, was automatisch die CI/CD-Pipeline triggert.

## Architektur

```
Streamlit Cloud  →  GitHub API (Commit)  →  GitHub Actions  →  AWS S3 + CloudFront
                         ↑
                  AWS Bedrock (Claude)
```

## Setup (einmalig)

### 1. Google OAuth konfigurieren

1. [Google Cloud Console](https://console.cloud.google.com/) → APIs & Dienste → Anmeldedaten → OAuth 2.0-Client erstellen
2. Authorized redirect URI: `https://<deine-app>.streamlit.app/oauth2callback`
3. Client-ID und Client-Secret notieren

### 2. GitHub Fine-Grained Token erstellen

1. GitHub → Settings → Developer settings → Fine-grained personal access tokens
2. Repository access: nur `Kompaniewebseite20`
3. Permissions: **Contents → Read and Write**

### 3. AWS Bedrock Zugriff

Erstelle einen dedizierten IAM-User mit folgender Mindestpolicy:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": "arn:aws:bedrock:eu-central-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
  }]
}
```

### 4. Streamlit Cloud Secrets einrichten

1. App auf [share.streamlit.io](https://share.streamlit.io) deployen (Repo: `Kompaniewebseite20`, Branch: `main`, Main file: `cms/app.py`)
2. App-Settings → Secrets → Inhalt aus `.streamlit/secrets.toml.example` einfügen und ausfüllen

## Lokal starten

```powershell
cd cms
pip install -r requirements.txt
# Kopiere .streamlit/secrets.toml.example nach .streamlit/secrets.toml und fülle es aus
streamlit run app.py
```

## Workflow für Redakteure

1. Mit Google anmelden (nur erlaubte E-Mail-Adressen haben Zugriff)
2. Seite auswählen (intern oder öffentlich)
3. Änderungswunsch in natürlicher Sprache beschreiben
4. Optional: PDF als Inhaltsquelle (z.B. neue Termine) oder als Download-Asset hochladen
5. „Änderungen generieren" klicken → Claude erstellt das neue HTML
6. Live-Vorschau prüfen
7. „Veröffentlichen" → GitHub Commit → CI/CD Pipeline → Live in ~2 Minuten

## Sicherheitshinweise

- Die `secrets.toml` enthält sensible Zugangsdaten – **niemals in Git committen**
- Die Datei `cms/.streamlit/secrets.toml` ist in `.gitignore` eingetragen
- Verwende immer Fine-Grained Tokens mit minimalen Berechtigungen
- Die App prüft die E-Mail-Adresse nach dem Google-Login gegen die `allowed_emails`-Liste
