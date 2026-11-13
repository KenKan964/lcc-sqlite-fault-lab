# デモ手順・検出根拠（DEV-001公開版）

## 再現する故障条件

試験用子プロセスが `BEGIN IMMEDIATE`、`INSERT effects`、`INSERT receipts`、`COMMIT` を実行する。`after_commit`チェックポイントは `COMMIT` が成功してから、完了通知の前にある。

親プロセスは子のチェックポイントイベント受信後、`Popen.kill()` で当該子を強制終了し、`returncode`および完了通知が**存在しないこと**を検査する。`sleep()`で停止位置を推測しない。

別のSQLite接続から、業務効果と操作IDの記録が**同一トランザクションで確定していること**を確認する。その後、新しい子プロセスに同じ操作IDを投入する。再投入側は既存receiptを検出し、業務効果を増やさず`COMPLETED, replayed=true`を返す。

## DB構造

- `receipts(op_id PRIMARY KEY, payload_hash, amount)`：確定操作の永続記録
- `effects(seq PRIMARY KEY, op_id, payload_hash, amount)`：業務効果の試験記録
- **意図的に**`effects`の一意制約を省き、不正実装が重複を作れるようにする（負例検出テストのため）。本番スキーマの設計例ではない。

## 自動判定の根拠

| 証拠 | 合格する条件 |
|---|---|
| 初回のcheckpoint | `after_commit`、`in_transaction=false` |
| 初回のプロセス | 強制終了、完了`result`イベントなし |
| 再起動時の独立読戻し | `effects`に確定内容が存在 |
| 復旧判定 | `COMPLETED` |
| 新子プロセス | 別PID、同じ`op_id`、`replayed=true` |
| 新子による副作用 | `effect_executed`イベントが0 |
| 最終DB | receipt1件、effect1件、op_id／amount／hashが一致 |
| DB整合性 | `PRAGMA quick_check=ok` |
| LCC診断器 | findingsが空 |

JSONでは`cases[*].target_is_known_bad`で正常サンプルと故意の不正を区別する。

## 故意の欠陥ケース

- `duplicate`：壊れたワーカーが同一IDでeffectを2件作る。`DUPLICATE_EFFECT`と`RECEIPT_EFFECT_MISMATCH`を検出。
- `false_complete`：保存せずに`COMPLETED`を返す。`FALSE_COMPLETION`を検出。
- `unsafe_replay`：COMMIT後停止、排他ロックにより読戻し失敗→`HOLD_UNKNOWN`。通常の再投入は拒否される。不正ワーカーがガードを迂回した場合、`UNSAFE_REPLAY`と二重effectを検出。

## 再現性の限界

同じ試験コマンドによって、障害位置と期待した状態を再現する。PID・時間・絶対パス・環境による実行時間の一致は要求しない。単一DBに閉じたサンプルなので、他のwriterや外部副作用が存在する実システムへ結果を外挿しない。

今回のデモはSQLite内のCOMMIT後を対象とし、実電源断、I/O故障、fsync故障、COMMIT内部、WALモード、Drift/Roomの実動作を保証しない。障害位置を知る試験監督者と、アプリの利用者に提示する完了状態を混同しない。

## 公開情報として提示できること

「再現可能な合成サンプルで、COMMIT直後の停止と同じIDの再投入による二重確定防止を検証できた」こと。

**提示してはいけないこと：** 「SQLiteを使うすべてのアプリで安全」「端末電源断後も必ず安全」「根本原因を自動特定」「企業の工数を削減した」「商用SDKが完成した」。
