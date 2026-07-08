# Operations — Resource Requirements

GaOTTT のリソース消費は **embedding model (RURI v3 310m)** が支配的。FAISS / SQLite / BM25 index は DB が数十万ノード規模になるまでは embedding に対して小さい。本ページは **開発機での実測値 (2026-05-28)** + DB スケーリング + OOM 時の挙動をまとめる。

> 実験の詳細レポートは [Research — VRAM/RAM Measurement 2026-05-28](../research/Research-VRAM-RAM-Measurement-2026-05-28.md) 参照。

## GPU / CPU 別の実測 (DB ~41,000 nodes)

| シナリオ | GPU VRAM | システム RAM | スループット | 備考 |
|---|---|---|---|---|
| GPU, recall 中心 (cold start) | ~4.2GB | ~2GB | — | model load 直後・ingest 未実行の最小値 |
| GPU, recall 中心 (warm = ingest 後) | **~6.7GB** | ~6.9GB | — | PyTorch allocator が前回 ingest 分の reserved pool を保持。実質これが定常 idle |
| GPU, MCP 経由 ingest (~100 chunk corpus) | peak ~6.7GB (+2.5GB) | ~6.9GB | **~6 chunk/s** | model は backend 常駐分を再利用 |
| GPU, batch 経由 ingest (新 process, ~100 chunk) | 合算 **~11.4GB** (backend 6.7 + 新 4.7) | 合算 ~13GB (backend 6.9 + 新 6.3) | ~3 chunk/s | 新 process が独自に model load (startup +90 秒で BM25/FAISS 再構築) |
| GPU, 大規模 ingest (数千 chunk 級) | peak ~16GB (推定) | — | — | 小規模測定からの外挿。`batch_size=32` × RURI max_seq=8192 で attention が爆発 |
| CPU, ingest (新 process, ~100 chunk corpus) | — | peak **~9.4GB** | **~0.4 chunk/s** (GPU の 1/15) | engine startup 120 秒、`CUDA_VISIBLE_DEVICES=""` で強制。`batch_size` を 4-8 に絞ると peak 抑えられる可能性 (未測定) |
| CPU, recall 中心 | — | ~5-6GB | — | model load + engine startup 後の安定値、推定 |

### 重要な観察

1. **PyTorch caching allocator は ingest 後に reserved を返さない**: バックエンドの "idle" baseline は ingest 前 4.2GB → ingest 後 6.7GB に +2.5GB 残留。OS 視点では空きでも実質的には握ったまま。**warm idle = 定常 idle** と考えるのが現実的。
2. **Batch route (新 process) の overhead は startup が支配的**: process spawn + RURI load (~24s) + engine startup (BM25 build + FAISS load + virtual FAISS, ~90s) + 実 ingest。Daily 運用で `scripts/load_files.py` を頻繁に回すなら毎回この ~100s を払う。常駐 MCP backend 経由なら ~0 秒。
3. **CPU bulk ingest は実用範囲外、ただし single-call remember は実用範囲**: GPU の 1/15 速度 (0.4 vs 6 chunk/s) は **batch ingest 前提** の数字。常駐 backend を warm に保った状態での 1 remember あたりの実測レイテンシは下記の通り、会話メモリ用途では完全に実用範囲 (詳細は次節)。

## Conversational memory での CPU 実用性

「LLM との会話を 1 件ずつ remember する」用途で CPU 動作が耐えられるかは、batch ingest とは別の問題。**常駐 backend を warm に保ったまま単発 `remember` を投げ続ける** 場合のレイテンシは:

| Content 長 | CPU latency (avg, n=5) |
|---|---|
| 28 chars (短文 1 行) | **0.24s** |
| 126 chars (会話 1 ターン分の抜粋) | **0.27s** |
| 245 chars (中程度のスニペット) | **0.45s** |
| 804 chars (長めのパッセージ) | **0.88s** |

→ 一般的な会話メモリ (100-500 chars) は **~250-500ms / 件**。Slack に短文投稿する程度の体感速度で、interactive 用途で十分実用。

### Bulk vs single-call の速度比が違う理由

| 用途 | GPU | CPU | 比 |
|---|---|---|---|
| Bulk ingest (batch_size=32) | ~6 chunk/s | ~0.4 chunk/s | **1/15** |
| Single remember (warm backend) | ~100-200ms 想定 | ~250-500ms (実測) | **~1/2-1/3** |

GPU の batch 並列性は 32 件まとめて処理することで固定 overhead を amortize する設計。1 件ずつなら GPU の優位性は薄れ、CPU との差は **2-3 倍** 程度に縮まる。

### CPU mode の現実的な運用パターン

- **初回 engine startup ~125 秒は避けられない** (常駐前提)
- それさえ済めば、conversational remember は GPU 不要で運用可能
- recall も CPU で 1-3 秒/query 程度、これも実用範囲
- 大規模 corpus を一括投入したいときだけ一時的に GPU 環境に移動、で十分

つまり **「普段は CPU で十分、初期 ingest だけ GPU」** という非対称運用が成立する。

## Multiverse MV1: embedding service 分離時のリソース試算（2026-07-02）

[Multiverse MV1](Plans-Multiverse-Scale-Out.md) で RURI model load を engine プロセスから分離できるようになった（[Operations — Server Setup](Operations-Server-Setup.md) の「embedding service を分離する」節）。これにより **engine プロセス単体の RAM（model 抜き）** が初めて直接計測可能になった。実測値は **次セッションで追記**（real RURI が必要）。計測手順のみここに固める。

### 計測手順

```bash
# 1. embedding service を起動（model はこのプロセスに乗る）
.venv/bin/python -m gaottt.embedding.service --host 127.0.0.1 --port 7879 &
SERVICE_PID=$!

# 2. service の /info が応答するまで待つ
until curl -s http://127.0.0.1:7879/info > /dev/null; do sleep 0.5; done

# 3. engine を RemoteEmbedder 接続で起動（model は engine プロセスに乗らない）
GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879 \
GAOTTT_DATA_DIR=/tmp/gaottt-measure \
.venv/bin/python -m gaottt.server.mcp_server --transport streamable-http --port 7878 &
ENGINE_PID=$!

# 4. 両プロセスの RSS を取る
ps -o rss= -p $SERVICE_PID  # → embedding service（model 込み）
ps -o rss= -p $ENGINE_PID   # → engine 単体（model 抜き）← これが知りたい値

# 5. warm 状態で recall / remember を数回走らせてから再計測（PyTorch allocator の reserved が安定した値）
```

> **★ 開発機（supervisor lazy spawn）との関係**: 開発機では multiverse supervisor が embedding service を lazy spawn するため（`supervisor_spawn_embedder=True`、default 有効）、上記の手動起動は不要です。**本手順で model 抜き engine の RSS を直接計測したい場合**は、supervisor の lazy spawn を opt-out（`GAOTTT_SUPERVISOR_SPAWN_EMBEDDER=0`）して手動起動ベースで計測してください。lazy spawn 機構自体の RSS overhead は supervisor プロセス内の watchdog task のみで無視可能です（[Tuning §MV3 follow-on](Operations-Tuning.md#multiverse-supervisor--embedder-lazy-spawn-mv3-follow-on2026-07-06)）。

### 期待される構成（次セッションで実測確認）

| プロセス | RAM 構成 | 備考 |
|---|---|---|
| embedding service | model + torch runtime + FastAPI/uvicorn | 既存実測 ~5-6GB から大きくは動かない |
| engine（model 抜き） | FAISS ×2 + BM25 + cache + SQLite + uvicorn | **本項目の新規実測対象**。wiki 計画 §5 では「数百 MB」と推定 |
| 合計 | service + engine | 単一プロセス（model 込み engine）と比較して、**model 分をユーザー数で割れる** のが本質的な削減 |

### 既存単一プロセス構成との比較

既存（model 込み engine 単体）の warm idle 実測は **~6.9GB**（上記 §「GPU / CPU 別の実測」）。これが service + engine に分かれたとき:
- 1 ユーザー運用: 合計は既存と同等かやや増（HTTP overhead +1-3ms、RAM は service ~5-6GB + engine 数百 MB = ほぼ同じ）
- N ユーザー運用: service 1 つ + engine N プロセス = `~5-6GB + N × 数百MB`。既存の `N × 6.9GB` から **大幅削減**

→ 実測値でこの表を上書きする（[Operations — Performance Testing](Operations-Performance-Testing.md) の Tier 6 baseline 手法を流用）。

## DB サイズの増え方

`~/.local/share/gaottt/` 配下のファイル:

| ノード数 | SQLite | FAISS (raw + virtual) | 合計目安 |
|---|---|---|---|
| 1,000 | ~20MB | ~6MB | ~25MB |
| 10,000 | ~180MB | ~60MB | ~240MB |
| **~41,000 (開発機の現在値)** | **~720MB** | **~230MB** | **~950MB** |
| 100,000 | ~1.8GB | ~600MB | ~2.4GB |
| 1,000,000 | ~18GB | ~6GB | ~24GB |

(node 1 件 ~= 平均 chunk 1KB-5KB + msgpack metadata + edges + 768 dim × 4 byte × 2 index ≒ 約 23KB/node。文書長に強く依存)

### システム RAM への影響

DB が大きくなると engine startup 時の BM25 build + virtual FAISS load で RAM が逼迫する場合がある。100k node を超える運用では:
- BM25 in-memory index: ~50-200MB
- FAISS index (raw + virtual): 各 dimension × 4 byte × N = 100k で ~600MB、合計 ~1.2GB
- SQLite cache live set (lazy load): ~10-30% of DB size

100k node 規模で常駐 backend に **+2GB 程度** が乗る計算。1M node 規模では **+15GB 以上** を見込む必要あり (FAISS 6GB だけで)。

## VRAM/RAM 不足時の挙動

### CPU fallback は自動化されていない

`gaottt/embedding/ruri.py` の `RuriEmbedder.__init__` は device 指定なしで `SentenceTransformer(...)` を構築する。sentence-transformers は **CUDA が見えれば必ず GPU を掴む** — VRAM 不足でも自動で CPU に逃げる経路はない。

### VRAM 不足時

`torch.cuda.OutOfMemoryError` がそのまま伝播する:

- **モデルロード時に踏む** (起動時): `SentenceTransformer(...)` 構築中に OOM → MCP サーバーが起動しない (proxy mode なら backend が起動失敗、shim だけ立つ)
- **推論時に踏む** (ingest / recall): model は載ったが forward pass で activation 分が overflow → `model.encode(...)` が OOM を raise
  - `recall`: MCP tool がエラー返却、その 1 ターンが失敗
  - `ingest` / `remember`: その batch が死ぬ。再試行しても `batch_size` 不変なので同じく死ぬ
  - dream loop の encode で踏むと background で例外ログが出て quiet node 巡回が停止

### RAM 不足時 (CPU mode)

Linux の OOM killer が process を SIGKILL する可能性あり (swap thrash 後に死ぬパターンも)。GPU の torch OOM のほうがある意味行儀がいい (例外で返ってくる)。

### 強制 CPU フォールバック

```bash
CUDA_VISIBLE_DEVICES="" .venv/bin/python -m gaottt.server.mcp_server
```

または `.env` / systemd unit / claude.json の env に設定。sentence-transformers が GPU を見えなくして CPU 経路に fallback する。

### ingest が VRAM/RAM 不足で落ちる時

`gaottt/config.py` の `batch_size` を 32 → 8 や 4 に下げて再試行。CPU mode では 4-8 が現実的。

```python
# gaottt/config.py
class GaOTTTConfig:
    batch_size: int = 32  # → 8 や 4 に下げる
```

将来的に env var (`GAOTTT_BATCH_SIZE` 等) で動的に絞れる knob を追加する余地あり (現状未整備)。

## 関連ページ

- [Operations — Server Setup](Operations-Server-Setup.md) — 起動モード (proxy/stdio/streamable-http) と RAM/VRAM トレードオフ
- [Operations — Troubleshooting](Operations-Troubleshooting.md) — OOM 以外の起動失敗・recall 失敗パターン
- [Operations — Tuning](Operations-Tuning.md) — ハイパーパラメータ全表 (`batch_size` 含む)
- [Research — VRAM/RAM Measurement 2026-05-28](../research/Research-VRAM-RAM-Measurement-2026-05-28.md) — 本ページの実測根拠
