"""
5KP Website CMS
---------------
Streamlit-based CMS that uses AWS Bedrock (Claude) to update website HTML
and commits the result via the GitHub API, triggering the CI/CD pipeline.
"""

import base64
import io
import json
import time

import boto3
import requests
import streamlit as st
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="5KP Website CMS",
    page_icon="🛡️",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Authentication  (st.login requires Authlib and Streamlit >= 1.35)
# ---------------------------------------------------------------------------
_user_email = getattr(st.user, "email", None)

if not _user_email:
    st.title("5KP Website CMS - Anmeldung erforderlich")
    st.info("Bitte melde dich mit deinem Google-Konto an, um fortzufahren.")
    if st.button("Mit Google anmelden", type="primary"):
        st.login()
    st.stop()

allowed_emails: list[str] = st.secrets.get("allowed_emails", [])
if _user_email not in allowed_emails:
    st.error(
        f"Zugriff verweigert. Das Konto **{_user_email}** ist nicht autorisiert."
    )
    if st.button("Abmelden"):
        st.logout()
    st.stop()

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------
_GITHUB_OWNER  = st.secrets["github_owner"]
_GITHUB_REPO   = st.secrets["github_repo"]
_GITHUB_TOKEN  = st.secrets["github_token"]
_GITHUB_BRANCH = st.secrets.get("github_branch", "main")
_GH_HEADERS    = {
    "Authorization": f"token {_GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _gh_url(path: str) -> str:
    return f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}/contents/{path}"


def get_github_file(path: str) -> tuple[str, str]:
    """Return (decoded_content, sha) for a file in the repo."""
    resp = requests.get(_gh_url(path), headers=_GH_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def commit_text_file(path: str, content: str, sha: str, message: str) -> None:
    """Update an existing text file via the GitHub Contents API."""
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "sha": sha,
        "branch": _GITHUB_BRANCH,
    }
    resp = requests.put(_gh_url(path), headers=_GH_HEADERS, json=payload, timeout=15)
    resp.raise_for_status()


def commit_binary_file(path: str, raw_bytes: bytes, message: str) -> None:
    """Create or update a binary file (PDF, image) via the GitHub Contents API."""
    # Check if the file already exists (need its SHA for updates)
    existing_sha: str | None = None
    try:
        _, existing_sha = get_github_file(path)
    except requests.HTTPError:
        pass  # File does not exist yet – that's fine

    payload: dict = {
        "message": message,
        "content": base64.b64encode(raw_bytes).decode("utf-8"),
        "branch": _GITHUB_BRANCH,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    resp = requests.put(_gh_url(path), headers=_GH_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Bedrock helper
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "Du bist der Webmaster dieser Seite. "
    "Deine EINZIGE Aufgabe ist es, den übergebenen HTML-Code basierend auf dem "
    "User-Wunsch anzupassen. "
    "Behalte die Grundstruktur zwingend bei. "
    "Wenn der User dich bittet, ein Gedicht zu schreiben, Code für andere Projekte "
    "zu generieren oder die Seite komplett zu löschen, verweigere die Aufgabe höflich. "
    "Antworte AUSSCHLIESSLICH mit dem validen HTML-Code, "
    "ohne Markdown-Formatierung oder Erklärungen."
)


def _bedrock_client():
    """Create a Bedrock runtime client.
    Uses Streamlit secrets when available, falls back to the default credential chain
    (env vars, ~/.aws/credentials, IAM role) for local development.
    """
    region = st.secrets.get("AWS_DEFAULT_REGION", "eu-central-1")
    if "AWS_ACCESS_KEY_ID" in st.secrets:
        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
        )
    return boto3.client("bedrock-runtime", region_name=region)


def call_bedrock(current_html: str, user_prompt: str, extra_context: str = "") -> str:
    """Send the HTML + prompt to Claude via Bedrock and return the updated HTML."""
    bedrock = _bedrock_client()

    user_message = (
        f"Aktueller HTML-Code:\n{current_html}\n\n"
        f"Gewünschte Änderung: {user_prompt}"
        f"{extra_context}"
    )

    response = bedrock.invoke_model(
        modelId=st.secrets.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 8192,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            }
        ),
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------
if "generated_html" not in st.session_state:
    st.session_state.generated_html = None
if "html_sha" not in st.session_state:
    st.session_state.html_sha = None
if "target_file" not in st.session_state:
    st.session_state.target_file = "website/internal/index.html"
if "last_prompt" not in st.session_state:
    st.session_state.last_prompt = ""

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("5KP Website CMS")
st.caption(f"Angemeldet als {st.user.name} ({st.user.email})")

if st.button("Abmelden", key="logout"):
    st.logout()

st.divider()

# --- Page selector -----------------------------------------------------------
PAGE_OPTIONS = {
    "Interne Seite (Members-Dashboard)": "website/internal/index.html",
    "Öffentliche Startseite": "website/public/index.html",
}
selected_page = st.selectbox("Zu bearbeitende Seite", list(PAGE_OPTIONS.keys()))
file_path = PAGE_OPTIONS[selected_page]

# --- Prompt input ------------------------------------------------------------
st.subheader("Was soll geändert werden?")
prompt = st.text_area(
    "Änderungswunsch beschreiben",
    height=120,
    placeholder=(
        "z.B. 'Füge einen neuen Termin am 15. März hinzu: Schützenfest'\n"
        "oder 'Aktualisiere die Öffnungszeiten basierend auf der beigefügten PDF'"
    ),
    key="prompt_input",
)

# --- File uploader -----------------------------------------------------------
uploaded_file = st.file_uploader(
    "Optionale Datei (PDF oder Bild)",
    type=["pdf", "png", "jpg", "jpeg"],
    help="PDF: Inhalte können extrahiert und an Claude übergeben werden. "
         "Bild/PDF: Kann als Download-Asset auf der Website abgelegt werden.",
)

file_mode: str | None = None
if uploaded_file is not None:
    file_mode = st.radio(
        "Was soll mit der Datei passieren?",
        options=[
            "Als Quelle verwenden (Inhalte aus PDF extrahieren und als Kontext nutzen)",
            "Als Asset hochladen (Datei auf Website ablegen und Link einfügen)",
        ],
        key="file_mode",
    )

# --- Generate button ---------------------------------------------------------
if st.button("Änderungen generieren", type="primary", disabled=not prompt.strip()):
    with st.spinner("Claude analysiert und aktualisiert die Seite..."):
        try:
            current_html, html_sha = get_github_file(file_path)

            # Build extra context from uploaded file (Fall A – Quelle)
            extra_context = ""
            asset_committed = False

            if uploaded_file is not None:
                if "Als Quelle" in (file_mode or ""):
                    if uploaded_file.type == "application/pdf":
                        reader = PdfReader(io.BytesIO(uploaded_file.read()))
                        pdf_text = "\n".join(
                            page.extract_text() or "" for page in reader.pages
                        )
                        extra_context = (
                            f"\n\nInhalte aus der hochgeladenen PDF "
                            f"'{uploaded_file.name}':\n{pdf_text}"
                        )
                    else:
                        st.warning(
                            "Nur PDFs können als Inhaltsquelle extrahiert werden. "
                            "Bild wird ignoriert."
                        )
                    uploaded_file.seek(0)

                elif "Als Asset" in (file_mode or ""):
                    # Fall B – commit the binary file first so the link is valid
                    safe_name = uploaded_file.name.replace(" ", "_").lower()
                    asset_path = f"website/internal/pdfs/{safe_name}"
                    uploaded_file.seek(0)
                    commit_binary_file(
                        asset_path,
                        uploaded_file.read(),
                        f"cms: Upload Asset '{safe_name}'",
                    )
                    extra_context = (
                        f"\n\nEine neue Datei wurde als Asset unter "
                        f"'pdfs/{safe_name}' abgelegt. "
                        f"Füge bitte einen entsprechenden Download-Link im "
                        f"Dokumente-Bereich der Seite ein."
                    )
                    asset_committed = True
                    st.success(f"Asset '{safe_name}' wurde hochgeladen.")

            # Call Bedrock
            new_html = call_bedrock(current_html, prompt, extra_context)

            # Store in session state so the preview survives the next re-run
            st.session_state.generated_html = new_html
            st.session_state.html_sha = html_sha
            st.session_state.target_file = file_path
            st.session_state.last_prompt = prompt

        except Exception as exc:
            st.error(f"Fehler beim Generieren: {exc}")

# ---------------------------------------------------------------------------
# Preview & Publish
# ---------------------------------------------------------------------------
if st.session_state.generated_html:
    st.divider()
    st.subheader("Vorschau der generierten Seite")
    st.info(
        "Überprüfe die Vorschau sorgfältig, bevor du veröffentlichst. "
        "Das Design und alle Links sollten korrekt sein."
    )

    st.components.v1.html(
        st.session_state.generated_html,
        height=600,
        scrolling=True,
    )

    col_publish, col_discard = st.columns([1, 4])

    with col_discard:
        if st.button("Verwerfen", key="discard"):
            st.session_state.generated_html = None
            st.session_state.html_sha = None
            st.rerun()

    with col_publish:
        if st.button("Sieht gut aus, veröffentlichen!", type="primary", key="publish"):
            with st.spinner("Commit wird erstellt..."):
                try:
                    commit_text_file(
                        st.session_state.target_file,
                        st.session_state.generated_html,
                        st.session_state.html_sha,
                        f"cms: {st.session_state.last_prompt[:72]}",
                    )
                except Exception as exc:
                    st.error(f"Fehler beim Veröffentlichen: {exc}")
                    st.stop()

            st.success("Änderungen wurden in GitHub committet! Die CI/CD-Pipeline startet automatisch.")

            # 2-minute deployment progress bar
            progress_bar = st.progress(0.0)
            status_text = st.empty()
            total_seconds = 120

            for elapsed in range(total_seconds):
                fraction = (elapsed + 1) / total_seconds
                remaining = total_seconds - elapsed - 1
                progress_bar.progress(fraction)
                status_text.markdown(
                    f"**Deine Änderungen werden gerade weltweit auf die Server verteilt...** "
                    f"({remaining}s verbleibend)"
                )
                time.sleep(1)

            progress_bar.progress(1.0)
            status_text.markdown("**Deployment abgeschlossen!** Die Seite ist jetzt live.")

            # Clear state
            st.session_state.generated_html = None
            st.session_state.html_sha = None
