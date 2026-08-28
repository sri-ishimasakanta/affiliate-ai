"""HTTP API 層。

Router は HTTP 入出力 / DI / Service 呼び出し / レスポンス返却のみを担う。
永続化・トランザクション・重複判定・status 遷移判定は Service / Repository に委ねる。
"""
