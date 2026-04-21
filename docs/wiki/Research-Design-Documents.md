# Research — Design Documents

GER-RAG の物理機構の設計根拠を集めた研究ドキュメント群。

## 文書一覧

| 文書 | 内容 |
|---|---|
| [Gravitational Displacement Design](../research/gravitational-displacement-design.md) | 重力座標変位の設計、二重座標系の理論的根拠 |
| [Gravity Wave Propagation Design](../research/gravity-wave-propagation-design.md) | 再帰的重力波伝播、mass 依存 top-k、重力半径 |
| [Orbital Mechanics Design](../research/orbital-mechanics-design.md) | 速度ベクトル、軌道力学、Hooke's law アンカー、彗星軌道 |
| [Co-occurrence Black Hole Design](../research/cooccurrence-blackhole-design.md) | 共起クラスタを超大質量 BH として表現 |
| [Habituation & Thermal Escape Design](../research/habituation-escape-design.md) | 返却飽和と温度脱出メカニクス |
| [MCP Server Design](../research/mcp-server-design.md) | AI エージェント外部長期記憶としての MCP 設計 |

## 共通テーマ

これら 6 本の設計文書は、**「物理アナロジーを比喩ではなく式として実装する」** という GER-RAG の設計思想を支えている:

- 万有引力 `F = G×m_i×m_j/d²` をそのままコードに
- 軌道力学（加速度→速度→位置）を 3 段階で実装
- Hooke's law の復元力を「アンカー引力」として
- ホーキング輻射を Hawking radiation として
- 共起の重心を超大質量 BH として

物理メタファーは **デコレーション** ではなく、**設計上の制約と着想の源泉** だった。各設計文書は、なぜこの式を選んだか、他の選択肢と何が違うか、を記述している。

→ 哲学的整理: [Five-Layer Philosophy](Reflections-Five-Layer-Philosophy.md)
→ 実装: [Architecture — Gravity Model](Architecture-Gravity-Model.md)
