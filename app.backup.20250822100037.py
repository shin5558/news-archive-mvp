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
    render_template_string, abort
)

from dotenv import load_dotenv
load_dotenv()  # .envファイルを自動読み込み

import httpx  # OpenAI呼び出しを安定させるためにhttpxを採用

# 追加インポート
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from flask import session, g
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

# セッション用シークレットキー（未設定なら暫定生成）
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)






# 簡易ログイン必須デコレータ
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return wrapper

# リクエスト毎の current_user ロード
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

# 画面: サインアップ
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

# 画面: ログイン
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

# スレッド作成（タイトル＋最初の本文を1件目のpostとして保存）
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
    return redirect(url_for("view_thread", thread_id=thread_id))

# スレッド詳細（投稿一覧＋AI要約（最新1件））
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

    # 簡易テンプレ：既存 index.html と別ページ（シンプル）
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

# 返信投稿
@app.route("/threads/<int:thread_id>/posts", methods=["POST"])
@login_required
def add_post(thread_id: int):
    content = sanitize_text(request.form.get("content","").strip())
    parent  = request.form.get("parent_post_id","")
    parent_id = int(parent) if (parent and parent.isdigit()) else None
    if not content:
        return "content は必須です", 400

    # スレッド存在確認
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
    return redirect(url_for("view_thread", thread_id=thread_id))

# 通報（オプション）
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
# サニタイズ（任意のローカル実装にフォールバック）
# -----------------------------
def _fallback_sanitize(text: str) -> str:
    """最低限の整形。過度に削らない。"""
    if text is None:
        return ""
    # 制御文字を落とす
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    # 末尾トリム
    return text.strip()

def _mask_pii(text: str) -> str:
    """公開ページ用の簡易マスク（メール・電話・住所らしきもの）"""
    if not text:
        return text
    # メール
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email masked]", text)
    # 電話（単純化）
    text = re.sub(r"\b(\+?\d{1,3}[- ]?)?\d{2,4}[- ]?\d{2,4}[- ]?\d{3,4}\b", "[tel masked]", text)
    # 住所らしき（都道府県/市区郡程度をマスク・簡易）
    text = re.sub(r"(東京都|大阪府|福岡県|北海道|京都府|神奈川県|愛知県|埼玉県|千葉県|兵庫県|福岡市|横浜市)[^\n、，。 ]{0,20}", "[address masked]", text)
    return text

try:
    # 任意: sanitize.py があるなら使う（なければフォールバック）
    from sanitize import sanitize_text as _ext_sanitize_text  # type: ignore
    def sanitize_text(text: str) -> str:
        return _ext_sanitize_text(text) or _fallback_sanitize(text)
except Exception:
    sanitize_text = _fallback_sanitize

# -----------------------------
# Flask アプリ
# -----------------------------
app = Flask(__name__)

# -----------------------------
# テンプレート読込
# -----------------------------
def load_index_html() -> str:
    try:
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
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
    _ensure_parent_dir(DB_PATH)  # ← 追加（フォルダがなければ作る）
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
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
    conn.commit()
    conn.close()

init_done = False

@app.before_request
def _on_boot():
    global init_done
    if not init_done:
        init_db()
        init_done = True

# -----------------------------
# OpenAI 呼び出し
# -----------------------------
def _build_thread_prompt_for_summary(article_text: str, recent_posts_text: str) -> str:
    # 会話風でも、分析でも差し替え可
    return (
        "あなたは議論の進行役です。以下の『記事』と『最近の投稿』を読み、"
        "会話調で、6〜8文（250〜400字）に収めて、"
        "合意点・懸念・次の一歩が自然に伝わるコメントを日本語で返してください。"
        "\n----\n【記事】\n" + article_text +
        "\n----\n【最近の投稿（新しい順）】\n" + recent_posts_text + "\n"
    )

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

    # 直近N件
    cur.execute("SELECT id, content FROM posts WHERE thread_id=? ORDER BY id DESC LIMIT ?", (thread_id, N_RECENT))
    recent = cur.fetchall()
    if not recent:
        conn.close()
        return "投稿がありません", 400

    latest_post_id = recent[0]["id"]
    recent_text = "\n\n".join([f"- {r['content']}" for r in recent])

    # キャッシュキー
    mode  = "conversation"
    model = OPENAI_MODEL
    raw_for_hash = f"{article_text}\n#last={latest_post_id}\n#mode={mode}\n#model={model}"
    hash_key = hashlib.sha256(raw_for_hash.encode("utf-8")).hexdigest()

    # 既存キャッシュ確認
    cur.execute("SELECT content FROM ai_summaries WHERE thread_id=? AND hash_key=? LIMIT 1", (thread_id, hash_key))
    hit = cur.fetchone()
    if hit:
        conn.close()
        # 既存ヒット → スレッド画面へ戻す（最新表示される）
        return redirect(url_for("view_thread", thread_id=thread_id))

    # 生成
    ok, text = call_openai(
        article=article_text,
        comment=recent_text
    )
    if not ok or not text.strip():
        conn.close()
        return f"AI生成に失敗しました: {text}", 500

    cur.execute("""INSERT INTO ai_summaries (thread_id, model, mode, content, hash_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (thread_id, model, mode, text.strip(), hash_key, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return redirect(url_for("view_thread", thread_id=thread_id))

# -----------------------------
# スレッド/メッセージ操作
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
    # index.html の期待に合わせた形へ
    return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]

def set_publish(thread_id: int, make_public: bool) -> Tuple[bool, Optional[str]]:
    conn = get_db()
    cur = conn.cursor()

    if make_public:
        # すでにトークンあれば再利用、無ければ発行
        cur.execute("SELECT public_token FROM threads WHERE id = ?", (thread_id,))
        row = cur.fetchone()
        token = row["public_token"] if row and row["public_token"] else secrets.token_urlsafe(18)
        cur.execute(
            "UPDATE threads SET is_public=1, public_token=? WHERE id=?",
            (token, thread_id)
        )
        conn.commit()
        conn.close()
        # ベースURLがあればそれを使う。無ければ相対パス
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

# -----------------------------
# ルーティング
# -----------------------------
@app.route("/", methods=["GET"])
def home():
    thread_id = request.args.get("thread_id", "").strip()
    active_thread_id = None
    history = []
    share_url = None

    if thread_id.isdigit():
        active_thread_id = int(thread_id)
        th = get_thread(active_thread_id)
        if th:
            # 公開中なら共有URLを表示
            if th["is_public"]:
                token = th["public_token"]
                share_url = f"{PUBLIC_BASE_URL.rstrip('/')}/p/{token}" if PUBLIC_BASE_URL else url_for("public_view", token=token, _external=True)
            history = get_history(active_thread_id)
        else:
            active_thread_id = None

    return render_template_string(
        INDEX_HTML,
        article="",
        result=None,
        threads=list_threads(),
        active_thread_id=active_thread_id,
        history=history,
        share_url=share_url
    )

@app.route("/analyze", methods=["POST"])
def analyze():
    # form fields
    raw_article = request.form.get("article", "")
    raw_comment = request.form.get("comment", "")
    raw_thread_id = request.form.get("thread_id", "").strip()
    thread_title = request.form.get("thread_title", "").strip() or None

    # sanitize（削りすぎない）
    article = sanitize_text(raw_article)
    comment = sanitize_text(raw_comment)

    # スレッド決定
    if raw_thread_id and raw_thread_id.isdigit():
        thread_id = int(raw_thread_id)
        if not get_thread(thread_id):
            thread_id = create_thread(thread_title)
    else:
        thread_id = create_thread(thread_title)

    # ユーザー発言として保存（記事・コメントをまとめて1メッセージ扱い）
    user_blob = f"【記事】\n{article}\n\n【コメント】\n{comment}"
    add_message(thread_id, "user", user_blob)

    # OpenAI 呼び出し
    ok, text = call_openai(article, comment)

    # 成功/失敗をメッセージに保存
    role = "assistant" if ok else "system"
    add_message(thread_id, role, text)

    # 画面表示用に履歴とスレッド一覧を用意
    th = get_thread(thread_id)
    share_url = None
    if th and th["is_public"] and th["public_token"]:
        token = th["public_token"]
        share_url = f"{PUBLIC_BASE_URL.rstrip('/')}/p/{token}" if PUBLIC_BASE_URL else url_for("public_view", token=token, _external=True)

    return render_template_string(
        INDEX_HTML,
        article=article,
        result=text,
        threads=list_threads(),
        active_thread_id=thread_id,
        history=get_history(thread_id),
        share_url=share_url
    )

@app.route("/threads/<int:thread_id>/publish", methods=["POST"])
def publish(thread_id: int):
    # form: make_public = 'true' or 'false'（現在値。ここでは反転して適用）
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
    <h2>スレッド一覧</h2>
    <div style="margin-bottom:12px;">
    {% if g.current_user %}
      <form method="post" action="/threads" style="border:1px solid #ddd;padding:8px;border-radius:8px;">
        <div>タイトル: <input name="title" required style="width:60%"></div>
        <div>本文: <br><textarea name="body" required style="width:100%;min-height:100px;"></textarea></div>
        <button type="submit">スレッドを作成</button>
      </form>
      <form method="post" action="/logout" style="margin-top:6px;"><button>ログアウト</button></form>
    {% else %}
      <a href="/login">ログイン</a> または <a href="/signup">新規登録</a> してください。
    {% endif %}
    </div>

    <ul>
      {% for r in rows %}
        <li style="margin:6px 0;">
          <a href="/threads/{{ r.id }}">#{{ r.id }} {{ r.title }}</a>
          <span style="color:#666;"> by {{ r.author_name or 'unknown' }} / {{ r.created_at }}</span>
          <span style="margin-left:6px;color:#666;">[{{ r.status }}]</span>
        </li>
      {% endfor %}
    </ul>
    """, rows=rows)

# -----------------------------
# エントリポイント
# -----------------------------
if __name__ == "__main__":
    # 本番運用では WSGI(eg. gunicorn) 推奨
    app.run(host=BIND_HOST, port=BIND_PORT, debug=os.getenv("FLASK_DEBUG") == "1")