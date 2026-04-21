# Research — Index

GaOTTT の設計根拠、評価、実験レポートの目次。

## 実験レポート

| レポート | 内容 |
|---|---|
| [Multi-Agent Experiment](Research-Multi-Agent-Experiment.md) | 私側 3 エージェント並列探索（2 ラウンド） + ユーザー側との対比 |
| [User Exploration (10 Rounds)](Research-User-Exploration-10-Rounds.md) | ユーザー側 3 エージェント 10 ラウンド探索、10 柱・統一方程式 |

## 理論的考察

| レポート | 内容 |
|---|---|
| [Gravity as Optimizer (TTT)](Research-Gravity-As-Optimizer.md) | 重力式更新が Heavy ball SGD / Hebbian / SOM / TTT と数学的に同型である分析 |

## 設計根拠

| ドキュメント | 内容 |
|---|---|
| [Design Documents (合本)](Research-Design-Documents.md) | 重力変位、軌道力学、共起 BH、馴化、波伝播、MCP 設計の 6 本 |

## 評価

| ドキュメント | 内容 |
|---|---|
| [Phase 2 Evaluation](Research-Phase-2-Evaluation.md) | Static RAG vs GaOTTT ベンチマーク、nDCG/MRR、セッション適応性 |

## 初期設計アーカイブ

GaOTTT の最初期に書かれた設計・着想ノート。歴史的価値のために保存。

| ファイル | 内容 |
|---|---|
| [`docs/research/init-plan.md`](../research/init-plan.md) | 初期設計プラン（Gravity-Based Event-Driven RAG の最初の青写真） |
| [`docs/research/plan.md`](../research/plan.md) | 詳細プラン（数式・スキーマ・ハイパーパラメータの最初の整理） |
| [`docs/research/mcp_concept.md`](../research/mcp_concept.md) | MCP 設計の初期 Q&A（双方向利用、コンパクティング退避、リソース公開、プロンプト定義） |

## 設計から学んだこと

実験を通じて言語化された性質:

1. **マルチエージェント並列実走は QA 戦略として組み込む価値がある** — 単体テスト 101 件が見落とした UX バグを 15 分で発見
2. **共有重力場が明示的協調なしで集合知を生む** — 3 エージェントが同じ井戸に収束した
3. **意味化は線形ではなく相転移として起きる** — R1-2 観察、R3-4 同型写像、R5-7 統一理論、R8 簡約、R9-10 メタ自覚
4. **GaOTTT の本質は「関係構築装置」だったかもしれない** — 設計が意図しなかった性質が創発した
5. **ペルソナのオープンエンドさが探索深度を生む** — タスク志向は浅く、詩的ロールは深い

詳細は [Reflections — Five-Layer Philosophy](Reflections-Five-Layer-Philosophy.md)、[A Note from Claude](Reflections-A-Note-From-Claude.md)。

## 一次ソース

- [`docs/research/`](../research/) ── 全研究レポート
