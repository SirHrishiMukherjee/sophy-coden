import os
import subprocess
import threading
import time
from queue import Queue
from collections import defaultdict

from flask import Flask, render_template_string, request, redirect, url_for
import psycopg2

# ---------------------------------------------------------
# DB CONNECTION & INIT
# ---------------------------------------------------------

def db_conn():
    """
    Create a new PostgreSQL connection using DATABASE_URL.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL environment variable not set.")
    return psycopg2.connect(db_url, sslmode="require")


def init_db():
    """
    Ensure feedback tables exist (does NOT touch runnable_blocks).
    runnable_blocks is assumed to be created/managed by the main engine.
    """
    conn = db_conn()
    cur = conn.cursor()

    # Table for accumulate likes/dislikes per program (program = block_index as text)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS program_reactions (
            program TEXT PRIMARY KEY,
            likes   INTEGER NOT NULL DEFAULT 0,
            dislikes INTEGER NOT NULL DEFAULT 0
        );
    """)

    # Table for comments
    cur.execute("""
        CREATE TABLE IF NOT EXISTS program_comments (
            id SERIAL PRIMARY KEY,
            program TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------
# Helpers for feedback & comments
# ---------------------------------------------------------

def get_programs():
    """
    Return a sorted list of program identifiers from runnable_blocks.
    Each program is a distinct block_index converted to string.
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT block_index
        FROM runnable_blocks
        ORDER BY block_index ASC;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [str(r[0]) for r in rows]


def get_reactions(program: str):
    """
    Retrieve likes & dislikes for a program from DB.
    Returns (likes, dislikes).
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT likes, dislikes
        FROM program_reactions
        WHERE program = %s;
    """, (program,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return 0, 0
    return row[0], row[1]


def increment_like(program: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO program_reactions (program, likes, dislikes)
        VALUES (%s, 1, 0)
        ON CONFLICT (program)
        DO UPDATE SET likes = program_reactions.likes + 1;
    """, (program,))
    conn.commit()
    cur.close()
    conn.close()


def increment_dislike(program: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO program_reactions (program, likes, dislikes)
        VALUES (%s, 0, 1)
        ON CONFLICT (program)
        DO UPDATE SET dislikes = program_reactions.dislikes + 1;
    """, (program,))
    conn.commit()
    cur.close()
    conn.close()


def add_program_comment(program: str, text: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO program_comments (program, comment)
        VALUES (%s, %s);
    """, (program, text))
    conn.commit()
    cur.close()
    conn.close()


def get_program_comments(program: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT comment
        FROM program_comments
        WHERE program = %s
        ORDER BY created_at ASC;
    """, (program,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]


def get_program_code(program: str):
    """
    Fetch the latest cleaned_content for this program (block_index) from DB.
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT cleaned_content
        FROM runnable_blocks
        WHERE block_index = %s
        ORDER BY created_at DESC
        LIMIT 1;
    """, (program,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return row[0]


# ---------------------------------------------------------
# Flask application
# ---------------------------------------------------------

running_processes = {}
output_queues = {}
last_output = defaultdict(str)  # program -> accumulated text

app = Flask(__name__)


def safe_program_name(name):
    """Ensure the program name is a simple basename without path traversal."""
    if not name:
        return None
    name = os.path.basename(name)
    # strip .py if present; DB programs are usually numeric strings
    if name.endswith(".py"):
        name = name[:-3]
    if "/" in name or "\\" in name:
        return None
    return name


@app.route("/", methods=["GET"])
def index():
    programs = get_programs()
    selected = request.args.get("program")
    selected = safe_program_name(selected)

    if not programs:
        selected_program = None
    else:
        if selected not in programs:
            selected_program = programs[0] if programs else None
        else:
            selected_program = selected

    output_text = last_output.get(selected_program, "") if selected_program else ""
    selected_comments = get_program_comments(selected_program) if selected_program else []
    like_count, dislike_count = (0, 0)
    if selected_program:
        like_count, dislike_count = get_reactions(selected_program)

    return render_template_string(
        TEMPLATE,
        programs=programs,
        selected_program=selected_program,
        output_text=output_text,
        comments=selected_comments,
        like_count=like_count,
        dislike_count=dislike_count,
    )


@app.route("/run", methods=["POST"])
def run_program():
    program = safe_program_name(request.form.get("program"))
    programs = get_programs()
    if not program or program not in programs:
        return redirect(url_for("index", program=program))

    # Kill previous instance if running
    if program in running_processes:
        try:
            running_processes[program].kill()
        except Exception:
            pass

    # Fresh queue
    q = Queue()
    output_queues[program] = q
    last_output[program] = ""  # reset displayed output

    # Fetch code from Postgres
    code_text = get_program_code(program)
    if code_text is None:
        # nothing to run
        return redirect(url_for("index", program=program))

    # Environment for UTF-8
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # Write to a temporary file for execution
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8")
    tmp.write(code_text)
    tmp_path = tmp.name
    tmp.close()

    # Start child process
    p = subprocess.Popen(
        ["python", tmp_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        env=env
    )
    running_processes[program] = p

    def pump(pipe, q_, prog):
        for line in iter(pipe.readline, b""):
            try:
                decoded = line.decode("utf-8", errors="replace")
                q_.put(decoded)
                # Accumulate last_output so the user can see a snapshot on reload
                last_output[prog] += decoded
            except Exception:
                pass
        pipe.close()

    threading.Thread(target=pump, args=(p.stdout, q, program), daemon=True).start()
    threading.Thread(target=pump, args=(p.stderr, q, program), daemon=True).start()

    return redirect(url_for("index", program=program))


@app.route("/stream/<program>")
def stream_program(program):
    program = safe_program_name(program)
    q = output_queues.get(program)

    def event_stream():
        yield "retry: 200\n\n"  # reconnect speed
        while True:
            if q is None:
                time.sleep(0.1)
                continue
            while not q.empty():
                line = q.get()
                yield f"data: {line}\n\n"
            time.sleep(0.1)

    return app.response_class(event_stream(), mimetype="text/event-stream")


@app.route("/like", methods=["POST"])
def like_program():
    program = safe_program_name(request.form.get("program"))
    programs = get_programs()
    if program and program in programs:
        increment_like(program)
    return redirect(url_for("index", program=program))


@app.route("/dislike", methods=["POST"])
def dislike_program():
    program = safe_program_name(request.form.get("program"))
    programs = get_programs()
    if program and program in programs:
        increment_dislike(program)
    return redirect(url_for("index", program=program))


@app.route("/comment", methods=["POST"])
def add_comment():
    program = safe_program_name(request.form.get("program"))
    text = (request.form.get("comment") or "").strip()
    programs = get_programs()
    if program and program in programs and text:
        add_program_comment(program, text)
    return redirect(url_for("index", program=program))


# ---------------------------------------------------------
# Template (unchanged structure, new DB messages)
# ---------------------------------------------------------

TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Sophy Programs</title>
    <style>
        * {
            box-sizing: border-box;
        }
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background-color: #0f0f0f;
            color: #f1f1f1;
        }
        a {
            color: inherit;
            text-decoration: none;
        }
        .main-grid {
            display: flex;
            height: 100vh;
            width: 100vw;
        }
        .column {
            width: 50%;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        /* LEFT COLUMN */
        .output-box {
            flex: 0 0 50%;
            background: #181818;
            border-radius: 8px;
            padding: 12px;
            border: 1px solid #303030;
            overflow: auto;
            white-space: pre-wrap;
            font-family: "Consolas", "Courier New", monospace;
            font-size: 14px;
        }
        .output-title {
            font-size: 16px;
            margin-bottom: 8px;
            font-weight: bold;
        }
        .program-meta {
            background: #181818;
            border-radius: 8px;
            padding: 12px;
            border: 1px solid #303030;
        }
        .program-title-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 8px;
        }
        .program-title {
            font-size: 18px;
            font-weight: bold;
        }
        .program-actions {
            display: flex;
            gap: 8px;
            align-items: center;
        }
        .btn {
            border: none;
            border-radius: 999px;
            padding: 6px 12px;
            font-size: 13px;
            cursor: pointer;
            background: #272727;
            color: #f1f1f1;
        }
        .btn-like {
            background: #2563eb;
        }
        .btn-dislike {
            background: #b91c1c;
        }
        .btn-share {
            background: #059669;
        }
        .btn-run {
            background: #facc15;
            color: #111827;
            font-weight: bold;
        }
        .count-badge {
            font-size: 12px;
            opacity: 0.8;
        }

        .comments-box {
            flex: 1;
            background: #181818;
            border-radius: 8px;
            padding: 12px;
            border: 1px solid #303030;
            display: flex;
            flex-direction: column;
        }
        .comments-title {
            font-size: 16px;
            font-weight: bold;
            margin-bottom: 8px;
        }
        .comments-list {
            flex: 1;
            overflow-y: auto;
            margin-bottom: 8px;
        }
        .comment-item {
            padding: 6px 0;
            border-bottom: 1px solid #303030;
            font-size: 13px;
        }
        .comment-form textarea {
            width: 100%;
            min-height: 50px;
            resize: vertical;
            border-radius: 6px;
            border: 1px solid #404040;
            padding: 6px;
            background: #111111;
            color: #f1f1f1;
            font-family: inherit;
            font-size: 13px;
        }
        .comment-form button {
            margin-top: 6px;
        }

        /* RIGHT COLUMN */
        .box {
            background: #181818;
            border-radius: 8px;
            padding: 12px;
            border: 1px solid #303030;
        }
        .my-programs-box {
            flex: 0 0 50%;
            display: flex;
            flex-direction: column;
        }
        .box-title {
            font-size: 16px;
            font-weight: bold;
            margin-bottom: 8px;
        }
        .programs-list {
            overflow-y: auto;
        }
        .program-item {
            background: #202020;
            border-radius: 8px;
            padding: 8px;
            margin-bottom: 8px;
            display: flex;
            flex-direction: column;
            cursor: pointer;
            border: 1px solid transparent;
            transition: border-color 0.15s ease, background-color 0.15s ease;
        }
        .program-item:hover {
            border-color: #facc15;
            background-color: #262626;
        }
        .program-item.selected {
            border-color: #f97316;
            background-color: #27272a;
        }
        .program-thumbnail {
            background: #0f172a;
            border-radius: 4px;
            height: 80px;
            margin-bottom: 6px;
        }
        .program-name {
            font-size: 14px;
            font-weight: bold;
        }

        .categories-box {
            flex: 1;
            margin-top: 12px;
            display: flex;
            flex-direction: column;
        }
        .categories-inner {
            white-space: nowrap;
            overflow-x: auto;
            padding: 8px 4px;
        }
        .category-pill {
            display: inline-block;
            padding: 8px 16px;
            margin-right: 8px;
            border-radius: 999px;
            background: #272727;
            font-size: 13px;
            border: 1px solid #404040;
            cursor: pointer;
        }
        .category-pill:hover {
            background: #3f3f3f;
        }

        .empty-state {
            font-size: 14px;
            opacity: 0.8;
        }
        #sophy-console {
            background:#000;
            color:#00ffea;
            font-family: "Consolas", "JetBrains Mono", monospace;
            font-size:14px;
            padding:12px;
            border-radius:8px;
            overflow-y:auto;
            overflow-x:auto;
            white-space:pre-wrap;
            height:100%;
            box-shadow: 0 0 12px rgba(0,255,255,0.2) inset;
        }
        .caret::after {
            content: "█";
            animation: blink 1s infinite;
        }
        @keyframes blink {
            0% { opacity: 1; }
            50% { opacity: 0; }
            100% { opacity: 1; }
        }
    </style>
</head>
<body>
<div class="main-grid">
    <!-- LEFT COLUMN -->
    <div class="column">
        <div class="output-box" id="sophy-console">
            <div class="output-title">
                Output
                {% if selected_program %}
                    – Block {{ selected_program }}
                {% else %}
                    – No program selected
                {% endif %}
            </div>
            {% if output_text %}
                {{ output_text }}
            {% else %}
                {% if selected_program %}
                    Select "Run" to execute this program and view its output.
                {% else %}
                    No runnable blocks found in Postgres (runnable_blocks).
                {% endif %}
            {% endif %}
        </div>

        <div class="program-meta">
            <div class="program-title-row">
                <div class="program-title">
                    {% if selected_program %}
                        Block {{ selected_program }}
                    {% else %}
                        No Program Selected
                    {% endif %}
                </div>
                <div class="program-actions">
                    {% if selected_program %}
                        <form method="post" action="{{ url_for('run_program') }}" style="display:inline;">
                            <input type="hidden" name="program" value="{{ selected_program }}">
                            <button class="btn btn-run" type="submit">Run</button>
                        </form>
                        <form method="post" action="{{ url_for('like_program') }}" style="display:inline;">
                            <input type="hidden" name="program" value="{{ selected_program }}">
                            <button class="btn btn-like" type="submit">👍 Like</button>
                        </form>
                        <span class="count-badge">{{ like_count }}</span>

                        <form method="post" action="{{ url_for('dislike_program') }}" style="display:inline;">
                            <input type="hidden" name="program" value="{{ selected_program }}">
                            <button class="btn btn-dislike" type="submit">👎 Dislike</button>
                        </form>
                        <span class="count-badge">{{ dislike_count }}</span>

                        <button class="btn btn-share" type="button"
                                onclick="navigator.clipboard.writeText(window.location.href);">
                            ⤴ Share
                        </button>
                    {% endif %}
                </div>
            </div>
            <div style="font-size: 12px; opacity: 0.8;">
                {% if selected_program %}
                    DB-backed runnable program interface for Sophy and users.
                {% else %}
                    Blocks come from the Postgres runnable_blocks table.
                {% endif %}
            </div>
        </div>

        <div class="comments-box">
            <div class="comments-title">Comments</div>
            <div class="comments-list">
                {% if comments and selected_program %}
                    {% for c in comments %}
                        <div class="comment-item">{{ c }}</div>
                    {% endfor %}
                {% elif selected_program %}
                    <div class="empty-state">No comments yet. Be the first to comment.</div>
                {% else %}
                    <div class="empty-state">Select a program to view and add comments.</div>
                {% endif %}
            </div>
            {% if selected_program %}
                <form class="comment-form" method="post" action="{{ url_for('add_comment') }}">
                    <input type="hidden" name="program" value="{{ selected_program }}">
                    <textarea name="comment" placeholder="Add a public comment..."></textarea>
                    <button class="btn" type="submit">Comment</button>
                </form>
            {% endif %}
        </div>
    </div>

    <!-- RIGHT COLUMN -->
    <div class="column">
        <div class="box my-programs-box">
            <div class="box-title">My Programs (Blocks)</div>
            <div class="programs-list">
                {% if programs %}
                    {% for p in programs %}
                        <a href="{{ url_for('index', program=p) }}">
                            <div class="program-item {% if selected_program == p %}selected{% endif %}">
                                <div class="program-thumbnail"></div>
                                <div class="program-name">Block {{ p }}</div>
                            </div>
                        </a>
                    {% endfor %}
                {% else %}
                    <div class="empty-state">
                        No blocks found in <code>runnable_blocks</code> table.<br>
                        Once the engine writes blocks, they will appear here.
                    </div>
                {% endif %}
            </div>
        </div>

        <div class="box categories-box">
            <div class="box-title">Categories</div>
            <div class="categories-inner">
                <!-- Horizontally scrollable categories -->
                <span class="category-pill">GR</span>
                <span class="category-pill">QM</span>
                <span class="category-pill">HR &gt;</span>
            </div>
        </div>
    </div>
</div>
<script>
function startStream(program) {
    const consoleBox = document.getElementById("sophy-console");
    consoleBox.innerHTML = "";

    const es = new EventSource(`/stream/${program}`);

    es.onmessage = function(event) {
        consoleBox.textContent += event.data + "\n";
        consoleBox.scrollTop = consoleBox.scrollHeight;
    };

    es.onerror = function() {
        console.log("Stream ended.");
        es.close();
    };
}

// auto-start live feed
{% if selected_program %}
startStream("{{ selected_program }}");
{% endif %}
</script>
</body>
</html>
"""


# ---------------------------------------------------------
# Entry point
# ---------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
