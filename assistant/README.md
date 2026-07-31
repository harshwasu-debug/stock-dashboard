# Assistant

Notes in — typed or spoken — to-do list out, with deadlines going to Google
Calendar. Built to be used on an Android phone as a home-screen app.

It shares this repo with the stock dashboard but is a completely separate
Streamlit app. **Your notes are never stored in this repo** — this one is
public. They go to a private repo you create.

---

## How it works

```
Android home screen  →  Streamlit app (password gated)
   ├── typed note ─────────────┐
   └── voice note (mic) ───────┤
                               ▼
                    Gemini reads it → structured tasks
                               ▼
                     review screen  ← nothing is saved before you press Save
                               ▼
     ┌─────────────────────────┴──────────────────────┐
  private repo                                  Google Calendar
  tasks.json + notes/*.md                       (only tasks with deadlines)
```

Two design rules worth knowing:

- **The model never writes a file.** It returns fields; deterministic Python
  writes the JSON. There is no path where a bad model response can eat your
  task list.
- **The model never invents a deadline.** If you didn't say when, the task has
  no date. A to-do list that quietly makes up dates is worse than none.

## Running it locally

```bash
pip install -r requirements.txt
streamlit run assistant/app.py
```

With nothing configured it still works: it stores tasks in a local
`assistant_data/` folder and falls back to keyword parsing instead of Gemini.
Voice notes are the one thing that genuinely needs a key.

```bash
python -m pytest assistant/tests -q      # 84 tests, no network needed
```

---

## Setup

Four steps. The app's **Setup** tab has a test button for each one, so you can
do them in any order and check as you go.

### 1. A private repo for your data

Create a new **private** repo — call it whatever you like, e.g. `harsh-notes`.
Leave it empty; the app creates `tasks.json` and `notes/` on first save.

Then make a fine-grained personal access token:
GitHub → Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token.

- Repository access: **Only select repositories** → your notes repo
- Permissions: **Contents → Read and write**

That's the only permission it needs. The Setup tab warns you loudly if the
repo you pointed at turns out to be public.

### 2. A Gemini API key

From [aistudio.google.com](https://aistudio.google.com/apikey). Gemini reads
audio natively, so this one key covers both transcription and task extraction
— there's no separate speech-to-text service to set up or pay for.

### 3. Google Calendar

The app authenticates as a **service account**, and you share your calendar
with it — the same way you'd share it with a person. This avoids storing OAuth
refresh tokens in a web app.

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project
2. APIs & Services → Library → enable **Google Calendar API**
3. APIs & Services → Credentials → Create credentials → **Service account**
4. Open the service account → Keys → Add key → **JSON** → download it
5. Open [Google Calendar](https://calendar.google.com/) → your calendar →
   Settings and sharing → **Share with specific people** → add the service
   account's email (it looks like `something@project.iam.gserviceaccount.com`)
   → permission: **Make changes to events**

Step 5 is the one people miss. Without it the API returns "calendar not found"
even though everything else is correct.

### 4. Secrets

Locally, put these in `.streamlit/secrets.toml`. On Streamlit Cloud, paste
them into the app's Settings → Secrets box. **Never commit them.**

```toml
app_password   = "something-only-you-know"
gemini_api_key = "..."

notes_repo   = "your-username/harsh-notes"
notes_branch = "main"
github_token = "github_pat_..."

google_calendar_id     = "you@gmail.com"
google_service_account = '''
{
  "type": "service_account",
  "project_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "assistant@....iam.gserviceaccount.com",
  ...
}
'''
```

Every setting also reads from an environment variable of the same name in
capitals (`GEMINI_API_KEY`, `NOTES_REPO`, …), which is handy for local runs.

---

## Deploying

On [share.streamlit.io](https://share.streamlit.io), create a **second** app
from this repo:

- Main file path: `assistant/app.py`
- Add your secrets under Advanced settings

Then on your phone: open the URL in Chrome → menu → **Add to Home screen**.

Two things to expect:

- **Cold starts.** A free Streamlit Cloud app sleeps after inactivity, so the
  first tap of the day can take 20–40 seconds to wake. Every tap after that is
  instant.
- **Mic permission.** Chrome asks once, the first time you tap the recorder.
  It needs HTTPS, which Streamlit Cloud provides.

---

## How deadlines reach you

| What you said | What lands in your calendar |
|---|---|
| "by Friday" | All-day event on Friday, popup at 09:00 Thursday |
| "Friday at 4pm" | 30-minute event at 16:00, popup at 15:30 |
| no deadline mentioned | nothing — it just sits in the list |

All-day reminders in Google are counted backwards from midnight, so "9am on
the day itself" isn't expressible for an all-day event; the day before is the
closest useful thing, and it's what Google's own UI does.

Ticking a task off deletes its event, so the calendar shows what's still
outstanding rather than a history of everything you ever wrote down.

---

## Layout

| File | What it does |
|---|---|
| `models.py` | `Task` and `Note`, and the rules for reading them back safely |
| `backends.py` | *Where* bytes live: a local folder, or a private GitHub repo |
| `store.py` | *What* those bytes mean: `tasks.json` + `notes/` |
| `extract.py` | Note → tasks, via Gemini, with a keyword fallback |
| `calendar_sync.py` | Tasks with deadlines → Google Calendar events |
| `config.py` | Settings, from Streamlit secrets or env vars |
| `app.py` | The Streamlit screen |

Everything except `app.py` is plain Python with no Streamlit import, so it's
testable — and reusable from a cron job later — without a web server.

## Notes on the data

`tasks.json` holds every task, open and done. Notes are markdown with a small
JSON front-matter block, one file per note under `notes/YYYY-MM/`, so they read
fine on github.com or in any editor.

Writes are optimistic: each one carries the version it expects to replace, and
retries if the file moved underneath it. Saving from your phone and your laptop
at the same moment produces two commits, not one lost note.

## Known limits

- This is a **separate list** from the Chief of Staff `Plan.md`. Nothing
  captured here shows up in its Aging Check. If this fills up with kitchen-ops
  work rather than personal items, that's the signal to wire the two together.
- No offline mode. The app needs a connection to save.
- No recurring tasks yet.
