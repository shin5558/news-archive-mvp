# -*- coding: utf-8 -*-
import os
import sqlite3
import json
import time
import secrets
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

from flask import (
    Flask, request, redirect, url_for, jsonify,
    render_template_string, abort, session, g
)

from dotenv import load_dotenv
load_dotenv()  # .envファイルを自動読み込み

import httpx  # OpenAI呼び出しを安定させるためにhttpxを採用

# 追加インポート
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import hashlib

# -----------------------------
# 環境変数
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("BIND_PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "data", "app.db"))
INDEX_HTML_PATH = os.getenv("INDEX_HTML_PATH", os.path.join(BASE_DIR, "index.html"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # 例: https://yourdomain/p/

# -----------------------------
# Flask アプリ
# -----------------------------
app = Flask(__name__)

# セッション用シークレットキー（未設定なら暫定生成）
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)

# -----------------------------
# サニタイズ（任意のローカル実装にフォールバック）
# -----------------------------
def _fallback_sanitize(text: str) -> str:
    """最低限の整形。過度に削らない。"""
    if text is None:
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)  # 制御文字
    return text.strip()

def _mask_pii(text: str) -> str:
    """公開ページ用の簡易マスク（メール・電話・住所らしきもの）"""
    if not text:
        return text
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email masked]", text)
    text = re.sub(r"\b(\+?\d{1,3}[- ]?)?\d{2,4}[- ]?\d{2,4}[- ]?\d{3,4}\b", "[tel masked]", text)
    text = re.sub(r"(東京都|大阪府|福岡県|北海道|京都府|神奈川県|愛知県|埼玉県|千葉県|兵庫県|福岡市|横浜市)[^\n、，。 ]{0,20}", "[address masked]", text)
    return text

try:
    from sanitize import sanitize_text as _ext_sanitize_text  # type: ignore
    def sanitize_text(text: str) -> str:
        return _ext_sanitize_text(text) or _fallback_sanitize(text)
except Exception:
    sanitize_text = _fallback_sanitize

# -----------------------------
# テンプレート読込
# -----------------------------
def load_index_html() -> str:
    try:
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return f"""<!doctype html><meta charset="utf-8"><title>議論アーカイブ MVP</title>
        <h1>index.html が見つかりませんでした</h1>
        <p>探したパス: {INDEX_HTML_PATH}</p>
        <p>プロジェクト直下に <code>index.html</code> を置くか、環境変数 <code>INDEX_HTML_PATH</code> を設定してください。</p>
        """
INDEX_HTML = load_index_html()

# -----------------------------
# DB ヘルパ
# -----------------------------
def _ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def get_db():
    _ensure_parent_dir(DB_PATH)  # ← フォルダがなければ作る
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # 既存スキーマ（スレ・メッセージ）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        is_public INTEGER DEFAULT 0,
        public_token TEXT,
        created_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id INTEGER NOT NULL,
        role TEXT NOT NULL,          -- 'user' | 'assistant' | 'system'
        content TEXT NOT NULL,
        created_at TEXT,
        FOREIGN KEY(thread_id) REFERENCES threads(id)
    )""")

    # 追加: users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      display_name TEXT,
      avatar_url TEXT,
      role TEXT DEFAULT 'user',
      created_at TEXT
    )""")

    # 追加: posts
    cur.execute("""
    CREATE TABLE IF NOT EXISTS posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
      user_id INTEGER REFERENCES users(id),
      parent_post_id INTEGER REFERENCES posts(id),
      content TEXT NOT NULL,
      is_hidden INTEGER DEFAULT 0,
      created_at TEXT,
      updated_at TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_thread_id ON posts(thread_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_thread_time ON posts(thread_id, created_at)")

    # 追加: ai_summaries
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ai_summaries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
      model TEXT,
      mode TEXT,
      content TEXT NOT NULL,
      hash_key TEXT NOT NULL,
      created_at TEXT
    )""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_summaries_thread_hash ON ai_summaries(thread_id, hash_key)")

    # 追加: reports
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      target_type TEXT,
      target_id INTEGER,
      reported_by INTEGER REFERENCES users(id),
      reason TEXT,
      status TEXT DEFAULT 'open',
      created_at TEXT,
      resolved_by INTEGER REFERENCES users(id),
      resolved_at TEXT
    )""")

    # 既存threadsに列が無ければ足す（tryで安全に）
    try:
        cur.execute("ALTER TABLE threads ADD COLUMN created_by INTEGER REFERENCES users(id)")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE threads ADD COLUMN status TEXT DEFAULT 'open'")
    except sqlite3.OperationalError:
        pass

    # ★ AIユーザーを用意（存在すればスキップ）
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute("""
      INSERT OR IGNORE INTO users (email, password_hash, display_name, role, created_at)
      VALUES ('ai@local', '', 'AI', 'system', ?)
    """, (now_iso,))

    conn.commit()
    conn.close()

init_done = False

# Ensure DB schema is initialized once per process
@app.before_request
def _on_boot():
    global init_done
    if not init_done:
        init_db()
        init_done = True

# -----------------------------
# current_user ロード
# -----------------------------
@app.before_request
def load_current_user():
    uid = session.get("user_id")
    if not uid:
        g.current_user = None
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, email, display_name, role FROM users WHERE id=?", (uid,))
    row = cur.fetchone()
    conn.close()
    g.current_user = dict(row) if row else None

# -----------------------------
# 認可デコレータ
# -----------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return wrapper

# -----------------------------
# 画面: サインアップ/ログイン
# -----------------------------
@app.route("/signup", methods=["GET"])
def signup_page():
    return render_template_string("""
    <!doctype html><meta charset="utf-8"><title>Sign up</title>
    <h2>Sign up</h2>
    <form method="post" action="/signup">
      <div>Email: <input name="email" type="email" required></div>
      <div>表示名: <input name="display_name" type="text"></div>
      <div>Password: <input name="password" type="password" required></div>
      <button type="submit">Create</button>
    </form>
    """)

@app.route("/signup", methods=["POST"])
def signup():
    email = request.form.get("email","").strip().lower()
    display_name = request.form.get("display_name","").strip() or None
    password = request.form.get("password","")
    if not email or not password:
        return "email/password は必須です", 400

    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (email, password_hash, display_name, created_at)
            VALUES (?, ?, ?, ?)
        """, (email, generate_password_hash(password), display_name, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return "そのメールは既に登録されています", 400
    conn.close()
    session["user_id"] = user_id
    return redirect(url_for("home"))

@app.route("/login", methods=["GET"])
def login_page():
    return render_template_string("""
    <!doctype html><meta charset="utf-8"><title>Login</title>
    <h2>Login</h2>
    <form method="post" action="/login">
      <div>Email: <input name="email" type="email" required></div>
      <div>Password: <input name="password" type="password" required></div>
      <button type="submit">Login</button>
    </form>
    """)

@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email","").strip().lower()
    password = request.form.get("password","")
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT id, password_hash FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        return "メールまたはパスワードが違います", 400
    session["user_id"] = row["id"]
    return redirect(url_for("home"))

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))

# -----------------------------
# スレッド作成/詳細/返信/通報
# -----------------------------
@app.route("/threads", methods=["POST"])
@login_required
def create_thread_route():
    title = sanitize_text(request.form.get("title","").strip())
    body  = sanitize_text(request.form.get("body","").strip())
    if not title or not body:
        return "title と body は必須です", 400

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("INSERT INTO threads (title, created_by, is_public, status, created_at) VALUES (?, ?, 1, 'open', ?)",
                (title, g.current_user["id"], datetime.now(timezone.utc).isoformat()))
    thread_id = cur.lastrowid
    cur.execute("""INSERT INTO posts (thread_id, user_id, parent_post_id, content, created_at)
                   VALUES (?, ?, NULL, ?, ?)""",
                (thread_id, g.current_user["id"], body, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    # トップページに統一
    return redirect(url_for("home", thread_id=thread_id))

@app.route("/threads/<int:thread_id>", methods=["GET"])
def view_thread(thread_id: int):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT t.*, u.display_name AS author_name FROM threads t LEFT JOIN users u ON u.id=t.created_by WHERE t.id=?", (thread_id,))
    th = cur.fetchone()
    if not th:
        conn.close()
        return "thread not found", 404

    cur.execute("SELECT p.*, u.display_name AS user_name FROM posts p LEFT JOIN users u ON u.id=p.user_id WHERE p.thread_id=? AND p.is_hidden=0 ORDER BY p.id ASC", (thread_id,))
    posts = cur.fetchall()

    cur.execute("SELECT content, created_at FROM ai_summaries WHERE thread_id=? ORDER BY id DESC LIMIT 1", (thread_id,))
    summary = cur.fetchone()
    conn.close()

    return render_template_string("""
    <!doctype html><meta charset="utf-8"><title>{{ th.title }}</title>
    <h2>{{ th.title }}</h2>
    <div>by {{ th.author_name or 'unknown' }} / {{ th.created_at }}</div>
    <hr>
    {% if summary %}
    <h3>AIの整理（最新）</h3>
    <pre style="white-space:pre-wrap">{{ summary.content }}</pre>
    {% endif %}

    <h3>投稿</h3>
    {% for p in posts %}
      <div style="border:1px solid #ddd;margin:8px 0;padding:8px;border-radius:8px;">
        <div style="color:#666;font-size:12px;">{{ p.user_name or '匿名' }} / {{ p.created_at }}</div>
        <div style="white-space:pre-wrap">{{ p.content }}</div>
      </div>
    {% endfor %}

    {% if g.current_user %}
    <hr>
    <form method="post" action="/threads/{{ th.id }}/posts">
      <textarea name="content" required style="width:100%;min-height:120px;"></textarea>
      <div><button type="submit">返信する</button></div>
    </form>

    <form method="post" action="/threads/{{ th.id }}/summarize" style="margin-top:8px;">
      <button type="submit">AIで整理する</button>
    </form>
    {% else %}
      <div><a href="/login">ログイン</a>すると返信できます</div>
    {% endif %}
    """, th=th, posts=posts, summary=summary)

@app.route("/threads/<int:thread_id>/posts", methods=["POST"])
@login_required
def add_post(thread_id: int):
    content = sanitize_text(request.form.get("content","").strip())
    parent  = request.form.get("parent_post_id","")
    parent_id = int(parent) if (parent and parent.isdigit()) else None
    if not content:
        return "content は必須です", 400

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT id, status FROM threads WHERE id=?", (thread_id,))
    th = cur.fetchone()
    if not th or th["status"] == "locked":
        conn.close()
        return "投稿できません（存在しない/ロック中）", 400

    cur.execute("""INSERT INTO posts (thread_id, user_id, parent_post_id, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (thread_id, g.current_user["id"], parent_id, content, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    # トップページに統一
    return redirect(url_for("home", thread_id=thread_id))

@app.route("/posts/<int:post_id>/report", methods=["POST"])
@login_required
def report_post(post_id: int):
    reason = sanitize_text(request.form.get("reason","").strip() or "not specified")
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""INSERT INTO reports (target_type, target_id, reported_by, reason, status, created_at)
                   VALUES ('post', ?, ?, ?, 'open', ?)""",
                (post_id, g.current_user["id"], reason, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return "reported", 200

# -----------------------------
# 公開中スレッドの読み取り専用ビュー
# -----------------------------

# --- 公開ビュー（読み取り専用） ---
@app.route("/p/<token>", methods=["GET"])
def public_view(token: str):
    conn = get_db()
    cur = conn.cursor()

    # 公開中スレッドを取得
    cur.execute("SELECT id, title, created_at FROM threads WHERE public_token=? AND is_public=1", (token,))
    th = cur.fetchone()
    if not th:
        conn.close()
        return "not found", 404

    thread_id = th["id"]

    # 公開ビューでは messages（AI＋ユーザーの分析用発言）を時系列で見せる
    cur.execute("SELECT role, content, created_at FROM messages WHERE thread_id=? ORDER BY id ASC", (thread_id,))
    msgs = cur.fetchall()
    conn.close()

    # 既存の INDEX_HTML を流用して表示だけ
    return render_template_string(
        INDEX_HTML,
        article="",
        result=None,
        threads=list_threads(),       # 右のスレッド一覧はそのまま
        active_thread_id=thread_id,   # ヘッダに #ID 表示
        history=[{"role": m["role"], "content": m["content"], "created_at": m["created_at"]} for m in msgs],
        share_url=request.url,        # 共有URLは表示
        is_public_view=True           # ← 追加：公開ビュー（操作系は隠す）
    )

# -----------------------------
# OpenAI 呼び出し
# -----------------------------
def build_conversation_prompt(article: str, comment: str) -> str:
     return (
         "あなたはニュースや社会の話題について気さくに会話する友人です。"
         "会話調で、6〜8文（250〜400字）に収めてください。"
         "断定は避け、わからない点はわからないと述べ、最後に短い一言の問いかけで締めてください。\n"
         "----\n"
         f"【記事/要約】\n{article}\n"
         "----\n"
         f"【ユーザーのコメント】\n{comment}\n"
     )

def _style_hint_for_thread(thread_id: int, latest_post_id: int) -> str:
    """ワンパターン回避のためのスタイルヒントをスレッド/直近投稿に応じてローテーション。"""
    styles = [
        "質問多めで柔らかく。断定は避ける。",
        "結論→根拠→一言質問の順で簡潔に。",
        "要点を3つだけ拾い、比喩なしで平易に。",
        "相手の感情に共感→事実→提案→短い問い。",
        "リスクとメリットを対にして述べる。語尾は落ち着いた口調。",
    ]
    idx = (thread_id + latest_post_id) % len(styles)
    return styles[idx]

def _build_thread_prompt_for_summary(article_text: str, recent_posts_text: str, style_hint: str, last_ai_text: str = "") -> str:
    """スレッド要約ではなく“会話風レスポンス”。直近投稿の温度感を取り入れ、最後は一言問いかけで締める。"""
    avoid_block = f"\n【過去のAI文（直近）※表現の焼き直し禁止】\n{last_ai_text}\n" if last_ai_text else ""
    return (
        "あなたは議論の進行役ではなく、場をあたためるフレンドリーな参加者です。"
        "以下の『記事』と『最近の投稿』を踏まえ、同じ話を繰り返さず、新しい角度で短く会話してください。"
        "会話調で6〜8文（250〜400字）、最後は一言の問いかけで締めること。"
        "具体例を1つ入れてもよいが、誇張や断定は避ける。"
        "既に使われた言い回しの再利用は避け、言い換えを必須とします。\n"
        f"【スタイル指示】{style_hint}\n"
        "----\n【記事】\n" + (article_text or "") +
        "\n----\n【最近の投稿（新しい順・抜粋）】\n" + (recent_posts_text or "") +
        avoid_block +
        "\n----\n出力は本文のみ。前置き・見出しは不要。\n"
    )

def call_openai_with_prompt(
    prompt: str,
    *,
    temperature: float = 0.5,
    presence_penalty: float = 0.2,
    frequency_penalty: float = 0.3,
    max_tokens: int = 600,
    timeout_sec: float = 30.0,
    retries: int = 3
) -> Tuple[bool, str]:
    
    if not OPENAI_API_KEY:
        return False, "⚠️ OPENAI_API_KEY を設定してください。"

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful, concise, reliable Japanese assistant."},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "frequency_penalty": frequency_penalty,
        "presence_penalty": presence_penalty,
    }

    last_err = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                )
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:500]}"
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                        time.sleep(1.2 * attempt)
                        continue
                    return False, f"⚠️ APIエラー: {last_err}"
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                if text and text.strip():
                    return True, text.strip()
                payload["messages"].append({"role": "user", "content": "前の回答が空でした。必ずテキストで返してください。"})
                if attempt < retries:
                    time.sleep(0.8 * attempt)
                    continue
                return False, "⚠️ 応答が空でした。"
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(1.0 * attempt)
                continue
            return False, f"⚠️ 通信エラー: {last_err}"
    return False, f"⚠️ 失敗しました: {last_err}"

def _get_ai_user_id() -> Optional[int]:
    """AIユーザー（ai@local）のIDを取得"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email='ai@local' LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return int(row["id"]) if row else None

@app.route("/threads/<int:thread_id>/summarize", methods=["POST"])
@login_required
def summarize_thread(thread_id: int):
    N_RECENT = 10  # 直近N件の投稿を要約に使う

    conn = get_db()
    cur  = conn.cursor()

    # 記事＝最初のpost
    cur.execute("SELECT id, content FROM posts WHERE thread_id=? ORDER BY id ASC LIMIT 1", (thread_id,))
    first = cur.fetchone()
    if not first:
        conn.close()
        return "記事がありません", 400
    article_text = first["content"]

    # 直近N件（新しい順）
    cur.execute("SELECT id, content FROM posts WHERE thread_id=? ORDER BY id DESC LIMIT ?", (thread_id, N_RECENT))
    recent = cur.fetchall()
    if not recent:
        conn.close()
        return "投稿がありません", 400

    latest_post_id = recent[0]["id"]
    recent_text = "\n\n".join([f"- {r['content']}" for r in recent])

    # 直近のAI文（表現の焼き直しを避けるためプロンプトへ渡す）
    cur.execute("SELECT content FROM ai_summaries WHERE thread_id=? ORDER BY id DESC LIMIT 1", (thread_id,))
    last_ai_row = cur.fetchone()
    last_ai_text = last_ai_row["content"] if last_ai_row else ""

    # スタイルヒント（スレッド×最新投稿でローテーション）
    style_hint = _style_hint_for_thread(thread_id, latest_post_id)

    # キャッシュキー
    mode  = "conversation"
    model = OPENAI_MODEL
    # 投稿テキストの指先だけでも差分を拾えるよう recent_text のハッシュを含める
    recent_digest = hashlib.sha256(recent_text.encode("utf-8")).hexdigest()[:16]
    raw_for_hash = f"{article_text}\n#last={latest_post_id}\n#recent={recent_digest}\n#style={style_hint}\n#mode={mode}\n#model={model}"
    hash_key = hashlib.sha256(raw_for_hash.encode("utf-8")).hexdigest()

    # 既存キャッシュ確認
    cur.execute("SELECT content FROM ai_summaries WHERE thread_id=? AND hash_key=? LIMIT 1", (thread_id, hash_key))
    hit = cur.fetchone()
    ai_uid = _get_ai_user_id()

    if hit:
        # ★ キャッシュの内容も投稿として積む（2回目以降もタイムラインに追加）
        cur.execute("""INSERT INTO posts (thread_id, user_id, parent_post_id, content, created_at)
                       VALUES (?, ?, NULL, ?, ?)""",
                    (thread_id, ai_uid, f"【AIの整理】\n{hit['content']}", datetime.now(timezone.utc).isoformat()))
        conn.commit()
    conn.close()
    # トップページに統一
    return redirect(url_for("home", thread_id=thread_id))

    # 生成（← ここはキャッシュ未ヒット時だけ通る）
    prompt = _build_thread_prompt_for_summary(article_text, recent_text, style_hint, last_ai_text)
    # 会話寄せなので温度やペナルティを少し上げてバリエーションを出す
    ok, text = call_openai_with_prompt(
        prompt,
        temperature=0.7,
        presence_penalty=0.5,
        frequency_penalty=0.2,
        max_tokens=700
    )
    if not ok or not text.strip():
        conn.close()
        return f"AI生成に失敗しました: {text}", 500
    ai_text = text.strip()

    # 保存（キャッシュ）＋ 投稿
    cur.execute("""INSERT INTO ai_summaries (thread_id, model, mode, content, hash_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (thread_id, model, mode, ai_text, hash_key, datetime.now(timezone.utc).isoformat()))
    cur.execute("""INSERT INTO posts (thread_id, user_id, parent_post_id, content, created_at)
                   VALUES (?, ?, NULL, ?, ?)""",
                (thread_id, ai_uid, f"【AIの整理】\n{ai_text}", datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    # トップページに統一
    return redirect(url_for("home", thread_id=thread_id))

# -----------------------------
# スレッド/メッセージ操作（既存UI用）
# -----------------------------
def create_thread(title: Optional[str]) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO threads (title, created_at) VALUES (?, ?)",
        (title or "無題スレッド", datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid

def get_thread(thread_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
    row = cur.fetchone()
    conn.close()
    return row

def list_threads():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM threads ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_message(thread_id: int, role: str, content: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (thread_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (thread_id, role, content, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

def get_history(thread_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content, created_at FROM messages WHERE thread_id = ? ORDER BY id ASC",
        (thread_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]

def set_publish(thread_id: int, make_public: bool) -> Tuple[bool, Optional[str]]:
    conn = get_db()
    cur = conn.cursor()
    if make_public:
        cur.execute("SELECT public_token FROM threads WHERE id = ?", (thread_id,))
        row = cur.fetchone()
        token = row["public_token"] if row and row["public_token"] else secrets.token_urlsafe(18)
        cur.execute("UPDATE threads SET is_public=1, public_token=? WHERE id=?", (token, thread_id))
        conn.commit()
        conn.close()
        share_url = f"{PUBLIC_BASE_URL.rstrip('/')}/p/{token}" if PUBLIC_BASE_URL else url_for("public_view", token=token, _external=True)
        return True, share_url
    else:
        cur.execute("UPDATE threads SET is_public=0 WHERE id=?", (thread_id,))
        conn.commit()
        conn.close()
        return False, None

def get_public_thread_by_token(token: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, title, created_at FROM threads WHERE public_token=? AND is_public=1", (token,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None, None
    thread_id = row["id"]
    cur.execute("SELECT role, content, created_at FROM messages WHERE thread_id=? ORDER BY id ASC", (thread_id,))
    msgs = cur.fetchall()
    conn.close()
    return row, msgs

def get_posts_for_feed(thread_id: int):
    """タイムライン用に posts を取得（表示専用・必要最低限）。"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT content, created_at FROM posts WHERE thread_id = ? AND is_hidden = 0 ORDER BY id ASC",
        (thread_id,)
    )
    rows = cur.fetchall()
    conn.close()
    # history と同じ形に揃える（roleは user で表示）
    return [{"role": "user", "content": r["content"], "created_at": r["created_at"]} for r in rows]



# -----------------------------
# ルーティング
# -----------------------------
@app.route("/", methods=["GET"])
def home():
    thread_id = request.args.get("thread_id", "").strip()
    active_thread_id = None
    history = []  # messages + posts をマージして流す
    share_url = None

    if thread_id.isdigit():
        active_thread_id = int(thread_id)
        th = get_thread(active_thread_id)
        if th:
            if th["is_public"]:
                token = th["public_token"]
                share_url = f"{PUBLIC_BASE_URL.rstrip('/')}/p/{token}" if PUBLIC_BASE_URL else url_for("public_view", token=token, _external=True)
            # messages と posts をマージして、created_at 昇順に並べ替え
            msgs  = get_history(active_thread_id)
            posts = get_posts_for_feed(active_thread_id)
            merged = msgs + posts
            # ISO8601(UTC)文字列なので文字列ソートでも概ね安全だが、念のためキーを明示
            merged.sort(key=lambda x: x.get("created_at", ""))
            history = merged
        else:
            active_thread_id = None

    return render_template_string(
        INDEX_HTML,
        article="",
        result=None,
        threads=list_threads(),
        active_thread_id=active_thread_id,
        history=history,
        share_url=share_url,
        is_public_view=False   # ← 追加：通常ビュー
    )

@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    raw_article = request.form.get("article", "")
    raw_comment = request.form.get("comment", "")
    raw_thread_id = request.form.get("thread_id", "").strip()
    thread_title = request.form.get("thread_title", "").strip() or None

    article = sanitize_text(raw_article)
    comment = sanitize_text(raw_comment)

    if raw_thread_id and raw_thread_id.isdigit():
        thread_id = int(raw_thread_id)
        if not get_thread(thread_id):
            thread_id = create_thread(thread_title)
    else:
        thread_id = create_thread(thread_title)

    user_blob = f"【記事】\n{article}\n\n【コメント】\n{comment}"
    add_message(thread_id, "user", user_blob)

    ok, text = call_openai_with_prompt(build_conversation_prompt(article, comment))

    role = "assistant" if ok else "system"
    add_message(thread_id, role, text)

    th = get_thread(thread_id)
    share_url = None
    if th and th["is_public"] and th["public_token"]:
        token = th["public_token"]
        share_url = f"{PUBLIC_BASE_URL.rstrip('/')}/p/{token}" if PUBLIC_BASE_URL else url_for("public_view", token=token, _external=True)

    # ✅ PRG（Post/Redirect/Get）で二重送信を防止
    return redirect(url_for("home", thread_id=thread_id))


@app.route("/threads/<int:thread_id>/publish", methods=["POST"])
@login_required
def publish(thread_id: int):
    cur_val = (request.form.get("make_public", "false").lower() == "true")
    new_val = not cur_val
    is_public, share_url = set_publish(thread_id, new_val)
    return jsonify({"ok": True, "is_public": is_public, "share_url": share_url})

@app.route("/threads", methods=["GET"])
def list_threads_route():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
      SELECT t.id, t.title, t.created_at, t.is_public, t.status,
             u.display_name AS author_name
      FROM threads t
      LEFT JOIN users u ON u.id = t.created_by
      WHERE t.status != 'hidden'
      ORDER BY t.id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    return render_template_string("""
    <!doctype html><meta charset="utf-8"><title>Threads</title>
    <h2 style="margin:12px 0">スレッド一覧</h2>

    <div style="margin-bottom:12px;">
      {% if g.current_user %}
        <form method="post" action="/logout" style="display:inline">
          <button>ログアウト</button>
        </form>
      {% else %}
        <a href="/login">ログイン</a> ／ <a href="/signup">新規登録</a>
      {% endif %}
    </div>

    <ul style="list-style:none;padding-left:0">
      {% for r in rows %}
        <li style="padding:8px 0;border-top:1px dashed #e5e5e5">
          <a href="/?thread_id={{ r.id }}">#{{ r.id }} {{ r.title or '無題' }}</a>
          <span style="color:#666;"> / {{ r.created_at }}</span>
          {% if r.is_public %}<span style="margin-left:6px;border:1px solid #cbe3ff;background:#eef7ff;border-radius:999px;padding:2px 8px;font-size:12px;">公開中</span>{% endif %}
          <span style="margin-left:6px;color:#666;">[{{ r.status }}]</span>
        </li>
      {% endfor %}
      {% if rows|length == 0 %}
        <li style="color:#666;">スレッドはまだありません。</li>
      {% endif %}
    </ul>
    """, rows=rows)

# -----------------------------
# エントリポイント
# -----------------------------
if __name__ == "__main__":
    app.run(host=BIND_HOST, port=BIND_PORT, debug=os.getenv("FLASK_DEBUG") == "1")