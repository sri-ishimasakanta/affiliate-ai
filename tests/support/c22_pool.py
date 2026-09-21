"""現在の 30 keyword pool (C2.2 / C2.4 の test で共有する固定データ)。

実データは git 管理外の ``data/`` にあるため、test を自己完結させるためここに埋め込む。
"""

from __future__ import annotations

# 現在の 30 keyword pool (id, keyword, opportunity_score, 欠けている signal)。
# 実データは git 管理外の ``data/`` にあるため、test は自己完結させるためここに埋め込む。
POOL_30: tuple[tuple[int, str, float | None, str], ...] = (
    (1, "ChatGPT とは", 37.4, ""),
    (2, "ChatGPT 使い方", 44.48, ""),
    (3, "生成AI とは", 52.98, ""),
    (4, "AI 業務効率化", 67.27, ""),
    (5, "AI 議事録", 57.92, ""),
    (6, "AI 議事録 使い方", None, "competition_ease"),
    (7, "ChatGPT 無料", 46.9, ""),
    (8, "AI 議事録 無料", 59.49, ""),
    (9, "生成AI 無料", 58.03, ""),
    (10, "業務効率化 ツール 無料", 60.39, ""),
    (11, "ChatGPT 料金", 53.13, ""),
    (12, "ChatGPT Plus 料金", 56.4, ""),
    (13, "Notion AI 料金", 56.28, ""),
    (14, "Zapier 料金", 54.19, ""),
    (15, "Make 料金", None, "competition_ease"),
    (16, "AI 議事録 料金", None, "competition_ease"),
    (17, "AI 議事録 おすすめ", None, "competition_ease"),
    (18, "AI 議事録 比較", None, "competition_ease"),
    (19, "生成AI ツール おすすめ", None, "competition_ease"),
    (20, "生成AI ツール 比較", None, "competition_ease"),
    (21, "業務効率化 ツール おすすめ", 68.81, ""),
    (22, "業務効率化 ツール 比較", None, "competition_ease"),
    (23, "RPA おすすめ", 50.94, ""),
    (24, "RPA 比較", 50.47, ""),
    (25, "議事録 自動作成 ツール", 53.48, ""),
    (26, "文字起こし AI おすすめ", None, "competition_ease"),
    (27, "法人向け 生成AI", None, "competition_ease"),
    (28, "生成AI 法人 導入", None, "competition_ease|trend"),
    (29, "AI 業務効率化 導入", None, "competition_ease|trend"),
    (30, "RPA 導入", 50.54, ""),
)
