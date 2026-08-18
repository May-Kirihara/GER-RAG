# Multiverse クイック健康チェック

> 🚨 パニック時・アラート時・朝一番・異変に気づいた時 — とりあえずこれを 1 分で走らせる。
> **全部 OK = まず正常。NG が出たら下の「🚑 NG 時の応急処置」の 1 行目だけ読む。**
> 詳細は [Multiverse 運用プレイブック §13](Operations-Multiverse-Operations.md#§13-動作確認回帰確認チェックリスト技術-smoke--回帰) へ。

---

## 📋 前提（初回だけ）

```bash
export MV_ROOT=${GAOTTT_MULTIVERSE_ROOT:-$HOME/.local/share/gaottt-multiverse}
export SUP_KEY=$GAOTTT_SUPERVISOR_ADMIN_KEY
export API_KEY=<宇宙作成時に受け取った平文 api_key>
```

---

## ✅ 本体（上から順に 5 行 — 前 5 つ通れば安心）

```bash
# 1. supervisor と embedding service のプロセスが生きている
ps -ef | grep -c "[g]aottt.*\(mcp_server\|embedding\)"

# 2. admin 認証が通る（supervisor が正しく起動している）
curl -fsS -H "Authorization: Bearer $SUP_KEY" http://127.0.0.1:7880/admin/status > /dev/null && echo OK

# 3. 宇宙が全て active（orphan がない）
curl -fsS -H "Authorization: Bearer $SUP_KEY" http://127.0.0.1:7880/admin/universes \
  | jq '[.[] | select(.status != "active")] | length'

# 4. route 解決が通る（API key が有効・backend が spawn できる）
curl -fsS -X POST http://127.0.0.1:7880/route \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$API_KEY\"}" > /tmp/_route.json \
  && jq -r '.url' /tmp/_route.json

# 5. backend が recall に応答する（最重要 — これが通れば実質全部動いている）
curl -fsS -H "Authorization: Bearer $(jq -r .token /tmp/_route.json)" \
  "$(jq -r .url /tmp/_route.json)" \
  -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"recall","arguments":{"query":"smoke","top_k":1}},"id":1}' \
  > /dev/null && echo OK
```

### 期待結果と NG 時の 1 行

| # | OK | NG の場合 |
|---|---|---|
| 1 | `2` 以上（supervisor + embedder） | プロセスが落ちている → [🚑 1](#🚑-ng-時の応急処置) |
| 2 | `OK` | supervisor が止まった / admin key 誤り → [🚑 2](#🚑-ng-時の応急処置) |
| 3 | `0`（全 active） | orphan 宇宙あり → supervisor 再起動で `reconcile()`、または [詳細](Operations-Multiverse-Setup.md#制限事項-v1) |
| 4 | `http://127.0.0.1:<port>/mcp` | API key 失効 / backend spawn 失敗 → [🚑 3](#🚑-ng-時の応急処置) |
| 5 | `OK`（結果空でもエラー出なければ OK） | engine startup 失敗 / lease 競合 → [🚑 4](#🚑-ng-時の応急処置) |

> **項目 1 の「2 以上」について**: 本番 systemd 運用（`deploy/gaottt-embedder.service` 常駐）では supervisor + embedder の 2 process が基本。**開発機（systemd 無し）では embedder は supervisor が lazy spawn する**（`supervisor_spawn_embedder=True`、default 有効）ので、初回 `/route` 前は supervisor 1 process でも正常・route 後に embedder が spawn されて 2 process になる。詳細: [Tuning §MV3 follow-on](Operations-Tuning.md#multiverse-supervisor--embedder-lazy-spawn-mv3-follow-on2026-07-06)

---

## 🚑 NG 時の応急処置

**まずこれを 1 分でやる（順不同で OK、急ぐなら上から）：**

1. **supervisor 再起動** — `systemctl restart gaottt-supervisor`（または該当する起動スクリプト）
2. **embedding service 再起動** — `systemctl restart gaottt-embedder`（port 7879）。**開発機（systemd 無し・supervisor lazy spawn 構成）では supervisor の再起動で embedder も再 spawn される**（手動 restart 不要）。`owned_terminating` に固まっている場合は [Troubleshooting §lazy spawn](Operations-Troubleshooting.md#supervisor-が-lazy-spawn-した-embedder-が-owned_terminating-に固まる) の手動 recovery 手順
3. **FAISS が壊れた疑い** — 全プロセス停止 → `scripts/rebuild_faiss_from_db.py --apply` → `--check` → 再起動（[Troubleshooting](Operations-Troubleshooting.md) 問題5.5）
4. **LeaseHeldError が出る** — 同じ宇宙を 2 プロセスで開いている。片方を止めるか `--force-takeover`（[Troubleshooting](Operations-Troubleshooting.md) §LeaseHeldError）
5. **litestream 設定が更新されない** — `GAOTTT_LITESTREAM_CONFIG_PATH` 未設定 or cron 停止（[Backup & DR §supervisor hook](Operations-Backup-Multiverse.md#supervisor-hook)）
6. **とにかく分からない** — supervisor と embedder を再起動 → 1 分待つ → 本体をもう 1 回

---

## 🧪 時間がある時・四半期・商用導入前

- [**詳細チェック（22 項目 + 症状早見表）**](Operations-Multiverse-Operations.md#§13-動作確認回帰確認チェックリスト技術-smoke--回帰) — MV5 実装後の回帰確認・商用サインオフ
- [**DR 演習（災害復旧 drill）**](Operations-Backup-Multiverse.md#dr-drill四半期実行) — `scripts/dr_drill.py` を 1 回走らせて EXIT=0 を確認
- [**7 階層パフォーマンス回帰**](Operations-Performance-Testing.md) — hot path 変更時に Tier 1/3/6

---

## 🔗 関連

- [Multiverse 運用プレイブック（統合）](Operations-Multiverse-Operations.md) — 全体像・ライフサイクル・評価方法
- [Multiverse Setup (MV3)](Operations-Multiverse-Setup.md) — supervisor 本体
- [Control Plane (MV4)](Operations-Control-Plane.md) — Postgres・台帳
- [Backup & DR (MV5)](Operations-Backup-Multiverse.md) — バックアップ・復旧
- [Troubleshooting](Operations-Troubleshooting.md) — 詰まった時
