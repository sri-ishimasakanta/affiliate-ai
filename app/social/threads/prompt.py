"""Threads 投稿案の prompt 生成 (T2、pure)。

このリポジトリの記事生成と **同じやり方** に揃える: prompt はローカルで決定的に
組み立て、生成そのものは外部で人が動かし、結果を submit して取り込む
(:mod:`app.services.draft_generation_adapters` の ManualAdapter と同じ形)。

新しい AI provider の抽象は作らない。

prompt には:

- 記事の事実 (本文) -- **これが事実の境界**
- 切り口 (angle) ごとの指示
- 文体ポリシー (版つき)
- 多様性の規則と、学習からの弱い参考 (T5.5。``guidance`` を渡したときだけ)
- 出力形式 (JSON)

学習の節は、事実の境界・文体・多様性より **弱い** と明記した別の節に置く。
``guidance`` を渡さなければ、T5.5 より前とまったく同じ prompt になる。

を入れる。同じ入力からは必ず同じ prompt が出る (``prompt_hash`` で確認できる)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.social.threads.guidance import ThreadsGenerationGuidance, render_prompt_sections
from app.social.threads.policy import ThreadsStylePolicy
from app.social.threads.proposal import LINK_MODES, LINK_PLACEHOLDER

#: 切り口ごとの狙い。生成器はこれを読んで書き分ける。
ANGLE_BRIEFS = {
    "insight": "記事の要点のうち、読んで意外に思う点を 1 つだけ取り出す。結論から書く。",
    "common_mistake": "よくある間違いと、その代わりに何をすればよいかを 1 つ書く。",
    "comparison": "選択肢の違いを 1 つの軸だけで比べる。全部を並べない。",
    "question": "読み手への自然な問いから入り、記事で確認できた答えの入口まで書く。",
    "beginner_tip": "最初の 1 歩だけを具体的に書く。網羅しない。",
}

#: 記事本文をそのまま全部入れない (prompt を膨らませない)。
MAX_SOURCE_CHARACTERS = 6000


@dataclass(frozen=True)
class ThreadsPromptPackage:
    """1 回の生成に渡す入力一式。"""

    source_article_id: int
    source_article_title: str
    source_article_body_hash: str
    angles: tuple[str, ...]
    policy_version: str
    rendered_prompt: str
    #: この prompt に入れた学習の参考 (T5.5)。無ければ ``None``。
    guidance: ThreadsGenerationGuidance | None = None

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(self.rendered_prompt.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "source_article_id": self.source_article_id,
            "source_article_title": self.source_article_title,
            "source_article_body_hash": self.source_article_body_hash,
            "angles": list(self.angles),
            "policy_version": self.policy_version,
            "prompt_hash": self.prompt_hash,
            "learning_guidance": self.guidance.as_dict() if self.guidance else None,
        }


def build_prompt(
    *,
    source_article_id: int,
    source_article_title: str,
    source_article_body: str,
    source_article_body_hash: str,
    angles,
    policy: ThreadsStylePolicy,
    guidance: ThreadsGenerationGuidance | None = None,
) -> ThreadsPromptPackage:
    """決定的に prompt を組み立てる (外部呼び出しはしない)。"""

    wanted = tuple(a for a in angles if a in policy.angles)
    if not wanted:
        raise ValueError("no supported angle was requested")

    body = (source_article_body or "").strip()
    truncated = len(body) > MAX_SOURCE_CHARACTERS
    excerpt = body[:MAX_SOURCE_CHARACTERS]

    lines = [
        "あなたは BizFluxLab の Threads 投稿を書く。",
        "",
        "## 立ち位置",
        "企業メディアの投稿より少し人間味があり、個人の雑談より情報価値が高い。",
        "記事の要約ではない。同じ論点を Threads 用に書き直す。",
        "",
        "## 文体",
        f"- 1 投稿 {policy.max_characters} 文字以内 (目安 {policy.preferred_max_characters} 文字)",
        "- 口語寄り。短い文。体言止めや言い切りを使ってよい",
        "- 結論を先に置く",
        "- リンクを開かなくても、それだけで役に立つ内容にする",
        f"- 問いかけは多くても {policy.max_questions} 個",
        f"- 絵文字は多くても {policy.max_emoji} 個",
        f"- 「〜{'」「〜'.join(policy.polite_markers)}」で終わる文を "
        f"{policy.max_consecutive_polite_endings} 文以上続けない",
        "",
        "## 書いてはいけないこと",
    ]
    lines += [f"- {item}" for item in policy.prohibitions]
    lines += [f"- 「{phrase}」のような煽り" for phrase in policy.banned_phrases]
    lines += [
        "",
        "## 避けたい言い回し (記事の書き出しのような硬さ)",
    ]
    lines += [f"- {phrase}" for phrase in policy.discouraged_phrases]
    lines += [
        "",
        "## 事実の扱い",
        "下の記事本文が **事実の境界** である。ここに無いことは書かない。",
    ]
    lines += [f"- {rule}" for rule in policy.factual_rules]
    lines += [
        "",
        "## 切り口",
    ]
    lines += [f"- {angle}: {ANGLE_BRIEFS.get(angle, '')}" for angle in wanted]
    lines += [
        "",
        "## リンク",
        f"リンクを入れる投稿では、本文中の入れたい位置に {LINK_PLACEHOLDER} と書く。",
        "URL は書かない (こちらで決定的に組み立てる)。",
        f"link_mode は {' か '.join(LINK_MODES)} のどちらか。",
        "リンクが宣伝臭くなる投稿では none にしてよい。",
        "",
    ]
    if guidance is not None:
        lines += [*render_prompt_sections(guidance), ""]
    lines += [
        "## 出力形式",
        "次の形の JSON だけを返す。説明文は付けない。",
        json.dumps(
            {
                "proposals": [
                    {"angle": wanted[0], "link_mode": "article", "body": f"...{LINK_PLACEHOLDER}"}
                ]
            },
            ensure_ascii=False,
        ),
        "",
        f"## 記事 (id={source_article_id}): {source_article_title}",
        excerpt,
    ]
    if truncated:
        lines.append("(本文はここで切ってある。書かれていないことは補わない)")

    return ThreadsPromptPackage(
        source_article_id=source_article_id,
        source_article_title=source_article_title,
        source_article_body_hash=source_article_body_hash,
        angles=wanted,
        policy_version=policy.policy_version,
        rendered_prompt="\n".join(lines),
        guidance=guidance,
    )


def parse_generated(raw_output: str) -> list[dict]:
    """生成結果 (JSON) を下書きの一覧に変える。壊れていれば例外。

    推測で補完しない -- 形が違えば失敗させ、人がやり直す。
    """

    text = (raw_output or "").strip()
    if text.startswith("```"):
        # コードフェンス付きで返ってくることがあるので剥がす。
        text = text.strip("`")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower() in ("json", ""):
                text = rest
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("the generated output must be a JSON object")
    items = payload.get("proposals")
    if not isinstance(items, list) or not items:
        raise ValueError("the generated output carried no proposals")
    drafts: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each proposal must be an object")
        angle = item.get("angle")
        body = item.get("body")
        if not isinstance(angle, str) or not isinstance(body, str) or not body.strip():
            raise ValueError("each proposal needs an angle and a non-empty body")
        drafts.append(
            {
                "angle": angle,
                "body": body,
                "link_mode": item.get("link_mode") or "none",
            }
        )
    return drafts
