# 技術仕様 DEV-001 v0.1

## 1. 対象とAdapter契約

試験対象は、操作ID `op-001`、整数amount `7` の業務効果をSQLiteへ一度だけ記録する合成サンプル。
別金額8は同一IDのpayload競合試験用。実顧客データ、送金、外部副作用は扱わない。

`adapter.py` の公開関数：

|API|入力・出力|契約|
|---|---|---|
|`initialize(directory, token)`|空ディレクトリ→新規lab.sqlite|既存DBを開くAPIではない。非空なら拒否|
|`validate_sandbox(directory, token)`|所有マーカー＋token→DBパス|一致必須。DB・ディレクトリのsymlink拒否|
|`connect(path, readonly=False)`|試験DB→新規接続|autocommit＋明示BEGIN。timeout=0。読戻しはmode=ro|
|`payload_hash(amount)`|整数→SHA-256|UTF-8正規化JSON `{"amount":7}` のハッシュ|
|`snapshot(path)`|試験DB→receipts/effects/integrity|独立接続の1読取トランザクションで一貫した読戻し。失敗は例外|

Adapterの操作実行契約は `worker.main()`：
1. `BEGIN IMMEDIATE` で書込直列化。
2. 操作IDの既存receiptを照合。同じhashと値なら効果を再実行せず既存完了を返す。違えばPAYLOAD_CONFLICT。
3. effectsとreceiptsを**同じDB・同じトランザクション**に挿入。
4. COMMIT成功後のみにCOMPLETEDを返す。

receipt主キーはop_id。effectsのUNIQUEを意図的に外している。正常実装は直列化＋receipt照合で一意性を守り、
壊れたAdapterでは独立オラクルが重複を検出する。制約を外したスキーマを本番設計の推奨とはしない。
汎用Adapter Protocol、ユーザー指定DB、外部アプリプラグインはまだ提供していない。

## 2. 子プロセスと障害注入

親は `subprocess.Popen([python, -u, worker.py, ...], shell=False)` で試験専用子だけを起動する。
任意PID、任意コマンド、既存DBを指定するCLIはない。

stdoutは改行区切りJSON、stdinは `continue` のみ。workerはcheckpointをflushした後に指定位置でstdinを待つ。
親がそのcheckpointを受領した後に、**そのPopenのkill()** を呼ぶ。LinuxではSIGKILL。returncodeを保存しwaitで回収する。
指定位置への到達と停止の間にCOMMITが進むことはない（workerがバリアで停止している）。
単なるsleepの競争による停止位置推定ではない。プロセス終了／ホスト障害に対する汎用監督ではない。

|位置|意味|利用ケース|
|---|---|---|
|before_begin|BEGIN呼出し前|拡張用。今回強制停止の主試験には未使用|
|before_commit|効果とreceiptのSQL後、COMMIT呼出し前|T01、T03、T05_commit|
|after_commit|COMMIT返却後、完了result送信前|T02、T04、T06_unsafe|
|commit_busy|実際のSQLITE_BUSY発生後、元トランザクション継続中|T05_commit|

バリアは計測用コードを対象へ追加する方式。無改修の任意アプリ内部で停止位置を自動発見する機能はない。
COMMIT内部のVFS／fsync呼出し中の停止、ハードウェア電源断、ディスクI/O障害の注入は含まない。
通常のSQL例外と、完了応答が来ないCOMMIT結果不明を混同しない。

## 3. 中立実験APIとLCC API

|API|役割|
|---|---|
|`Lab.spawn(name, pause=..., mutant=..., amount=...)`|所有子起動。同名再使用拒否|
|`Child.wait_point(point)`|checkpoint到達待ち。10秒上限|
|`Child.go()` / `finish()` / `kill()`|継続、終了回収、所有子の強制停止|
|`Lab.lock(reader/writer/exclusive)` / `unlock()`|試験用の別SQLite接続による実ロック競合|
|`Lab.observe()`|独立読戻し。SQLエラーコードも保存|
|`engine.resolve(lab)`|状態を判定し根拠snapshotのseqを記録|
|`engine.replay(lab, name)`|毎回resolveし、許可時だけ同じIDで新workerへ投入|
|`engine.diagnose(lab, snapshot)`|不変条件違反と危険な再投入を検出|
|`engine.execute(lab, plan)`|JSON手順を実行、観測値と期待値を照合|

JSON actions：`spawn`, `wait`, `go`, `finish`, `kill`, `lock`, `unlock`, `resolve`, `replay`。
`unsafe_replay` はT06専用の明示的な安全ガード迂回を作る負例。通常経路から呼ばない。
追加シナリオは `plans.json` のactionsとexpect/containsで定義可能。CLIが公開するケースIDは今回の9件に固定。
未知のactionは例外で停止する。汎用の条件分岐、任意SQL実行、ループ言語は実装していない。

## 4. 復旧判定と状態遷移

|観測／制約|判定|次の動作|
|---|---|---|
|所有workerのいずれかが生存|HOLD_IN_FLIGHT|新規業務実行を拒否|
|独立読戻しが失敗|HOLD_UNKNOWN|成功／失敗を確定せず再投入拒否|
|receiptも効果もなし、全worker終了済み|REPLAY_ALLOWED|同じID・payloadで再投入可能|
|receiptと効果が各1、ID/hash/amount一致、quick_check=ok|COMPLETED|新しい効果を作らず既存結果を返せる|
|片側のみ、重複、値不一致、integrity異常|HOLD_INVALID|自動修復・再実行せず調査|

典型的な遷移はT01で未観測→REPLAY_ALLOWED→COMPLETED、T04で未観測→HOLD_UNKNOWN→COMPLETED。
対象実行のcheckpoint・effect_executed・sql_error・resultと、復旧側のrecoveryは別のイベントとして保存する。
COMMIT結果不明のまま完了応答を根拠なく生成しない。

「全worker終了」「receiptなしなら安全に再実行可能」は、この閉じた単一DBのSQL専用fixtureの契約。
実サービスの他worker、キュー、外部副作用、レプリカ、後から確定する処理を排除する証明ではない。
観測後の外部変更を防ぐフェンシング／世代管理は未実装。したがって本番へ流用しない。

## 5. ロックと不明結果の扱い

T05_begin：別接続のBEGIN IMMEDIATE中に対象がBEGIN IMMEDIATEを試み、SQLITE_BUSYを実測。
T05_commit：対象が書込み後、別接続に読取りロックを保持させてCOMMITする。DELETE journalでSQLITE_BUSYを発生。
元接続の `in_transaction=True` を記録し、競合解除後に**同じ接続のCOMMITだけ**を再試行。effect_executedは1回。
一般のCOMMITエラーを全て同じ方法で再試行する実装ではない。

T04：COMMIT後に子を停止し、別接続のBEGIN EXCLUSIVEで新しい読戻し接続を失敗させる。
これは実際のSQLite読戻しSQLITE_BUSY。物理媒体の読取り不良やDB破損ではない。
障害位置を知る試験側と完了応答を失ったアプリ側の知識は別。復旧は独立読戻しが成功するまで保留する。

SQLite公式仕様では、COMMITのSQLITE_BUSY後にトランザクションが継続し、後にCOMMITを再試行できる場合がある。
参考：<https://www.sqlite.org/lang_transaction.html>、<https://www.sqlite.org/rescode.html>。
killの仕様：<https://docs.python.org/3/library/subprocess.html#subprocess.Popen.kill>。

## 6. 診断と証拠形式

- DUPLICATE_EFFECT：op_idごとのeffects件数が2以上。
- RECEIPT_EFFECT_MISMATCH：receiptに全フィールドが一致する効果が1件でない。
- ORPHAN_EFFECT：対応receiptがない効果。
- FALSE_COMPLETION：COMPLETED応答があるが、最終独立読戻しで効果とreceiptが各1でない。
- UNSAFE_REPLAY：直前の復旧状態がHOLD_*のまま、実際のspawnが記録された。負例の自己申告タグだけに依存しない。
- UNRESOLVED / INTEGRITY_FAILURE：読戻し不明／整合性異常。

既知の不変条件に基づくルール診断であり、未知の根本原因を自動発見するものではない。
FALSE_COMPLETIONは今回の単一操作・追記専用対象の最終snapshotに基づく。任意の更新／削除業務への一般化は未実装。

result.json：mode/case/iteration、passed、runtime_ms、checks（LCC）またはunittest失敗（OSS）、
findings、final_snapshot、全子のevents/returncode/stderr、trace。
trace.jsonl：連番seq、親起点monotonic elapsed_ns、checkpoint到達、kill位置、ロック、読戻し、復旧判定、再投入。
environment.json：Python/SQLite/OS、seed、条件、ソースと計画のSHA-256。
summary.json：全実行数、方式別集計、対ごとの最終状態・診断コード一致、工数null。

sandbox tokenは誤操作防止用。悪意ある同権限プロセスへのセキュリティ境界ではない。
親の証拠ログは子のkillには耐えるが、ホスト電源断に対するfsync保証はない。
