# Operations — Compact & Backup

定期メンテナンス（`compact()`）と永続化ファイルのバックアップ。

## compact() の運用

長期運用では archived/expired ノードのベクトルが FAISS に残り、wave 伝播のヒット率を下げる。週次〜月次で `compact()` を実行:

```python
# Python から（engine 直叩き）
await engine.compact()                              # 安全な既定
await engine.compact(auto_merge=True, merge_threshold=0.95)
await engine.compact(rebuild_faiss=False, auto_merge=False)
```

MCP 経由:
```
compact()                                           # 既定
compact(auto_merge=true, merge_threshold=0.95)
```

レポート例:
```
Compaction complete:
  TTL-expired:    3
  Auto-merged:    2 pairs
  FAISS rebuilt:  True
  FAISS vectors:  1503 → 1496
```

## compact が行うこと

| 処理 | 物理アナロジー |
|---|---|
| TTL 期限切れ → archive | 仮想粒子の崩壊 |
| archived ベクトルを FAISS から除去 | ホーキング輻射の最終段階 |
| FAISS rebuild (id_map 詰め直し) | 真空ゼロ点リセット |
| 近接ノード対の衝突合体 (auto_merge=True) | 銀河衝突合体 |
| orphan 有向エッジの掃除 | 散逸 |

## 永続化ファイル

| ファイル | 内容 | 消失時の影響 |
|---|---|---|
| `gaottt.db` | SQLite DB（documents + nodes + edges + directed_edges） | 全データ消失、再投入必要 |
| `gaottt.faiss` | FAISS ベクトルインデックス | 起動時に再構築不可、再投入必要 |
| `gaottt.faiss.ids` | FAISS 位置 → document ID マッピング | 上記同様 |

## バックアップ

```bash
# サーバー停止中に実行
cp ~/.local/share/gaottt/gaottt.db ~/.local/share/gaottt/gaottt.db.bak
cp ~/.local/share/gaottt/gaottt.faiss ~/.local/share/gaottt/gaottt.faiss.bak
cp ~/.local/share/gaottt/gaottt.faiss.ids ~/.local/share/gaottt/gaottt.faiss.ids.bak
```

**注意**: サーバー稼働中のバックアップは dirty 状態がフラッシュされていない可能性。確実なバックアップにはサーバー停止が必要。

## 完全リセット

```bash
# サーバー停止後
rm ~/.local/share/gaottt/gaottt.db
rm ~/.local/share/gaottt/gaottt.faiss
rm ~/.local/share/gaottt/gaottt.faiss.ids
# サーバー再起動で空の状態から開始
```

## 定期実行（cron 例）

```cron
# 毎週日曜 03:00 に compact 実行（MCP 経由）
0 3 * * 0 echo 'compact()' | claude-code --mcp gaottt --no-interactive
```

→ 関連: [Tuning](Operations-Tuning.md), [Architecture — Storage & Schema](Architecture-Storage-And-Schema.md)
