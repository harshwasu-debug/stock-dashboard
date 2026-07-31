"""
assistant - a phone-friendly capture tool: notes (typed or spoken) in,
to-do items out.

The pieces, and why they are split this way:

  models.py        the two things this app stores: a Note and a Task
  backends.py      *where* bytes live (a local folder, or a private GitHub repo)
  store.py         *what* those bytes mean (tasks.json + notes/)
  extract.py       turning a note into structured tasks (Gemini, with a
                   rule-based fallback so the app still works without a key)
  calendar_sync.py pushing tasks that have deadlines into Google Calendar
  config.py        reading settings out of Streamlit secrets / env vars
  app.py           the Streamlit screen you actually touch on your phone

Everything except app.py is plain Python with no Streamlit import, so it can
be tested (and reused) without spinning up a web server.
"""
