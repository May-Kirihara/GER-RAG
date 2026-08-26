"""Phase U WP-2 — multiverse supervisor の runtime-tuning env allowlist。

背景 (R1 / Plans-Phase-U-Review-Hardening.md §4 WP-2): supervisor の
``_build_spawn_env`` は proxy-backend env-inheritance trap 対策で全
``GAOTTT_*`` を strip する設計 (MV3) だったため、Phase T/U の tuning knob
(``GAOTTT_DIRECT_QUALIFICATION_ENABLED=false`` 等の env rollback) が
multiverse backend に届かなかった。本 module はその rollback 経路を
**閉じた完全名 allowlist** で開く:

* ``RUNTIME_TUNING_ENV_ALLOWLIST`` — exact-name の閉集合。prefix /
  wildcard 一致は無く、未来の knob は自動的に deny (allowlist 拡張は
  review 経由のみ)。
* ``GAOTTT_CONFIG`` は伝播しない — 任意の config field を含め得るため
  tuning-only allowlist と矛盾する (Codex review 指摘)。永続 rollback は
  supervisor 起動元の service 定義に env を置く運用で対応する。
* 値検証は ``GaOTTTConfig._coerce_env`` と同じ coercion 規則で行い
  (parse logic の複製をしない)、不正値は backend spawn を拒否
  (fail-fast)。backend 側 ``_resolve_overrides`` は unparseable 値を
  log-and-drop、bool は黙って False coerce してしまうため、spawn 時点で
  拒否しないと静かな挙動変化になる。
* identity 系 (``GAOTTT_DATA_DIR`` / ``GAOTTT_EMBEDDER_ENDPOINT`` /
  ``GAOTTT_OWNER_LEASE_ENABLED`` / ``GAOTTT_BACKEND_TOKEN``) は allowlist
  外 — supervisor の明示上書きが常に勝つ (caller は ``tuning_env``
  経由では触れない)。

純粋な ops 層: physics / store は import しない。``gaottt.config`` は
field 定義 (dataclasses.fields) と coercion 規則の参照のみに使う。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import MISSING, fields

from gaottt.config import GaOTTTConfig

# 閉じた完全名 allowlist。対象は Phase T/U の tuning knob のみ — 全項目が
# ``GaOTTTConfig`` の bool/int/float scalar field に 1:1 対応し、対応が
# なくなった場合は ``validate_tuning_env`` が常時 error を返す
# (staleness guard — allowlist は実 field を追跡しなければならない)。
# identity 系 4 key と GAOTTT_CONFIG は意図的に含めない。
RUNTIME_TUNING_ENV_ALLOWLIST: frozenset[str] = frozenset({
    # Phase T Stage 2 — semantic decay (half-life + floor 契約)
    "GAOTTT_SEMANTIC_HALFLIFE_ENABLED",
    "GAOTTT_SEMANTIC_HALF_LIFE_SECONDS",
    "GAOTTT_SEMANTIC_FLOOR",
    # Phase T Stage 3/4 — direct relevance / TTT update qualification
    # (Phase U WP-1 で code default True 昇格 → env rollback 経路が本件)
    "GAOTTT_DIRECT_QUALIFICATION_ENABLED",
    "GAOTTT_DIRECT_RAW_COSINE_MIN",
    "GAOTTT_DIRECT_VIRTUAL_COSINE_MIN",
    "GAOTTT_DIRECT_BM25_RELATIVE_MIN",
    "GAOTTT_DIRECT_BM25_ABSOLUTE_MIN",
    "GAOTTT_DIRECT_BM25_POOL_SIZE",
    "GAOTTT_TTT_QUALIFICATION_ENABLED",
    # Phase U WP-4b — raw-top rescue (default 3 の env rollback 面)
    "GAOTTT_DIRECT_RESCUE_RAW_RANK",
    # Phase T Stage 6 — explore presentation diversity (MMR)
    "GAOTTT_EXPLORE_DIVERSIFIED_PRESENTATION_ENABLED",
    "GAOTTT_EXPLORE_COHORT_PENALTY",
    "GAOTTT_EXPLORE_MIN_SEMANTIC",
    "GAOTTT_EXPLORE_DIVERSITY_POOL_MULTIPLIER",
    # Phase T Stage 5 — ambient gate OR semantics
    "GAOTTT_AMBIENT_GATE_OR_SEMANTIC",
    "GAOTTT_AMBIENT_SEMANTIC_RAW_MIN",
    "GAOTTT_AMBIENT_BM25_MIN_SCORE",
    "GAOTTT_AMBIENT_MIN_SCORE",
    "GAOTTT_AMBIENT_GATE_USE_BM25",
    # Phase U §10 R3 follow-up — ambient composite gate 3-arm。しきい値系
    # 4 件は tuning 対象。GAOTTT_AMBIENT_COMPOSITE_REFERENCE_FILENAME は
    # **意図的に含めない** — 参照 artifact の置き場所は deployment の構造
    # 選択 (universe data_dir と紐づく) なので env 経由の runtime 変更は
    # 想定せず、config-file only とする。
    "GAOTTT_AMBIENT_GATE_MODE",
    "GAOTTT_AMBIENT_COMPOSITE_VIRT_HI",
    "GAOTTT_AMBIENT_COMPOSITE_BM25_MID",
    "GAOTTT_AMBIENT_COMPOSITE_VIRT_MID",
    "GAOTTT_AMBIENT_COMPOSITE_COUNT_DRIFT_MAX",
    # Phase U WP-6b/6c/6d — staged readiness / BM25 background build /
    # snapshot (R5 cold-start 対策の rollback 経路)
    "GAOTTT_READINESS_PROTOCOL_ENABLED",
    "GAOTTT_BM25_BACKGROUND_BUILD_ENABLED",
    "GAOTTT_BM25_SNAPSHOT_ENABLED",
})

# bool field の認識 token。真値側は ``_coerce_env`` の認識集合そのもの
# (関数経由で判定するため、``_coerce_env`` の真値定義が変われば自動追従)。
# 偽値側はその補集合として明示する — ``_coerce_env`` は bool に対して
# 決して raise しないため、「認識できない token」(例: "banana") を弁別
# するには偽値側の閉集合が必要。認識外 token は backend 側で黙って
# False coerce される (``bool("false") is True`` trap の逆) ので、
# spawn 時点で拒否する。
_FALSY_BOOL_TOKENS = frozenset({"0", "false", "no", "off"})


class TuningEnvValidationError(ValueError):
    """runtime-tuning env の値検証失敗 (backend spawn 拒否用、fail-fast)。

    ``.errors`` に人間可読の検証 error list を保持する。supervisor の
    /route handler が 500 detail に載せて観測可能にする。
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _tuning_field_types() -> dict[str, type]:
    """env var 名 → ``GaOTTTConfig`` scalar field 型の対応を組み立てる。

    ``GaOTTTConfig._resolve_overrides`` と同一の規則 (具体 default を持つ
    bool/int/float/str field を ``GAOTTT_<FIELD>`` に対応付ける) を
    ``dataclasses.fields`` 経由で再現する。呼び出し毎に組み立てるため
    staleness guard は常に現状の class 定義に対して効く。
    """
    out: dict[str, type] = {}
    for f in fields(GaOTTTConfig):
        if f.default is MISSING:
            continue
        target = type(f.default)
        if target not in (bool, int, float, str):
            continue
        out[f"GAOTTT_{f.name.upper()}"] = target
    return out


# str enum knob の許容値 (tuple = error message の表示順)。``_coerce_env``
# は str を identity で通すため汎用 parse では意味的検査にならない —
# 認識外の値が backend まで流れると ``GaOTTTConfig.__post_init__`` が
# backend 起動時に raise し、spawn 成功后の初回接続失敗という観測しにくい
# 形で壊れる。ここ (spawn 時点) で拒否するための閉集合。services 側の
# 定義は import しない (ops 層は physics/services に依存しない設計契約)。
_ENUM_ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "GAOTTT_AMBIENT_GATE_MODE": ("or", "composite"),
}


def validate_tuning_env(env: Mapping[str, str]) -> list[str]:
    """Allowlisted tuning env の値を検証し、人間可読 error list を返す。

    空 list = 有効。検証規則:

    * staleness guard — allowlist 項目が実 config field に対応しなければ
      **常時** error (env に値がなくても)。allowlist が実 field を追跡
      することを強制する hard error。
    * 空値 (空文字 / whitespace のみ) は拒否。
    * bool は認識 token (1/true/yes/on/0/false/no/off、大小文字・前後
      空白は無視) のみ許可 — 判定の真値側は ``_coerce_env`` を再利用。
    * int/float は ``_coerce_env`` で parse できなければ拒否
      (parse logic の複製をしない)。
    * float は NaN / ±Infinity を拒否 (``float()`` は通るが semantically
      不正 — 閾値 knob に非有限値は無意味)。
    * str enum knob (``GAOTTT_AMBIENT_GATE_MODE``) は
      ``_ENUM_ALLOWED_VALUES`` の閉集合外を拒否 — identity coercion では
      検査にならないため、config ``__post_init__`` と同じ基準を spawn
      時点で先に効かせる。

    allowlist 外の ``GAOTTT_*`` は error にしない — それらは strip 側
    (``_build_spawn_env``) の管轄であり、ここでは何もしないのが正しい。
    """
    errors: list[str] = []
    field_types = _tuning_field_types()

    # staleness guard: allowlist 項目 → 実 field 対応の全数検査。
    for name in sorted(RUNTIME_TUNING_ENV_ALLOWLIST):
        if name not in field_types:
            errors.append(
                f"{name}: allowlisted but has no matching GaOTTTConfig "
                f"scalar field (stale allowlist entry — remove or remap it)"
            )

    for name in sorted(RUNTIME_TUNING_ENV_ALLOWLIST):
        raw = env.get(name)
        if raw is None:
            continue
        target = field_types.get(name)
        if target is None:
            continue  # staleness guard が既に報告済み
        if not raw.strip():
            errors.append(f"{name}: value must not be empty")
            continue
        if target is bool:
            token = raw.strip().lower()
            truthy = GaOTTTConfig._coerce_env(token, bool)  # noqa: SLF001 — 同一 package 内での規則再利用
            if not truthy and token not in _FALSY_BOOL_TOKENS:
                errors.append(
                    f"{name}={raw!r}: not a recognized boolean token "
                    f"(expected one of 1/true/yes/on/0/false/no/off)"
                )
            continue
        try:
            parsed = GaOTTTConfig._coerce_env(raw, target)  # noqa: SLF001
        except (ValueError, TypeError) as exc:
            errors.append(
                f"{name}={raw!r}: cannot parse as {target.__name__} ({exc})"
            )
            continue
        if target is float and not math.isfinite(parsed):
            errors.append(
                f"{name}={raw!r}: non-finite float (NaN/Infinity) is rejected"
            )
            continue
        allowed = _ENUM_ALLOWED_VALUES.get(name)
        if allowed is not None and parsed not in allowed:
            errors.append(
                f"{name}={raw!r}: invalid value "
                f"(expected {' or '.join(repr(v) for v in allowed)})"
            )
    return errors


def filter_tuning_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` から allowlisted key のみを返す (検証済みであること)。

    不正値があれば :class:`TuningEnvValidationError` を raise する
    (fail-fast — 不正値を黙って drop すると「設定したのに効かない」
    静かな事故になるため、呼び出し側で拒否できる形にする)。
    """
    errors = validate_tuning_env(env)
    if errors:
        raise TuningEnvValidationError(errors)
    return {
        name: env[name]
        for name in sorted(RUNTIME_TUNING_ENV_ALLOWLIST)
        if name in env
    }
