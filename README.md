# FT_NewsScoring — ニュース採点フォワードテスト実験装置

本リポジトリは、ニュースをLLMで±5にスコア化した値が将来の市場リターンに対して
取引コスト控除後も有意な予測力を持つかを、12ヶ月のフォワードテストで検証する
計測装置である。**売買執行機能ではない。** 詳細な設計思想・制約は [SPEC.md](SPEC.md) を、
実装レビュー観点は [REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md) を参照。

## セットアップ

### 1. 依存関係のインストール

Python 3.11+ が必要。

```bash
python -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. APIキーの設定(ローカル実行)

`.env.example` を `.env` にコピーし、Anthropic の API キーを設定する。
`.env` は `.gitignore` 対象であり、絶対にコミットしないこと。

```bash
cp .env.example .env
# .env を編集して ANTHROPIC_API_KEY=sk-... を設定
```

ローカル実行時は、シェルで環境変数として読み込んでから実行する。例(PowerShell):

```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') { Set-Item "env:$($matches[1])" $matches[2] }
}
```

### 3. GitHub Secrets の設定(Actions実行)

GitHub Actions から実行する場合、実キーはリポジトリの Secrets からのみ注入する。

1. GitHubリポジトリの `Settings` → `Secrets and variables` → `Actions` を開く
2. `New repository secret` から `ANTHROPIC_API_KEY` を追加し、値にAPIキーを設定
3. `.github/workflows/daily.yml` は `secrets.ANTHROPIC_API_KEY` を参照して自動実行される

## 実行方法(ローカル・Actions共通のエントリポイント)

各スクリプトは `src/` 配下にあり、ローカル・GitHub Actions のどちらからも同じ
コマンドで実行できる。

```bash
python src/score.py          # 未採点ニュースを取得(内部でfetch_news.pyを呼ぶ)し採点、scores.jsonlに追記
python src/fetch_prices.py   # 対象銘柄/指数の日足を取得し、prices.parquetを更新
python src/evaluate.py       # フォワードリターンを結合し、月次評価レポートを生成
python src/fetch_news.py     # (単体デバッグ用)未採点ニュース候補をJSONで標準出力に表示
```

`evaluate.py` は `--month YYYY-MM` でレポート対象月を指定できる(省略時は当月, JST)。

## 自動実行(GitHub Actions)

`.github/workflows/daily.yml` が以下を自動実行する(SPEC §3, §10-4)。

- 毎営業日 JST 07:00 / 16:30: `score.py` → `fetch_prices.py` を実行し、結果を
  `data/scores.jsonl` / `data/prices.parquet` に追記コミットする
- 毎月1日: `evaluate.py` を実行し、`data/reports/YYYY-MM.md` をコミットする
- `workflow_dispatch` から `collect` / `evaluate` を手動実行することも可能

## テスト

制約A(ルックアヘッド禁止)・制約B(学習データ汚染対策)のガードは
`tests/test_guards.py` で検証する。実装変更時は必ず全て通ることを確認すること
(SPEC §10 の実装順序指示)。

```bash
pytest tests/ -v
```

## 設計上の制約(要約)

詳細は [SPEC.md](SPEC.md) §0, §2 を参照。判断に迷ったら「予測精度を上げる方向」
ではなく「計測の汚染を防ぐ方向」に倒すこと。

- 売買執行・自動発注・過去データのバックテストは実装しない(スコープ外, §8)
- `data/scores.jsonl` は追記専用。既存レコードの更新・削除は行わない
- 採点プロンプトは `prompts/scorer_v1.md` から読み込み、SHA-256をレコードに保存する。
  改訂時は `scorer_v2.md` として新規追加し、旧スコアとは混合評価しない
- 採点対象は取得時点から48時間以内に公開されたニュースのみ(学習データ汚染対策)
- 記事本文はいかなるファイルにも保存しない(見出し・URL・スコアのみ保存)

## リポジトリ構成

構成の詳細は [SPEC.md §3.1](SPEC.md) を参照。
