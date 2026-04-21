# Maintainers Guide

このディレクトリは **GaOTTT リポジトリの保守者・コントリビュータ向け** のドキュメントです。
ユーザー向けではなく、リポジトリを編集・運用する人が必要な手順をまとめています。

ユーザー向けドキュメントは [`docs/wiki/`](../wiki/Home.md) を参照。

## 目次

| ドキュメント | 内容 |
|---|---|
| [Wiki Sync Workflow](wiki-sync.md) | `docs/wiki/` → GitHub Wiki repo の自動同期、リンク変換、ページ追加・削除の手順 |
| [Rename Plan: GER-RAG → GaOTTT](rename-to-gaottt-plan.md) | 改名プロジェクトの全 Phase の作業計画と決定事項 |
| [Rename Handover (Session 1-3)](rename-handover.md) | 改名プロジェクトの各セッション引き継ぎ・完了記録 |
| [Session Handover — 2026-04-21](handover.md) | 改名後のドキュメント温度調整 + bootstrap_report 実装セッションの引き継ぎ |
| [REST × MCP Unification Plan](rest-mcp-unification-plan.md) | REST API を MCP parity まで引き上げ、共有サービス層に集約するリファクタ計画（Phase S0-S6） |

## 将来追加されうる項目

- リリース手順（バージョニング、CHANGELOG、tag、PyPI 公開等）
- 依存関係の更新方針
- Phase 計画の昇格手順（実装完了 → ロードマップ更新）
- ベンチマーク回帰の判定基準
- セキュリティ報告の取り扱い

新しい保守 workflow を追加するときは、ここに 1 ファイル追加 + 上の表に行追加。
