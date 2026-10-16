# 入金確認アラート（Moneytree × Chatwork / LINE WORKS）サンプル

住宅会社の入金予定（契約金・着工金・中間金・最終金）と Moneytree の入金明細を突き合わせ、
- 担当者に「入金大丈夫？」を事前に Chatwork か LINE WORKS で送る（返信がなければ再送）
- 予定日を過ぎても入っていなければ上長にアラート、遅れの原因を番号で返信してもらう
- 今月・年間の入金進捗と、原因別の遅れ金額を集計する

お客様名・金額・人名・明細はすべて架空です。`config.json` の mode が `mock` のあいだは外部に一切通信しません。
画面イメージは `画面サンプル.html`（ブラウザで開く）。

## 動かし方（Python 3.9以上・標準ライブラリのみ）
```
python main.py demo                         # 10/16〜10/22 を早送りで再現し、最後に進捗を表示
python main.py run --now 2026-10-18T09:00   # 1回分の定期実行
python main.py reply A001 "20日に振込予定です" --now 2026-10-17T11:30   # 担当者の返信を再現（モック）
python main.py progress --now 2026-10-22T11:00 --csv   # 今月・年間の進捗と遅れの原因（CSVも出力）
python main.py reason P05-2 3 "建築確認の補正待ち"     # 遅れの原因を手で記録
python main.py status
python main.py reset
python main.py --service lineworks demo     # 通知先を LINE WORKS にして再現
```

## 通知先（Chatwork / LINE WORKS）
`config.json` の `notify_service` を `chatwork` か `lineworks` にします。画面サンプルでは「送信設定」の一番上と「通知のようす」のチャット欄で切り替えられます。

| | Chatwork | LINE WORKS |
|---|---|---|
| 宛先 | `[To:アカウントID]` | `<m userId="ユーザーID">`（要実機確認） |
| 担当者のID | `assignees[].account_id` | `assignees[].lineworks_user_id` |
| 送り先 | `chatwork.room_id` | `lineworks.channel_id`（トークルームのチャンネルID） |
| どの確認への返事か | 返信ボタン | **返信の先頭に案件番号**（例 `A001 確認しました`） |
| 返信の読み取り | APIでルームの直近100件を読む | **受け口が必要**（`lineworks_inbox_worker.js`） |

どちらも、手がかりのない発言は「その担当者の確認待ちが1件だけ」のときに限り返事とみなします。

## 入金予定表（data/expected.csv）
| 列 | 内容 |
|---|---|
| id / お客様 / 物件 / 区分 / 金額 / 入金予定日 | 予定の1行 |
| 振込名義 | 半角カナ。**ご主人・奥様など複数を `/` 区切り**（例 `ﾔﾏﾀﾞ ﾀﾛｳ/ﾔﾏﾀﾞ ﾊﾅｺ`） |
| 担当者名 / 担当者ID / ルームID | ID が空なら config の `assignees` と `chatwork.room_id` で補う |
| 入金済日 | 導入前にすでに入金済みのものは日付を入れておく（通知しない） |

## 入金の照合
- 振込名義のどれか＋金額が一致 → 入金済み
- 名義に登録していない家族の名前でも、**苗字と金額が一致**すれば入金済み（メッセージに「念のため確認」を添える）。`match_surname_with_amount: false` で無効
- 名義か金額の片方だけ一致した入金は、未入金アラートに「この入金ではないか」として載せる
- 照合する期間は予定日の7日前〜14日後

## ルール（config.json の rules）
| 項目 | 既定 | 内容 |
|---|---|---|
| ask_days_before | 3 | 予定日の3日前に確認を送る |
| remind_after_hours / max_reminders | 24 / 2 | 返信がなければ24時間ごとに2回まで再送 |
| cc_manager_from_reminder | 2 | 再送2回目から上長も [To] |
| overdue_grace_days | 1 | 予定日＋1日で未入金アラート（原因の選択肢つき） |
| send_hours | [9, 19] | この時間帯だけ送る |
| paid_notice_within_days | 3 | これより古い入金は「入金確認」を送らず静かに入金済みにする |
| fiscal_year_start_month | 4 | 年間集計の期首月 |

遅れの原因の選択肢は `delay_reasons`、文面は `templates` で変更できます（画面サンプルの「送信設定」→「config.json を表示」で書き出せます）。
担当者が未入金アラートに「2 上棟が11月にずれたため」のように番号から返信すると、原因として記録されます。

## 本番にするとき
1. **Moneytree**：`moneytree.mode` を `csv` にし、明細CSVを `data/moneytree_export.csv` に置く（列名は `csv_columns` で合わせる）。APIで直接取るには Moneytree LINK の契約が必要
2. **Chatwork**：`chatwork.mode` を `real`、Bot用アカウントのトークンを環境変数 `CHATWORK_API_TOKEN` に、`bot_account_id` にそのアカウントID
3. **LINE WORKS**（Chatwork の代わりに使う場合）
   - 管理者が Developer Console でアプリ（Client ID・Client Secret・サービスアカウント・秘密鍵）と Bot を作り、Bot を通知用トークルームに招待する
   - `lineworks_inbox_worker.js` を Cloudflare Workers に置き（KV `INBOX`・Secret `BOT_SECRET` `INBOX_TOKEN`）、`https://<Worker>/callback` を Bot の Callback URL に登録
   - `lineworks` の `mode` を `real`、`bot_id`・`client_id`・`service_account`・`inbox_url`（Worker のURL）を入れ、環境変数 `LINEWORKS_CLIENT_SECRET`・`LINEWORKS_PRIVATE_KEY`（PEMの中身）・`LINEWORKS_INBOX_TOKEN` を設定
   - 認証の署名に `pip install cryptography` が必要（モックでは不要）
4. **定期実行**：1時間ごとに `python main.py run`、月初などに `python main.py progress --csv`

⚠️ 実データ（明細CSV・状態ファイル・進捗CSV）は `.gitignore` で除外しています。トークンはファイルに書かないでください。
