声を消させない・議論整理（MVP）

📌 概要

このプロジェクトは「声を消させない・議論整理」をテーマにした AI搭載掲示板アプリ (MVP) です。
ユーザーが投稿した記事やコメントを AIが自動で要約・分類（賛成 / 反対 / 代替案） し、議論を整理して見やすくします。
「意見が埋もれず、建設的に進められる掲示板」を目指しています。

✨ 機能
	•	複数ユーザーによるスレッド形式の掲示板
	•	投稿された記事本文やコメントの保存
	•	AIが即時にレスポンスし、要点をまとめる
	•	Web UI からリアルタイムで確認可能
	•	HTTPS (Let’s Encrypt) による安全な通信対応

🛠 技術スタック
	•	言語/フレームワーク: Python (Flask / FastAPI)
	•	Webサーバー: Nginx + Gunicorn
	•	データベース: SQLite（MVP段階）
	•	AI: OpenAI API (ChatGPT系)
	•	OS: Ubuntu 24.04 (Xserver VPS)

🚀 セットアップ方法
1.	リポジトリをクローン
git clone https://github.com/ユーザー名/news-archive-mvp.git
cd news-archive-mvp

2.	仮想環境を作成 & パッケージインストール
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

3.	.env を作成して環境変数を設定
OPENAI_API_KEY=あなたのAPIキー

4.	開発環境で起動
gunicorn -b 127.0.0.1:8000 app:app

5.	本番環境では systemd + nginx を使用

🌐 デモ

実際に稼働しているMVP:
👉 https://shin5558.net

📄 ライセンス

MIT License
