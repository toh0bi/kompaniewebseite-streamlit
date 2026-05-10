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
from botocore.config import Config
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
# Authentication
# ---------------------------------------------------------------------------
# Simple password auth matching the pattern used in the other app,
# avoiding the OAuth CSRF/Authlib issues entirely.

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    st.title("5KP Website CMS - Anmeldung erforderlich")
    st.info("Bitte melde dich an, um fortzufahren.")
    
    with st.form("login_form"):
        password = st.text_input("CMS Passwort", type="password")
        submit = st.form_submit_button("Einloggen", type="primary")
        
        if submit:
            # Get password from secrets or use a default one for deployment ease
            correct_password = st.secrets.get("cms_password", "moorhasen_cms_2026")
            
            if password == correct_password:
                st.session_state.logged_in = True
                st.rerun()
            else:
                st.error("Falsches Passwort.")
    st.stop()

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------
_GITHUB_OWNER  = st.secrets["github_owner"]
_GITHUB_REPO   = st.secrets["github_repo"]
_GITHUB_TOKEN  = st.secrets["github_token"]
# Force master branch ignoring any accidently set secrets
_GITHUB_BRANCH = "master"
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


def commit_text_file(path: str, content: str, sha: str | None, message: str) -> str:
    """Update or create a text file via the GitHub Contents API. Returns to commit SHA."""
    url = _gh_url(path)
    print(f"\n[API REQ] method=PUT url={url}")
    print(f"[API REQ] path={path}, sha={sha}, branch={_GITHUB_BRANCH}")
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": _GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, headers=_GH_HEADERS, json=payload, timeout=15)
    if not resp.ok:
        print(f"[API ERROR] Status: {resp.status_code}")
        print(f"[API ERROR] Body: {resp.text}")
        # Wir werfen unsere eigene Exception, damit die Fehlermeldung direkt im Streamlit UI angezeigt wird
        raise Exception(f"GitHub API Error {resp.status_code} für {path}: {resp.text} \n(URL: {url}, Branch: {_GITHUB_BRANCH}, SHA: {sha})")
    resp.raise_for_status()
    # Ensure to return the new commit sha which we can poll for later
    return resp.json()["commit"]["sha"]


def get_failed_job_log(run_id: int) -> str:
    """Fetch the log tail for a failed GitHub action job to show to the agent/user."""
    jobs_url = f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}/actions/runs/{run_id}/jobs"
    resp = requests.get(jobs_url, headers=_GH_HEADERS, timeout=15)
    if not resp.ok:
        return "Konnte Error-Logs nicht laden (fehlende Berechtigungen?)."
        
    jobs = resp.json().get("jobs", [])
    failed_job = next((j for j in jobs if j.get("conclusion") == "failure"), None)
    if not failed_job:
        return "Pipeline fehlgeschlagen, aber kein fehlerhafter Job gefunden."
        
    html_url = failed_job.get("html_url", "")
    job_id = failed_job["id"]
    
    # Try downloading logs (this redirects to a text file download)
    try:
        log_resp = requests.get(f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}/actions/jobs/{job_id}/logs", headers=_GH_HEADERS, timeout=15)
        if log_resp.ok:
            log_text = log_resp.text
            # extract bottom ~30 lines to catch the error
            lines = log_text.splitlines()[-30:]
            error_snippet = "\n".join(lines)
            return f"**Fehler im Quality Gate (Job '{failed_job['name']}'):**\n```\n{error_snippet}\n```\n\n[Hier klicken für komplette GitHub Logs]({html_url})"
    except Exception:
        pass
        
    return f"Pipeline fehlgeschlagen. [Hier die Logs auf GitHub ansehen]({html_url})"


def poll_github_action(commit_sha: str, status_text) -> tuple[bool, str]:
    """Polls GitHub Actions API for the workflow run attached to the commit."""
    run_id = None
    
    status_text.markdown("⏳ **Suche nach gestarteter CI/CD Pipeline...** (Dies kann einen Moment dauern)")
    for attempt in range(15):
        # Wir filtern direkt nach aktuellen push events im Branch
        resp = requests.get(
            f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}/actions/runs?branch={_GITHUB_BRANCH}&event=push",
            headers=_GH_HEADERS, timeout=15
        )
        print(f"[POLL ATTEMPT {attempt}] Suche nach push-pipelines für Commit: {commit_sha}")
        if resp.ok and resp.json().get("total_count", 0) > 0:
            runs = resp.json()["workflow_runs"]
            
            # Wir checken lieber die letzten 5 Pipelines als nur die allererste,
            # da gerade bei parallelen Action-Triggern oder irre schnell beendeten Workflows
            # die Position 0 schwanken kann.
            found_latest = False
            for r in runs[:5]:
                print(f"  -> Run {r['id']} | Status: {r['status']} | SHA: {r.get('head_sha')}")
                # Entweder der SHA-Commit matcht, oder wir klinken uns in die gerade laufende Queue ein.
                if r.get("head_sha") == commit_sha or (not found_latest and r.get("status") in ["queued", "in_progress", "pending", "requested"]):
                    run_id = r["id"]
                    print(f"[POLL SUCCESS] Match für Pipeline gefunden: {run_id}")
                    break
                found_latest = True
                    
        if run_id:
            break
        time.sleep(3)
        
    if not run_id:
        return False, "Konnte den GitHub Actions Lauf nicht finden. Er wurde möglicherweise nicht gestartet."
        
    # Polling des Lauf-Status
    while True:
        resp = requests.get(
            f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}/actions/runs/{run_id}",
            headers=_GH_HEADERS, timeout=15
        )
        if not resp.ok:
            return False, "Fehler beim Abrufen des Pipeline-Status."
            
        run = resp.json()
        status = run["status"]
        conclusion = run["conclusion"]
        
        if status == "completed":
            if conclusion == "success":
                return True, ""
            else:
                return False, get_failed_job_log(run_id)
        else:
            status_text.markdown(f"🔄 **Pipeline läuft noch...** (Status: `{status}` / URL: [GitHub Actions]({run['html_url']}))")
            time.sleep(5)


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
def _bedrock_client():
    """Create a Bedrock runtime client.
    Uses Streamlit secrets when available, falls back to the default credential chain
    (env vars, ~/.aws/credentials, IAM role) for local development.
    """
    region = st.secrets.get("AWS_DEFAULT_REGION", "eu-central-1")
    # Increase read_timeout exponentially since large HTML generations with Sonnet take time
    my_config = Config(
        read_timeout=900,
        connect_timeout=900,
        retries={"max_attempts": 3}
    )
    
    if "AWS_ACCESS_KEY_ID" in st.secrets:
        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
            config=my_config
        )
    return boto3.client("bedrock-runtime", region_name=region, config=my_config)


def load_system_prompt() -> str:
    try:
        with open("CLAUDE.md", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "Du bist der Webmaster der Seite. Passe das HTML an."


def call_agentic_bedrock(user_prompt: str, extra_context: str = "") -> str:
    """Agentic loop using the Bedrock Converse API with Function Calling."""
    bedrock = _bedrock_client()
    model_id = st.secrets.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")

    system_prompt = [{"text": load_system_prompt()}]
    
    # Bedrock tool definitions
    tool_config = {
        "tools": [
            {
                "toolSpec": {
                    "name": "read_github_file",
                    "description": "Liest den aktuellen Inhalt einer Datei aus dem GitHub Repository (z.B. 'website/internal/index.html').",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string", "description": "Der relative Pfad zur Datei"}
                            },
                            "required": ["file_path"]
                        }
                    }
                }
            },
            {
                "toolSpec": {
                    "name": "replace_string_in_file",
                    "description": "BEVORZUGTES TOOL FÜR KLEINE ANPASSUNGEN: Ersetzt einen exakten Textbereich in einer Datei. Der 'old_string' muss exakt so in der Datei gefunden werden inkl. Leerzeichen und Umbrüchen.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string", "description": "Pfad, der bearbeitet wird (z.B. 'website/internal/index.html')"},
                                "old_string": {"type": "string", "description": "Der exakte alte Textabschnitt, der entfernt werden soll (idealerweise ein etwas größerer Block zur Eindeutigkeit)."},
                                "new_string": {"type": "string", "description": "Der neue Text, der genau an diese Stelle gesetzt wird."}
                            },
                            "required": ["file_path", "old_string", "new_string"]
                        }
                    }
                }
            },
            {
                "toolSpec": {
                    "name": "stage_file_edit",
                    "description": "Speichert die vorgenommenen Änderungen an einer Datei ab. WICHTIG: Du musst das komplette, aktualisierte HTML übergeben.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string", "description": "Pfad, der bearbeitet wird"},
                                "new_html_content": {"type": "string", "description": "Das komplette aktualisierte HTML."}
                            },
                            "required": ["file_path", "new_html_content"]
                        }
                    }
                }
            }
        ]
    }

    user_message = f"Nutzer-Anforderung: {user_prompt}\n{extra_context}"
    messages = [{"role": "user", "content": [{"text": user_message}]}]
    
    print(f"\n[AGENT START] Neues Agenten-Ziel: {user_prompt[:50]}...")
    
    # ---------------------------------------------------------------------
    # The Agent Loop with visible UI Status
    # ---------------------------------------------------------------------
    with st.status("🤖 Agent übernimmt Kontrolle...", expanded=True) as status:
        status.write("Initialisiere Bedrock-Verbindung...")
        
        loop_counter = 1
        while True:
            status.write(f"🔄 **Iteration {loop_counter}:** Claude überlegt (Dies kann bei großen Änderungen 1-2 Minuten dauern)...")
            print(f"[AGENT LOOP {loop_counter}] Warte auf Antwort von Bedrock...")
            
            start_time = time.time()
            response = bedrock.converse(
                modelId=model_id,
                messages=messages,
                system=system_prompt,
                toolConfig=tool_config
            )
            duration = time.time() - start_time
            
            output_message = response["output"]["message"]
            messages.append(output_message)
            
            stop_reason = response["stopReason"]
            print(f"[AGENT LOOP {loop_counter}] Antwort erhalten nach {duration:.1f}s. Stop reason: {stop_reason}")
            
            if stop_reason == "tool_use":
                tool_results = []
                for block in output_message.get("content", []):
                    if "toolUse" in block:
                        tool = block["toolUse"]
                        tool_name = tool["name"]
                        tool_input = tool["input"]
                        tool_id = tool["toolUseId"]
                        
                        print(f"  -> [TOOL] {tool_name} aufgerufen mit Args: {str(tool_input)[:100]}")
                        
                        if tool_name == "read_github_file":
                            path = tool_input["file_path"]
                            status.write(f"📖 Claude liest sich in die Datei ein: `{path}`")
                            try:
                                content, sha = get_github_file(path)
                                result_text = json.dumps({"content": content, "sha": sha})
                            except Exception as e:
                                result_text = json.dumps({"error": str(e)})
                                
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool_id,
                                    "content": [{"text": result_text}]
                                }
                            })
                            
                        elif tool_name == "replace_string_in_file":
                            path = tool_input["file_path"]
                            old_str = tool_input["old_string"]
                            new_str = tool_input["new_string"]
                            status.write(f"✂️ **Claude ersetzt Text in `{path}` (Token gespart!)**")
                            
                            try:
                                if path in st.session_state.staged_edits:
                                    current_content = st.session_state.staged_edits[path]["content"]
                                    sha = st.session_state.staged_edits[path]["sha"]
                                else:
                                    current_content, sha = get_github_file(path)
                                
                                if old_str not in current_content:
                                    result_text = json.dumps({"error": f"Fehler: Der exakte Code '{old_str[:30]}...' wurde nicht gefunden. Nutze einen fehlerfreien Block."})
                                else:
                                    # Ersetze exakt das Vorkommen
                                    new_content = current_content.replace(old_str, new_str, 1)
                                    st.session_state.staged_edits[path] = {
                                        "content": new_content,
                                        "sha": sha
                                    }
                                    result_text = json.dumps({"status": "Erfolgreich ersetzt und vorgemerkt."})
                            except Exception as e:
                                result_text = json.dumps({"error": str(e)})
                                
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool_id,
                                    "content": [{"text": result_text}]
                                }
                            })
                            
                        elif tool_name == "stage_file_edit":
                            path = tool_input["file_path"]
                            status.write(f"✍️ **Claude hat HTML-Code für `{path}` generiert und vorgemerkt!**")
                            new_content = tool_input["new_html_content"]
                            
                            try:
                                try:
                                    _, sha = get_github_file(path)
                                except:
                                    sha = None
                                    
                                st.session_state.staged_edits[path] = {
                                    "content": new_content,
                                    "sha": sha
                                }
                                result_text = json.dumps({"status": "Erfolgreich im System vorgemerkt."})
                            except Exception as e:
                                result_text = json.dumps({"error": str(e)})
                                
                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool_id,
                                    "content": [{"text": result_text}]
                                }
                            })
                            
                messages.append({"role": "user", "content": tool_results})
                loop_counter += 1
                
            else:
                # End of conversation loop
                status.update(label="✅ Agent hat seine Arbeit beendet!", state="complete", expanded=False)
                print("[AGENT ENDE] Agent ist fertig.")
                text_blocks = [b["text"] for b in output_message.get("content", []) if "text" in b]
                return "\n".join(text_blocks)


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------
if "staged_edits" not in st.session_state:
    st.session_state.staged_edits = {} # file_path -> {"content": html, "sha": string}
if "agent_feedback" not in st.session_state:
    st.session_state.agent_feedback = None
if "last_prompt" not in st.session_state:
    st.session_state.last_prompt = ""
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("5KP Website CMS")

if st.button("Abmelden", key="logout"):
    st.session_state.logged_in = False
    st.rerun()

st.divider()

# --- Dropdown selector entfernt, da Claude die Dateien über Tools selbst sucht ---

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
if st.button("Änderungen generieren (Agent starten)", type="primary"):
    if not prompt.strip():
        st.warning("⚠️ Bitte trage zuerst oben im Textfeld einen Änderungswunsch ein!")
    else:
        # Clear previous edits
        st.session_state.staged_edits = {}
        st.session_state.agent_feedback = None
    
    try:
        extra_context = ""
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
                    st.warning("Nur PDFs können als Inhaltsquelle extrahiert werden.")
                uploaded_file.seek(0)

            elif "Als Asset" in (file_mode or ""):
                safe_name = uploaded_file.name.replace(" ", "_").lower()
                asset_path = f"website/internal/pdfs/{safe_name}"
                uploaded_file.seek(0)
                commit_binary_file(
                    asset_path,
                    uploaded_file.read(),
                    f"cms: Upload Asset '{safe_name}'",
                )
                extra_context = (
                    f"\n\nDie Datei wurde bereits hochgeladen unter: '{asset_path}'. "
                    f"Du kannst nun einen Link darauf einbauen."
                )
                st.success(f"Asset '{safe_name}' wurde hochgeladen.")

        st.session_state.last_prompt = prompt
        # Starte den Conversation Loop
        feedback = call_agentic_bedrock(prompt, extra_context)
        st.session_state.agent_feedback = feedback

    except Exception as exc:
        st.error(f"Fehler im Agent Loop: {exc}")

# ---------------------------------------------------------------------------
# Preview & Publish
# ---------------------------------------------------------------------------
if st.session_state.staged_edits or st.session_state.agent_feedback:
    st.divider()
    
    if st.session_state.agent_feedback:
        st.subheader("Agent Feedback")
        st.info(st.session_state.agent_feedback)
        
    if not st.session_state.staged_edits:
        st.warning("Der Agent hat geantwortet, aber keine Datei-Änderungen vorgemerkt.")
    else:
        st.subheader("Bestehende Dateivormerkungen")
        
        # Tabs for multiple staged files
        tabs = st.tabs(list(st.session_state.staged_edits.keys()))
        for idx, (path, data) in enumerate(st.session_state.staged_edits.items()):
            with tabs[idx]:
                st.components.v1.html(data["content"], height=800, scrolling=True)

        col_publish, col_discard = st.columns([1, 4])
        with col_discard:
            if st.button("Verwerfen", key="discard"):
                st.session_state.staged_edits = {}
                st.session_state.agent_feedback = None
                st.rerun()

        with col_publish:
            if st.button("Sieht gut aus, veröffentlichen!", type="primary", key="publish"):
                with st.spinner("Commits werden erstellt..."):
                    last_commit_sha = None
                    print("\n[PUBLISH ACTION] Starte Veröffentlichungsprozess")
                    print(f"[PUBLISH OVERVIEW] Anzahl vorbereiteter Dateien: {len(st.session_state.staged_edits)}")
                    for p, d in st.session_state.staged_edits.items():
                        print(f"  -> {p}: Content={'JA' if d['content'] else 'NEIN'} | SHA={d['sha']}")
                        
                    try:
                        for path, data in st.session_state.staged_edits.items():
                            print(f"[PUBLISH ITERATION] Rufe commit_text_file auf für: {path}")
                            last_commit_sha = commit_text_file(
                                path,
                                data["content"],
                                data["sha"],
                                f"cms: {st.session_state.last_prompt[:72]}"
                            )
                    except Exception as exc:
                        st.error(f"Fehler beim Veröffentlichen: {exc}")
                        st.stop()

                st.success("Änderungen wurden in GitHub committet! Die CI/CD-Pipeline startet automatisch.")

                if last_commit_sha:
                    status_text = st.empty()
                    success, error_msg = poll_github_action(last_commit_sha, status_text)
                    
                    if success:
                        status_text.markdown("✅ **Deployment abgeschlossen!** Alle Quality Gates passiert, die Seite ist jetzt live.")
                    else:
                        status_text.markdown("❌ **Deployment fehlgeschlagen!**")
                        st.error(error_msg)
                        st.info("Kopiere die Fehlermeldung und gib sie dem Agenten, damit er das HTML reparieren kann!")
                        
                    # Ein Streamlit UI Fehler behoben: Wenn wir hier einfach den Session-State löschen, 
                    # würde er es bei fehlerhaften Deployments trotzdem nicht sofort im Terminal refreshen.
                    if success:
                        # Clear und Reload nach erfolgreichem Deployment!
                        time.sleep(2)  # kurz für das ✅ Feedback
                        st.session_state.staged_edits = {}
                        st.session_state.agent_feedback = None
                        st.rerun()
                else:
                    st.session_state.staged_edits = {}
                    st.session_state.agent_feedback = None
