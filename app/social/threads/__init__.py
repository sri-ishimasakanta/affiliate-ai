"""Threads 連携 (T1: API 基盤のみ)。

この段階で作るのは **接続の土台だけ** である:

- 認証つき HTTP の関心事 (:mod:`app.social.threads.client`)
- アプリケーション側の意味論 (:mod:`app.social.threads.service`)
- 公式 API から確認した定数と形 (:mod:`app.social.threads.models`)
- sanitized な失敗の語彙 (:mod:`app.social.threads.errors`)

**投稿文は作らない。公開もしない。** 文章生成は T2、計測は T3 の担当で、
公開は「人が承認した不変の提案」を経由したときにだけ起きる (C8.8 の境界)。
"""
